from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_codedeploy as codedeploy
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from lambda_bundle import UI_BUNDLE_REVISION, hiveflow_lambda_runtime

# Must match hiveflow.dna.web.portal.tenant_credentials._tenant_role_arn and the
# trust condition in DnaStack._create_portal_tenant_role.
SERVE_ROLE_NAME_TEMPLATE = "hiveflow-portal-{environment}-serve-role"


class PortalStack(Stack):
    """The single multi-tenant client reporting portal.

    Replaces the per-client ``ReportingStack``. One Lambda + one API Gateway serve
    every client; the tenant is resolved per request from the Cognito ``client_id``
    claim. This Lambda's own role can touch nothing tenant-scoped — it assumes
    ``hiveflow-portal-tenant-{company}-{env}`` (minted by each company's DnaStack)
    per request for all S3 / Athena / Glue / Step Functions access.
    """

    web_api: apigateway.RestApi

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        portal_user_pool: cognito.IUserPool,
        portal_user_pool_client: cognito.IUserPoolClient,
        portal_session_secret: secretsmanager.ISecret,
        domain_config: dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._apply_cost_allocation_tags(environment)

        env_slug = environment.strip().lower()
        zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
        primary_hostname = (
            str(domain_config.get("primary_hostname", zone_name)).strip().lower().rstrip(".")
        )

        serve_role = iam.Role(
            self,
            "PortalServeRole",
            role_name=SERVE_ROLE_NAME_TEMPLATE.format(environment=env_slug),
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            description=f"Shared multi-tenant portal Lambda role ({env_slug})",
        )

        config_bucket = f"meshflow-platform-config-{env_slug}-{self.account}-{self.region}"
        reporting_fn = self._create_portal_lambda(
            environment=environment,
            serve_role=serve_role,
            portal_user_pool=portal_user_pool,
            portal_user_pool_client=portal_user_pool_client,
            portal_session_secret=portal_session_secret,
            config_bucket=config_bucket,
            zone_name=zone_name,
            primary_hostname=primary_hostname,
        )

        self._grant_serve_role(
            serve_role,
            portal_user_pool=portal_user_pool,
            portal_session_secret=portal_session_secret,
            config_bucket=config_bucket,
            environment=env_slug,
            # Literal name (not reporting_fn.function_name) — referencing the
            # function here would cycle role-policy <-> function.
            function_name=f"portal-{env_slug}-reporting-ui-serve",
        )

        alias = self._canary_alias(reporting_fn)

        self.web_api = apigateway.RestApi(
            self,
            "PortalWebApi",
            rest_api_name=f"hiveflow-portal-{env_slug}",
            description=f"Multi-tenant client reporting portal ({env_slug})",
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
        integration = apigateway.LambdaIntegration(alias, proxy=True, allow_test_invoke=False)
        self.web_api.root.add_method("ANY", integration)
        self.web_api.root.add_proxy(default_integration=integration, any_method=True)

        CfnOutput(self, "PortalFunctionName", value=reporting_fn.function_name)
        CfnOutput(self, "ApiGatewayUrl", value=self.web_api.url)
        CfnOutput(
            self,
            "WebApiIdV2",
            value=self.web_api.rest_api_id,
            export_name=f"hiveflow-portal-{env_slug}-web-api-id",
        )
        if primary_hostname:
            CfnOutput(self, "SiteUrl", value=f"https://{primary_hostname}/portal/login")

    def _apply_cost_allocation_tags(self, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags("PLATFORM", environment).items():
            Tags.of(self).add(key, value)
        Tags.of(self).add("Component", "portal")

    def _create_portal_lambda(
        self,
        *,
        environment: str,
        serve_role: iam.Role,
        portal_user_pool: cognito.IUserPool,
        portal_user_pool_client: cognito.IUserPoolClient,
        portal_session_secret: secretsmanager.ISecret,
        config_bucket: str,
        zone_name: str,
        primary_hostname: str,
    ) -> _lambda.Function:
        lambda_runtime = hiveflow_lambda_runtime(self, profile="reporting")
        env_slug = environment.strip().lower()

        environment_vars = {
            "HIVEFLOW_UI_MODE": "reporting_multitenant",
            "HIVEFLOW_PLATFORM_UI": "true",
            "HIVEFLOW_ENVIRONMENT": environment,
            "HIVEFLOW_CONFIG_S3_URI": f"s3://{config_bucket}/config.yaml",
            "HIVEFLOW_TENANT_ASSUME_ROLE": "1",
            "HIVEFLOW_PORTAL_COOKIE_SECURE": "true",
            "HIVEFLOW_COGNITO_USER_POOL_ID": portal_user_pool.user_pool_id,
            "HIVEFLOW_COGNITO_CLIENT_ID": portal_user_pool_client.user_pool_client_id,
            "HIVEFLOW_PORTAL_SESSION_SECRET_ARN": portal_session_secret.secret_arn,
            "HIVEFLOW_ADMIN_USERNAME": "GlobalAdmin",
            # Haiku 4.5 inference profile — Bedrock stays on the shared role, not
            # the per-tenant one (it is not tenant data).
            "HIVEFLOW_BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        }
        if zone_name:
            environment_vars["HIVEFLOW_PORTAL_COOKIE_DOMAIN"] = f".{zone_name}"
        if primary_hostname:
            environment_vars["HIVEFLOW_PRIMARY_SITE_URL"] = f"https://{primary_hostname}"
            environment_vars["HIVEFLOW_GLOBAL_LOGIN_URL"] = (
                f"https://{primary_hostname}/portal/login"
            )

        return _lambda.Function(
            self,
            "PortalUiFunction",
            function_name=f"portal-{env_slug}-reporting-ui-serve",
            role=serve_role,
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.dna.web.lambda_handler.ui_handler",
            timeout=Duration.seconds(120),
            memory_size=1024,
            description=(
                f"Multi-tenant client reporting portal ({env_slug}) — charts, KPIs, "
                f"dashboards for every client (bundle {UI_BUNDLE_REVISION})"
            ),
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment=environment_vars,
        )

    def _canary_alias(self, fn: _lambda.Function) -> _lambda.Alias:
        alias = _lambda.Alias(
            self,
            "PortalUiAliasLive",
            alias_name="live",
            version=fn.current_version,
        )
        errors_alarm = cloudwatch.Alarm(
            self,
            "PortalUiErrorsAlarm",
            metric=alias.metric_errors(period=Duration.minutes(1)),
            threshold=5,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        codedeploy.LambdaDeploymentGroup(
            self,
            "PortalUiDeployGroup",
            alias=alias,
            deployment_config=codedeploy.LambdaDeploymentConfig.CANARY_10_PERCENT_5_MINUTES,
            alarms=[errors_alarm],
        )
        return alias

    def _grant_serve_role(
        self,
        serve_role: iam.Role,
        *,
        portal_user_pool: cognito.IUserPool,
        portal_session_secret: secretsmanager.ISecret,
        config_bucket: str,
        environment: str,
        function_name: str,
    ) -> None:
        # The ONLY route to tenant data: assume a per-company tenant role.
        serve_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/hiveflow-portal-tenant-*-{environment}"
                ],
            )
        )
        portal_session_secret.grant_read(serve_role)
        serve_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::{config_bucket}/config.yaml"],
            )
        )
        serve_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminRespondToAuthChallenge",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminUpdateUserAttributes",
                    "cognito-idp:ListUsers",
                ],
                resources=[portal_user_pool.user_pool_arn],
            )
        )
        serve_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:ForgotPassword", "cognito-idp:ConfirmForgotPassword"],
                resources=["*"],
            )
        )
        # Config Assistant / KPI Generator drafting — Bedrock is shared, not tenant data.
        serve_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                    "aws-marketplace:ViewSubscriptions",
                    "aws-marketplace:Subscribe",
                    "aws-marketplace:Unsubscribe",
                ],
                resources=["*"],
            )
        )
        # Async Config Assistant / KPI Generator self-invoke.
        serve_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{function_name}",
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{function_name}:*",
                ],
            )
        )
