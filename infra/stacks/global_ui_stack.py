from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from lambda_bundle import HiveFlowLambdaRuntime, UI_BUNDLE_REVISION, hiveflow_lambda_runtime
from portal_email import configure_portal_user_pool_email, resolve_portal_email_settings


class GlobalUiStack(Stack):
    """Global HiveFlowAI site — public pages, portal auth (Cognito), and admin."""

    portal_user_pool: cognito.UserPool
    portal_user_pool_client: cognito.UserPoolClient
    portal_session_secret: secretsmanager.Secret
    web_api: apigateway.RestApi

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        client_buckets: dict[str, str],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._apply_cost_allocation_tags(environment)

        portal_resources = self._create_portal_user_pool(environment=environment, ui_config=ui_config)
        self.portal_user_pool = portal_resources["user_pool"]
        self.portal_user_pool_client = portal_resources["user_pool_client"]
        self.portal_session_secret = portal_resources["session_secret"]

        domain_cfg = ui_config.get("domain", {})
        if not isinstance(domain_cfg, dict):
            domain_cfg = {}
        zone_name = str(domain_cfg.get("zone_name", "")).strip().lower().rstrip(".")
        primary_hostname = str(domain_cfg.get("primary_hostname", zone_name)).strip().lower().rstrip(".")

        lambda_runtime = hiveflow_lambda_runtime(self, profile="ui")
        ui_fn = self._create_ui_lambda(
            lambda_runtime=lambda_runtime,
            environment=environment,
            portal_resources=portal_resources,
            cookie_domain=f".{zone_name}" if zone_name else "",
            primary_hostname=primary_hostname,
        )

        self.web_api = apigateway.RestApi(
            self,
            "HiveFlowWebApi",
            rest_api_name=f"hiveflow-global-ui-{environment}".lower(),
            description=f"Global HiveFlowAI UI for {environment}",
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
            ui_fn,
            proxy=True,
            allow_test_invoke=False,
        )
        self.web_api.root.add_method("ANY", ui_integration)
        self.web_api.root.add_proxy(default_integration=ui_integration, any_method=True)

        from hiveflow.project_config import resolve_ui_primary_site_url

        platform_env_config = {"ui": ui_config}
        site_url = resolve_ui_primary_site_url(platform_env_config, fallback=self.web_api.url)

        CfnOutput(self, "UiFunctionName", value=ui_fn.function_name)
        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "ApiGatewayUrl", value=self.web_api.url)
        # Logical id stays WebApiIdV2 (not WebApiId) — renaming it back would make
        # CFN delete-then-recreate the hiveflow-* export while GlobalDnsStack-dev
        # still imports it by name, hitting the same in-use block this replaced.
        CfnOutput(
            self,
            "WebApiIdV2",
            value=self.web_api.rest_api_id,
            export_name=f"hiveflow-global-ui-{environment}-web-api-id",
        )
        CfnOutput(self, "PortalUserPoolId", value=self.portal_user_pool.user_pool_id)
        CfnOutput(self, "PortalUserPoolClientId", value=self.portal_user_pool_client.user_pool_client_id)

        # Published under a pinned SSM parameter name (not just a CfnOutput,
        # which only a same-deploy Fn::ImportValue could read — exactly the
        # hard cross-stack dependency GlobalDnaEngineStack needs to avoid, the
        # same reasoning portal_session_secret_name's Secrets Manager entry
        # already gets it for GlobalAgentPipelinesStack). See
        # hiveflow.project_config.portal_user_pool_id_parameter_name.
        from hiveflow.project_config import (
            portal_user_pool_client_id_parameter_name,
            portal_user_pool_id_parameter_name,
        )

        ssm.StringParameter(
            self,
            "PortalUserPoolIdParameter",
            parameter_name=portal_user_pool_id_parameter_name(environment),
            string_value=self.portal_user_pool.user_pool_id,
        )
        ssm.StringParameter(
            self,
            "PortalUserPoolClientIdParameter",
            parameter_name=portal_user_pool_client_id_parameter_name(environment),
            string_value=self.portal_user_pool_client.user_pool_client_id,
        )
        portal_email = resolve_portal_email_settings(ui_config)
        if portal_email is not None:
            CfnOutput(self, "PortalEmailFromAddress", value=portal_email["from_address"])
        if client_buckets:
            CfnOutput(self, "PortalClientBuckets", value=",".join(sorted(set(client_buckets.values()))))

    def _apply_cost_allocation_tags(self, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags("PLATFORM", environment).items():
            Tags.of(self).add(key, value)

    def _create_portal_user_pool(
        self,
        *,
        environment: str,
        ui_config: dict[str, Any],
    ) -> dict[str, Any]:
        portal_cfg = ui_config.get("portal", {})
        if not isinstance(portal_cfg, dict):
            portal_cfg = {}

        default_client_id = str(portal_cfg.get("default_client_id", "default")).strip().lower() or "default"
        # Deliberately NOT renamed to hiveflow- during the 2026-08 meshflow->hiveflow
        # rebrand: UserPoolName forces CloudFormation replacement, which would destroy
        # this pool and every existing portal user. Pin to the already-deployed name.
        pool_name = f"meshflow-portal-{environment}".lower()
        portal_login_url = "https://hive-flow-ai.com/portal/login"
        domain_cfg = ui_config.get("domain", {})
        if isinstance(domain_cfg, dict):
            primary_hostname = str(domain_cfg.get("primary_hostname", "")).strip()
            if primary_hostname:
                portal_login_url = f"https://{primary_hostname}/portal/login"

        pool_email = configure_portal_user_pool_email(
            self,
            id_prefix="Global",
            ui_config=ui_config,
            region=Stack.of(self).region,
        )
        user_pool_kwargs: dict[str, Any] = {
            "user_pool_name": pool_name,
            "sign_in_aliases": cognito.SignInAliases(username=True, email=True),
            "auto_verify": cognito.AutoVerifiedAttrs(email=True),
            "standard_attributes": cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=False, mutable=True),
            ),
            "custom_attributes": {
                "client_id": cognito.StringAttribute(min_len=1, max_len=64, mutable=True),
                "portal_role": cognito.StringAttribute(min_len=1, max_len=16, mutable=True),
            },
            "password_policy": cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            "account_recovery": cognito.AccountRecovery.EMAIL_ONLY,
            "user_invitation": cognito.UserInvitationConfig(
                email_subject="Your HiveFlowAI portal account",
                email_body=(
                    "You have been invited to the HiveFlowAI client portal.\n\n"
                    "Username: {username}\n"
                    "Temporary password: {####}\n\n"
                    f"Sign in at {portal_login_url} and set a new password when prompted."
                ),
            ),
            "removal_policy": RemovalPolicy.RETAIN,
        }
        if pool_email is not None:
            user_pool_kwargs["email"] = pool_email

        user_pool = cognito.UserPool(
            self,
            "PortalUserPool",
            **user_pool_kwargs,
        )

        user_pool_client = user_pool.add_client(
            "PortalUserPoolClient",
            user_pool_client_name=f"{pool_name}-web",
            auth_flows=cognito.AuthFlow(
                admin_user_password=True,
                user_password=True,
            ),
            generate_secret=False,
        )

        from hiveflow.project_config import portal_session_secret_name

        session_secret = secretsmanager.Secret(
            self,
            "PortalSessionSecret",
            # Pinned to the pre-rebrand name — Secret.Name forces CFN replacement,
            # which would orphan the already-deployed secret. See pool_name above.
            # Name shared with GlobalAgentPipelinesStack, which imports this same
            # secret by name rather than depending on this stack being constructed.
            secret_name=portal_session_secret_name(environment),
            description=f"HiveFlowAI global portal session signing secret for {environment}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=48,
                exclude_punctuation=True,
            ),
        )

        return {
            "user_pool": user_pool,
            "user_pool_client": user_pool_client,
            "session_secret": session_secret,
            "default_client_id": default_client_id,
        }

    def _create_ui_lambda(
        self,
        *,
        lambda_runtime: HiveFlowLambdaRuntime,
        environment: str,
        portal_resources: dict[str, Any],
        cookie_domain: str,
        primary_hostname: str,
    ) -> _lambda.Function:
        user_pool: cognito.UserPool = portal_resources["user_pool"]
        user_pool_client: cognito.UserPoolClient = portal_resources["user_pool_client"]
        session_secret: secretsmanager.Secret = portal_resources["session_secret"]
        default_client_id: str = portal_resources["default_client_id"]

        environment_vars = {
            "HIVEFLOW_UI_MODE": "global",
            "HIVEFLOW_PLATFORM_UI": "true",
            "HIVEFLOW_ENVIRONMENT": environment,
            "HIVEFLOW_PORTAL_COOKIE_SECURE": "true",
            "HIVEFLOW_COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "HIVEFLOW_COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
            "HIVEFLOW_PORTAL_DEFAULT_CLIENT_ID": default_client_id,
            "HIVEFLOW_PORTAL_SESSION_SECRET_ARN": session_secret.secret_arn,
            "HIVEFLOW_ADMIN_USERNAME": "GlobalAdmin",
        }
        if cookie_domain:
            environment_vars["HIVEFLOW_PORTAL_COOKIE_DOMAIN"] = cookie_domain
        if primary_hostname:
            environment_vars["HIVEFLOW_PRIMARY_SITE_URL"] = f"https://{primary_hostname}"

        ui_fn = _lambda.Function(
            self,
            "GlobalUiServeFunction",
            function_name=f"platform-{environment}-global-ui-serve",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.dna.web.lambda_handler.ui_handler",
            timeout=Duration.seconds(30),
            memory_size=512,
            description=f"Global HiveFlowAI site for {environment} — public pages, login, and admin (bundle {UI_BUNDLE_REVISION})",
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment=environment_vars,
        )

        session_secret.grant_read(ui_fn)
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminRespondToAuthChallenge",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:ListUsers",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )
        # ForgotPassword / ConfirmForgotPassword are client APIs without resource-level IAM.
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:ForgotPassword",
                    "cognito-idp:ConfirmForgotPassword",
                ],
                resources=["*"],
            )
        )
        return ui_fn
