"""Spreadsheet Lab — a standalone sandbox stack for the simplified Spreadsheet
Engine rebuild (see docs/spreadsheet-lab.md).

Deliberately independent of every other platform stack: single Lambda, single
REST API, its own subdomain wired directly here (not through
``GlobalDnsStack``), single-tenant (``poc`` only, via static env vars — no
Cognito, no per-request tenant resolution). This lets it deploy and tear down
on its own for fast iteration while the design is being validated, exactly as
the user asked for a "separate parallel process."
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from constructs import Construct

from iam_grants import grant_bedrock_semantic_access
from lambda_bundle import hiveflow_lambda_runtime
from ui_domain import attach_admin_subdomain, import_hosted_zone


class SpreadsheetLabStack(Stack):
    """Sandbox site for the Spreadsheet Lab rebuild — see the module docstring."""

    web_api: apigateway.RestApi

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        lab_config: dict[str, Any],
        data_bucket_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env = environment.strip().lower()
        self._apply_cost_allocation_tags(env)

        data_bucket = s3.Bucket.from_bucket_name(self, "PocDataBucket", data_bucket_name)

        lambda_runtime = hiveflow_lambda_runtime(self, profile="spreadsheet_lab")
        ui_fn = self._create_lab_lambda(
            lambda_runtime=lambda_runtime,
            environment=env,
            data_bucket=data_bucket,
            lab_config=lab_config,
        )

        self.web_api = apigateway.RestApi(
            self,
            "SpreadsheetLabWebApi",
            rest_api_name=f"hiveflow-spreadsheet-lab-{env}".lower(),
            description=f"Spreadsheet Lab sandbox UI for {env}",
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
        integration = apigateway.LambdaIntegration(ui_fn, proxy=True, allow_test_invoke=False)
        self.web_api.root.add_method("ANY", integration)
        self.web_api.root.add_proxy(default_integration=integration, any_method=True)

        hostname = str(lab_config.get("hostname", "spreadsheet-engine")).strip().lower() or "spreadsheet-engine"
        domain_cfg = ui_config.get("domain", {}) if isinstance(ui_config.get("domain"), dict) else {}
        zone_name = str(domain_cfg.get("zone_name", "")).strip().lower().rstrip(".")
        full_hostname = f"{hostname}.{zone_name}" if zone_name else hostname

        # Wired directly here rather than through GlobalDnsStack — this stack
        # deploys/destroys independently of every other platform stack, which
        # is the point of a fast-iterating sandbox. See the module docstring.
        hosted_zone = import_hosted_zone(self, domain_cfg)
        if hosted_zone is not None and zone_name:
            attach_admin_subdomain(
                self,
                rest_api_id=self.web_api.rest_api_id,
                hosted_zone=hosted_zone,
                zone_name=zone_name,
                admin_hostname=hostname,
            )

        CfnOutput(self, "SpreadsheetLabFunctionName", value=ui_fn.function_name)
        CfnOutput(self, "SpreadsheetLabSiteUrl", value=f"https://{full_hostname}/")
        CfnOutput(self, "ApiGatewayUrl", value=self.web_api.url)
        CfnOutput(
            self,
            "WebApiIdV2",
            value=self.web_api.rest_api_id,
            export_name=self._web_api_export_name(env),
        )

    @staticmethod
    def _web_api_export_name(environment: str) -> str:
        from hiveflow.project_config import spreadsheet_lab_web_api_export_name

        return spreadsheet_lab_web_api_export_name(environment)

    def _apply_cost_allocation_tags(self, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags("SPREADSHEET-LAB", environment).items():
            Tags.of(self).add(key, value)
        Tags.of(self).add("hiveflow:component", "spreadsheet-lab")

    def _create_lab_lambda(
        self,
        *,
        lambda_runtime: Any,
        environment: str,
        data_bucket: s3.IBucket,
        lab_config: dict[str, Any],
    ) -> _lambda.Function:
        environment_vars = {
            "HIVEFLOW_S3_BUCKET": data_bucket.bucket_name,
            "HIVEFLOW_COMPANY": "poc",
            "HIVEFLOW_ENVIRONMENT": environment,
            "HIVEFLOW_BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        }
        auth_secret_arn = str(lab_config.get("basic_auth_secret_arn", "")).strip()
        if auth_secret_arn:
            environment_vars["HIVEFLOW_SPREADSHEET_LAB_AUTH_SECRET_ARN"] = auth_secret_arn

        ui_fn = _lambda.Function(
            self,
            "SpreadsheetLabFunction",
            function_name=f"spreadsheet-lab-{environment}-serve",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.spreadsheet_lab.web.lambda_handler.handler",
            # Room for the async self-invoke table-task path (see worker.py) —
            # not just the HTTP request/response, which API Gateway itself caps
            # at ~29s regardless of this value.
            timeout=Duration.minutes(5),
            memory_size=1024,
            description=f"Spreadsheet Lab sandbox UI + table-task worker for {environment}",
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment=environment_vars,
        )

        # Scoped to exactly the two Spreadsheet Lab prefixes — this Lambda must
        # never be able to read or overwrite the production Spreadsheet Engine's
        # data in the same bucket (governance/spreadsheet_engine/*, silver/reference/*).
        data_bucket.grant_read_write(ui_fn, "governance/spreadsheet_lab/*")
        data_bucket.grant_read_write(ui_fn, "silver/reference_lab/*")

        if auth_secret_arn:
            ui_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[auth_secret_arn],
                )
            )

        grant_bedrock_semantic_access(ui_fn)

        # Self-invoke for the async table-task worker path (worker._invoke_self_async).
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[ui_fn.function_arn],
            )
        )
        return ui_fn
