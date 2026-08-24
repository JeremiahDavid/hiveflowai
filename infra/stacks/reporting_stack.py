from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, CustomResource, Duration, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import custom_resources as cr
from constructs import Construct

from lambda_bundle import HiveFlowLambdaRuntime, UI_BUNDLE_REVISION, hiveflow_lambda_runtime


def _dna_refresh_state_machine_names(company: str, environment: str) -> list[str]:
    """Current process_config names plus legacy Step Functions names."""
    from hiveflow.process_config import Process, step_function_name_for_process

    current = step_function_name_for_process(
        company, environment, "all", Process.DNA_REFRESH
    )
    spreadsheet = step_function_name_for_process(
        company, environment, "all", Process.SPREADSHEET_ANALYZE
    )
    prefix = f"{company.strip().lower()}-{environment.strip().lower()}"
    legacy = f"{prefix}-all-gold-dna-refresh"
    names: list[str] = []
    for name in (current, spreadsheet, legacy):
        if name and name not in names:
            names.append(name)
    return names
    return names


class ReportingStack(Stack):
    """Per-portal-client reporting UI — charts, KPIs, and dashboard views over gold DNA outputs."""

    web_api: apigateway.RestApi

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        client_id: str,
        company: str,
        environment: str,
        data_bucket_name: str,
        source: str,
        client_config: dict[str, Any],
        dna_config: dict[str, Any],
        portal_user_pool: cognito.IUserPool,
        portal_user_pool_client: cognito.IUserPoolClient,
        portal_session_secret: secretsmanager.ISecret,
        domain_config: dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not data_bucket_name.strip():
            raise ValueError("data_bucket_name is required for ReportingStack")
        if not source.strip():
            raise ValueError("source connector is required for ReportingStack (typically dbc)")

        self._apply_cost_allocation_tags(client_id, company, environment)

        data_bucket = s3.Bucket.from_bucket_name(
            self,
            "ImportedDataBucket",
            data_bucket_name,
        )

        from hiveflow.storage.paths import company_dna_config_id

        pack_id = company_dna_config_id(company)

        lambda_runtime = hiveflow_lambda_runtime(self, profile="reporting")
        reporting_fn = self._create_reporting_lambda(
            data_bucket=data_bucket,
            lambda_runtime=lambda_runtime,
            client_id=client_id,
            company=company,
            environment=environment,
            source=source,
            pack_id=pack_id,
            client_config=client_config,
            portal_user_pool=portal_user_pool,
            portal_user_pool_client=portal_user_pool_client,
            portal_session_secret=portal_session_secret,
            domain_config=domain_config,
        )
        self._seed_reporting_config_on_deploy(
            reporting_fn=reporting_fn,
            company=company,
            environment=environment,
            pack_id=pack_id,
            client_id=client_id,
        )

        self.web_api = apigateway.RestApi(
            self,
            "ReportingWebApi",
            rest_api_name=f"hiveflow-reporting-{client_id}-{environment}".lower(),
            description=f"Client reporting UI for portal client {client_id} ({environment})",
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
            ),
            endpoint_configuration=apigateway.EndpointConfiguration(
                types=[apigateway.EndpointType.REGIONAL]
            ),
            binary_media_types=["*/*"],
        )

        ui_integration = apigateway.LambdaIntegration(
            reporting_fn,
            proxy=True,
            allow_test_invoke=False,
        )
        self.web_api.root.add_method("ANY", ui_integration)
        self.web_api.root.add_proxy(default_integration=ui_integration, any_method=True)

        from hiveflow.project_config import resolve_reporting_site_url

        reporting_url = resolve_reporting_site_url(
            domain_config,
            client_config,
            client_id,
            fallback=self.web_api.url,
        )

        CfnOutput(self, "PortalClientId", value=client_id)
        CfnOutput(self, "ReportingCompany", value=company)
        CfnOutput(self, "DataBucketName", value=data_bucket_name)
        CfnOutput(self, "ReportingFunctionName", value=reporting_fn.function_name)
        CfnOutput(self, "ReportingWebUrl", value=reporting_url)
        CfnOutput(self, "ApiGatewayUrl", value=self.web_api.url)
        # Logical id stays WebApiIdV2 — see global_ui_stack.py for why.
        reporting_slug = client_id.strip().lower().replace("_", "-")
        CfnOutput(
            self,
            "WebApiIdV2",
            value=self.web_api.rest_api_id,
            export_name=f"hiveflow-reporting-{reporting_slug}-{environment}-web-api-id",
        )

    def _apply_cost_allocation_tags(self, client_id: str, company: str, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags(company, environment).items():
            Tags.of(self).add(key, value)
        Tags.of(self).add("PortalClientId", client_id.strip().lower())

    def _seed_reporting_config_on_deploy(
        self,
        *,
        reporting_fn: _lambda.Function,
        company: str,
        environment: str,
        pack_id: str,
        client_id: str,
    ) -> None:
        """Seed ``{company}_reporting_config.yaml`` on deploy when missing."""
        provider = cr.Provider(
            self,
            "ReportingConfigInitProvider",
            on_event_handler=reporting_fn,
        )
        seed = CustomResource(
            self,
            "InitReportingConfigV1",
            service_token=provider.service_token,
            properties={
                "pack_id": pack_id,
                "seed_revision": "20260804-reporting-config-driven",
                "company": company,
                "environment": environment,
                "client_id": client_id.strip().lower(),
            },
        )
        seed.node.add_dependency(reporting_fn)
        CfnOutput(
            self,
            "ReportingConfigInitResource",
            value=f"{client_id.strip().lower()}-{environment}-reporting-config-init-v1",
            description="Custom resource that seeds reporting config on ReportingStack deploy",
        )

    def _create_reporting_lambda(
        self,
        *,
        data_bucket: s3.IBucket,
        lambda_runtime: HiveFlowLambdaRuntime,
        client_id: str,
        company: str,
        environment: str,
        source: str,
        pack_id: str,
        client_config: dict[str, Any],
        portal_user_pool: cognito.IUserPool,
        portal_user_pool_client: cognito.IUserPoolClient,
        portal_session_secret: secretsmanager.ISecret,
        domain_config: dict[str, Any],
    ) -> _lambda.Function:
        zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
        primary_hostname = str(domain_config.get("primary_hostname", zone_name)).strip().lower().rstrip(".")
        global_login_url = f"https://{primary_hostname}/portal/login" if primary_hostname else ""

        assistant_cfg = client_config.get("config_assistant", {})
        if not isinstance(assistant_cfg, dict):
            assistant_cfg = {}
        budget_raw = assistant_cfg.get("monthly_budget_usd", 10)
        try:
            assistant_budget = float(budget_raw)
        except (TypeError, ValueError):
            assistant_budget = 10.0
        if assistant_budget <= 0:
            assistant_budget = 10.0

        refresh_cfg = client_config.get("dna_manual_refresh", {})
        if not isinstance(refresh_cfg, dict):
            refresh_cfg = {}
        refresh_limit_raw = refresh_cfg.get("monthly_limit", 10)
        try:
            refresh_monthly_limit = int(refresh_limit_raw)
        except (TypeError, ValueError):
            refresh_monthly_limit = 10
        if refresh_monthly_limit <= 0:
            refresh_monthly_limit = 10

        environment_vars = {
            "HIVEFLOW_UI_MODE": "reporting",
            "HIVEFLOW_COMPANY": company,
            "HIVEFLOW_ENVIRONMENT": environment,
            "HIVEFLOW_S3_BUCKET": data_bucket.bucket_name,
            "HIVEFLOW_DNA_SOURCE": source,
            "HIVEFLOW_DNA_PACK_ID": pack_id,
            "HIVEFLOW_PORTAL_CLIENT_ID": client_id.strip().lower(),
            "HIVEFLOW_CONFIG_ASSISTANT_MONTHLY_BUDGET_USD": str(assistant_budget),
            "HIVEFLOW_DNA_MANUAL_REFRESH_MONTHLY_LIMIT": str(refresh_monthly_limit),
            # Haiku 4.5 inference profile — cheaper than Sonnet; Sonnet 4 is legacy.
            "HIVEFLOW_BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "HIVEFLOW_SOURCE_DOCS_GOLD_FUNCTION": (
                f"{company.strip().lower()}-{environment}-bc-source-docs-gold"
            ),
            "HIVEFLOW_PORTAL_COOKIE_SECURE": "true",
            "HIVEFLOW_COGNITO_USER_POOL_ID": portal_user_pool.user_pool_id,
            "HIVEFLOW_COGNITO_CLIENT_ID": portal_user_pool_client.user_pool_client_id,
            "HIVEFLOW_PORTAL_SESSION_SECRET_ARN": portal_session_secret.secret_arn,
            "HIVEFLOW_ADMIN_USERNAME": "GlobalAdmin",
        }
        if global_login_url:
            environment_vars["HIVEFLOW_GLOBAL_LOGIN_URL"] = global_login_url
        if primary_hostname:
            environment_vars["HIVEFLOW_PRIMARY_SITE_URL"] = f"https://{primary_hostname}"
        if zone_name:
            environment_vars["HIVEFLOW_PORTAL_COOKIE_DOMAIN"] = f".{zone_name}"

        reporting_fn = _lambda.Function(
            self,
            "ReportingUiFunction",
            function_name=f"{client_id.strip().lower()}-{environment}-reporting-ui-serve",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.dna.web.lambda_handler.ui_handler",
            timeout=Duration.seconds(120),
            memory_size=1024,
            description=(
                f"Client reporting UI for portal client {client_id}: "
                f"charts, KPIs, and dashboards for {company}/{environment} "
                f"(bundle {UI_BUNDLE_REVISION})"
            ),
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment=environment_vars,
        )

        # Read gold + write governance reporting sidecar on deploy seed.
        data_bucket.grant_read_write(reporting_fn)
        portal_session_secret.grant_read(reporting_fn)
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminUpdateUserAttributes",
                    "cognito-idp:ListUsers",
                ],
                resources=[portal_user_pool.user_pool_arn],
            )
        )
        # Config Assistant — Bedrock Converse + Marketplace subscribe (first Claude invoke).
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                    "aws-marketplace:ViewSubscriptions",
                    "aws-marketplace:Unsubscribe",
                    "aws-marketplace:Subscribe",
                ],
                resources=["*"],
            )
        )
        # KPI Generator — Athena validation / preview (does not regenerate SQL on refresh).
        from hiveflow.project_config import resolve_athena_results_bucket_name

        results_bucket = resolve_athena_results_bucket_name(
            company,
            environment,
            account=Stack.of(self).account,
            region=Stack.of(self).region,
        )
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:StopQueryExecution",
                    "athena:GetWorkGroup",
                ],
                resources=["*"],
            )
        )
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartition",
                    "glue:GetPartitions",
                ],
                resources=[
                    f"arn:aws:glue:{Stack.of(self).region}:{Stack.of(self).account}:catalog",
                    f"arn:aws:glue:{Stack.of(self).region}:{Stack.of(self).account}:database/*",
                    f"arn:aws:glue:{Stack.of(self).region}:{Stack.of(self).account}:table/*/*",
                ],
            )
        )
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetBucketLocation",
                    "s3:GetObject",
                    "s3:ListBucket",
                    "s3:PutObject",
                    "s3:DeleteObject",
                ],
                resources=[
                    f"arn:aws:s3:::{results_bucket}",
                    f"arn:aws:s3:::{results_bucket}/*",
                ],
            )
        )
        # Async Config Assistant chat self-invoke (avoids API Gateway 29s limit).
        # Lambda ARNs use function:name (colon). format_arn defaults to slash and
        # silently fails authorization. Avoid grant_invoke(self) — it cycles CFN.
        fn_name = f"{client_id.strip().lower()}-{environment}-reporting-ui-serve"
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{Stack.of(self).region}:{Stack.of(self).account}:function:{fn_name}",
                    f"arn:aws:lambda:{Stack.of(self).region}:{Stack.of(self).account}:function:{fn_name}:*",
                ],
            )
        )
        gold_fn_name = f"{company.strip().lower()}-{environment}-bc-source-docs-gold"
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    (
                        f"arn:aws:lambda:{Stack.of(self).region}:{Stack.of(self).account}"
                        f":function:{gold_fn_name}"
                    ),
                    (
                        f"arn:aws:lambda:{Stack.of(self).region}:{Stack.of(self).account}"
                        f":function:{gold_fn_name}:*"
                    ),
                ],
            )
        )
        dna_refresh_state_machines = _dna_refresh_state_machine_names(
            company, environment
        )
        region = Stack.of(self).region
        account = Stack.of(self).account
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[
                    f"arn:aws:states:{region}:{account}:stateMachine:{name}"
                    for name in dna_refresh_state_machines
                ],
            )
        )
        reporting_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution"],
                resources=[
                    f"arn:aws:states:{region}:{account}:execution:{name}:*"
                    for name in dna_refresh_state_machines
                ],
            )
        )
        return reporting_fn
