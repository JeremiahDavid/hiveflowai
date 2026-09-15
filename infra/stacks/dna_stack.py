from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    Stack,
    Tags,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    custom_resources as cr,
)
from constructs import Construct

from iam_grants import grant_athena_query, grant_bedrock_semantic_access, grant_glue_catalog_sync
from lambda_bundle import HiveFlowLambdaRuntime, hiveflow_lambda_runtime

SOURCE_DOCUMENTATION_BUCKET_NAME = "hiveflowai-source-documentation"


class DnaStack(Stack):
    """DNA Semantic Engine stack — independent of ingest; reads silver, writes gold."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        company: str,
        environment: str,
        data_bucket_name: str,
        source: str,
        dna_config: dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not data_bucket_name.strip():
            raise ValueError("data_bucket_name is required for DnaStack")
        if not source.strip():
            raise ValueError("source connector is required for DnaStack (typically dbc)")

        self._apply_cost_allocation_tags(company, environment)

        data_bucket = s3.Bucket.from_bucket_name(
            self,
            "ImportedDataBucket",
            data_bucket_name,
        )

        from hiveflow.storage.paths import company_dna_config_id

        # Gold semantic layer always uses the company DNA config pack.
        pack_id = company_dna_config_id(company)
        lambda_runtime = hiveflow_lambda_runtime(self)
        from glue_bundle import hiveflow_glue_dna_assets, hiveflow_glue_extra_py_files_asset

        glue_extra_py_files = hiveflow_glue_extra_py_files_asset(self)
        glue_dna_assets = hiveflow_glue_dna_assets(self, extra_py_files_asset=glue_extra_py_files)
        dna_publish_fn = self._create_dna_publish_lambda(
            data_bucket=data_bucket,
            lambda_runtime=lambda_runtime,
            company=company,
            environment=environment,
            pack_id=pack_id,
        )
        source_docs_gold_fn = self._create_source_docs_gold_lambda(
            data_bucket=data_bucket,
            lambda_runtime=lambda_runtime,
            company=company,
            environment=environment,
            source=source,
        )

        schedule_cfg = dna_config.get("schedule", {})
        if not isinstance(schedule_cfg, dict):
            schedule_cfg = {}
        schedule_hour = schedule_cfg.get("hour")
        schedule_minute = schedule_cfg.get("minute")
        if schedule_hour is not None:
            schedule_hour = int(schedule_hour)
        if schedule_minute is not None:
            schedule_minute = int(schedule_minute)

        from dna_pipeline import create_dna_pipeline
        from dna_refresh_glue import create_dna_refresh_glue_job

        dna_glue = create_dna_refresh_glue_job(
            self,
            "Dna",
            company=company,
            environment=environment,
            data_bucket=data_bucket,
            glue_assets=glue_dna_assets,
            grant_glue_catalog_sync=grant_glue_catalog_sync,
            grant_athena_query=grant_athena_query,
            source=source,
        )

        resources = create_dna_pipeline(
            self,
            "Dna",
            company=company,
            environment=environment,
            source=source,
            dna_glue_job=dna_glue["glue_job"],
            dna_glue_default_arguments=dna_glue["default_arguments"],
            schedule_hour=schedule_hour,
            schedule_minute=schedule_minute,
            pack_id=pack_id,
        )

        self._seed_governance_on_deploy(
            dna_publish_fn,
            company=company,
            environment=environment,
            pack_id=pack_id,
        )

        self._create_portal_tenant_role(
            company=company,
            environment=environment,
            data_bucket=data_bucket,
            source_docs_gold_fn=source_docs_gold_fn,
        )

        CfnOutput(self, "DataBucketName", value=data_bucket_name)
        CfnOutput(self, "DnaPublishFunctionName", value=dna_publish_fn.function_name)
        CfnOutput(self, "DnaRefreshGlueJobName", value=dna_glue["glue_job_name"])
        CfnOutput(
            self,
            "BcSourceDocsGoldFunctionName",
            value=source_docs_gold_fn.function_name,
        )
        CfnOutput(
            self,
            "DnaRefreshStateMachineArn",
            value=resources["state_machine"].state_machine_arn,
        )
        CfnOutput(
            self,
            "DnaRefreshStateMachineName",
            value=resources["state_machine"].state_machine_name,
        )

    def _apply_cost_allocation_tags(self, company: str, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags(company, environment).items():
            Tags.of(self).add(key, value)

    def _create_portal_tenant_role(
        self,
        *,
        company: str,
        environment: str,
        data_bucket: s3.IBucket,
        source_docs_gold_fn: _lambda.Function,
    ) -> iam.Role:
        """Narrow role the multi-tenant portal Lambda assumes to touch THIS company's data.

        The shared ``PortalStack`` Lambda has no standing access to any tenant's
        bucket/Athena/Step Functions; it assumes this role per request. Trust is
        pinned to the shared serve role by ARN so no cross-stack ref is needed.
        """
        from hiveflow.project_config import (
            agent_pipelines_role_name,
            spreadsheet_engine_state_machine_name,
        )

        company_slug = company.strip().lower()
        env_slug = environment.strip().lower()
        serve_role_arn = (
            f"arn:aws:iam::{self.account}:role/hiveflow-portal-{env_slug}-serve-role"
        )
        agent_pipelines_role_arn = (
            f"arn:aws:iam::{self.account}:role/{agent_pipelines_role_name(env_slug)}"
        )
        role = iam.Role(
            self,
            "PortalTenantRole",
            role_name=f"hiveflow-portal-tenant-{company_slug}-{env_slug}",
            assumed_by=iam.AccountPrincipal(self.account).with_conditions(
                {
                    "ArnEquals": {
                        "aws:PrincipalArn": [serve_role_arn, agent_pipelines_role_arn]
                    }
                }
            ),
            description=(
                f"Data-plane access for portal client company {company_slug} "
                f"({env_slug}); assumed per request by the shared portal Lambda "
                f"and by global agent-pipeline Lambdas (e.g. the Spreadsheet Engine)"
            ),
            max_session_duration=Duration.hours(1),
        )

        data_bucket.grant_read_write(role)
        grant_athena_query(role, company=company, environment=environment)
        source_docs_gold_fn.grant_invoke(role)

        # Starting/watching an execution of the shared, global Spreadsheet
        # Engine pipeline (GlobalAgentPipelinesStack) happens under this same
        # assumed tenant role — the portal request that kicks it off is already
        # running with these credentials installed — so its fixed state
        # machine name is granted here alongside the per-company `{company}-
        # {env}-*` pattern used by this company's own DNA/ingest pipelines.
        shared_spreadsheet_state_machine = spreadsheet_engine_state_machine_name(env_slug)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[
                    f"arn:aws:states:{self.region}:{self.account}:stateMachine:"
                    f"{company_slug}-{env_slug}-*",
                    f"arn:aws:states:{self.region}:{self.account}:stateMachine:"
                    f"{shared_spreadsheet_state_machine}",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution", "states:StopExecution"],
                resources=[
                    f"arn:aws:states:{self.region}:{self.account}:execution:"
                    f"{shared_spreadsheet_state_machine}:*",
                    f"arn:aws:states:{self.region}:{self.account}:execution:"
                    f"{company_slug}-{env_slug}-*:*"
                ],
            )
        )

        CfnOutput(
            self,
            "PortalTenantRoleArn",
            value=role.role_arn,
            export_name=f"hiveflow-portal-tenant-{company_slug}-{env_slug}-role-arn",
        )
        return role

    def _seed_governance_on_deploy(
        self,
        dna_publish_fn: _lambda.Function,
        *,
        company: str,
        environment: str,
        pack_id: str,
    ) -> None:
        """Seed governance boilerplates on stack create/update via CFN Provider.

        Uses a Provider (not AwsCustomResource Lambda invoke) so handler failures
        fail the deploy instead of reporting CREATE_COMPLETE with FunctionError.
        """
        provider = cr.Provider(
            self,
            "GovernanceInitProvider",
            on_event_handler=dna_publish_fn,
        )
        # Distinct id from the previous AwsCustomResource so CFN replaces the broken seed.
        seed = CustomResource(
            self,
            "InitClientGovernanceV2",
            service_token=provider.service_token,
            properties={
                "pack_id": pack_id,
                # Bump to force re-invoke after seed logic changes.
                "seed_revision": "20260804-company-dna-config",
                "company": company,
                "environment": environment,
            },
        )
        seed.node.add_dependency(dna_publish_fn)
        CfnOutput(
            self,
            "GovernanceInitResource",
            value=f"{company.lower()}-{environment}-governance-init-v2",
            description="Custom resource that seeds governance/ on DnaStack deploy (skips if present)",
        )

    def _create_dna_publish_lambda(
        self,
        *,
        data_bucket: s3.IBucket,
        lambda_runtime: HiveFlowLambdaRuntime,
        company: str,
        environment: str,
        pack_id: str,
    ) -> _lambda.Function:
        from hiveflow.process_config import Process, lambda_name_for_process

        dna_fn = _lambda.Function(
            self,
            "DnaPublishFunction",
            function_name=lambda_name_for_process(company, environment, "all", Process.DNA_PUBLISH),
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.dna.lambda_handler.lambda_handler",
            timeout=Duration.minutes(10),
            memory_size=512,
            description=(
                f"DNA publish: compile, validate, and publish certified gold tables "
                f"for {company}/{environment}"
            ),
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment={
                "HIVEFLOW_COMPANY": company,
                "HIVEFLOW_ENVIRONMENT": environment,
                "HIVEFLOW_S3_BUCKET": data_bucket.bucket_name,
                "HIVEFLOW_DNA_PACK_ID": pack_id,  # {company}_dna_config
                "HIVEFLOW_BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            },
        )

        data_bucket.grant_read_write(dna_fn)
        grant_glue_catalog_sync(dna_fn, company=company, environment=environment)
        grant_athena_query(dna_fn, company=company, environment=environment)
        self._grant_bedrock_semantic_access(dna_fn)
        return dna_fn

    def _create_source_docs_gold_lambda(
        self,
        *,
        data_bucket: s3.IBucket,
        lambda_runtime: HiveFlowLambdaRuntime,
        company: str,
        environment: str,
        source: str,
    ) -> _lambda.Function:
        """Merge global MS Learn source docs with client overlays into gold YAML."""
        source_docs_bucket_name = SOURCE_DOCUMENTATION_BUCKET_NAME
        connector = source.strip().lower() or "dbc"
        gold_fn = _lambda.Function(
            self,
            "BcSourceDocsGoldFunction",
            function_name=f"{company.lower()}-{environment}-bc-source-docs-gold",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.dna.source_docs.handlers.gold.lambda_handler",
            timeout=Duration.minutes(5),
            memory_size=512,
            description=(
                f"Merge global + client source-docs overlays into "
                f"governance/source_semantic_reference/{connector}/gold/"
            ),
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment={
                "HIVEFLOW_COMPANY": company,
                "HIVEFLOW_ENVIRONMENT": environment,
                "HIVEFLOW_S3_BUCKET": data_bucket.bucket_name,
                "HIVEFLOW_SOURCE_DOCS_BUCKET": source_docs_bucket_name,
            },
        )
        data_bucket.grant_read_write(gold_fn)
        docs_bucket = s3.Bucket.from_bucket_name(
            self,
            "SourceDocumentationBucket",
            SOURCE_DOCUMENTATION_BUCKET_NAME,
        )
        docs_bucket.grant_read(gold_fn)
        gold_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
            )
        )
        gold_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[f"{docs_bucket.bucket_arn}/{connector}/*"],
            )
        )
        return gold_fn

    def _grant_bedrock_semantic_access(self, fn: _lambda.Function) -> None:
        grant_bedrock_semantic_access(fn)

