from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from lambda_bundle import HiveFlowLambdaRuntime, UI_BUNDLE_REVISION, hiveflow_lambda_runtime
from portal_email import configure_portal_user_pool_email, resolve_portal_email_settings


class PlatformAdminStack(Stack):
    """Platform admin site — separate Cognito pool, job runners for source-docs and future sources."""

    admin_user_pool: cognito.UserPool
    admin_user_pool_client: cognito.UserPoolClient
    admin_session_secret: secretsmanager.Secret
    config_bucket: s3.Bucket
    web_api: apigateway.RestApi

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env = environment.strip().lower()
        self._apply_cost_allocation_tags(env)

        domain_cfg = ui_config.get("domain", {})
        if not isinstance(domain_cfg, dict):
            domain_cfg = {}
        zone_name = str(domain_cfg.get("zone_name", "")).strip().lower().rstrip(".")
        admin_host_raw = str(domain_cfg.get("admin_hostname", "admin")).strip().lower().rstrip(".")
        if admin_host_raw.endswith(zone_name) if zone_name else False:
            admin_hostname = admin_host_raw
        elif zone_name:
            admin_hostname = f"{admin_host_raw}.{zone_name}"
        else:
            admin_hostname = admin_host_raw or "admin"

        portal_resources = self._create_admin_user_pool(
            environment=env,
            ui_config=ui_config,
            admin_hostname=admin_hostname,
        )
        self.admin_user_pool = portal_resources["user_pool"]
        self.admin_user_pool_client = portal_resources["user_pool_client"]
        self.admin_session_secret = portal_resources["session_secret"]

        account = Stack.of(self).account
        region = Stack.of(self).region
        self.config_bucket = s3.Bucket(
            self,
            "PlatformConfigBucket",
            # Pinned to the pre-rebrand name — BucketName forces CFN replacement,
            # which would orphan the already-deployed bucket and its contents.
            bucket_name=f"meshflow-platform-config-{env}-{account}-{region}".lower(),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )

        lambda_runtime = hiveflow_lambda_runtime(self, profile="ui")
        ui_fn = self._create_admin_ui_lambda(
            lambda_runtime=lambda_runtime,
            environment=env,
            portal_resources=portal_resources,
            admin_hostname=admin_hostname,
            config_bucket=self.config_bucket,
        )

        self.web_api = apigateway.RestApi(
            self,
            "PlatformAdminWebApi",
            rest_api_name=f"hiveflow-platform-admin-{env}".lower(),
            description=f"Platform admin UI for {env}",
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

        CfnOutput(self, "AdminUiFunctionName", value=ui_fn.function_name)
        CfnOutput(self, "AdminSiteUrl", value=f"https://{admin_hostname}/")
        CfnOutput(self, "ApiGatewayUrl", value=self.web_api.url)
        CfnOutput(
            self,
            "WebApiId",
            value=self.web_api.rest_api_id,
            export_name=f"hiveflow-platform-admin-{env}-web-api-id",
        )
        CfnOutput(self, "AdminUserPoolId", value=self.admin_user_pool.user_pool_id)
        CfnOutput(self, "AdminUserPoolClientId", value=self.admin_user_pool_client.user_pool_client_id)
        CfnOutput(self, "PlatformConfigBucketName", value=self.config_bucket.bucket_name)
        CfnOutput(
            self,
            "PlatformConfigS3Uri",
            value=self.config_bucket.s3_url_for_object("config.yaml"),
        )
        portal_email = resolve_portal_email_settings(ui_config)
        if portal_email is not None:
            CfnOutput(self, "AdminEmailFromAddress", value=portal_email["from_address"])

    def _apply_cost_allocation_tags(self, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags("PLATFORM", environment).items():
            Tags.of(self).add(key, value)
        Tags.of(self).add("hiveflow:component", "platform-admin")

    def _create_admin_user_pool(
        self,
        *,
        environment: str,
        ui_config: dict[str, Any],
        admin_hostname: str,
    ) -> dict[str, Any]:
        # Deliberately NOT renamed to hiveflow- during the 2026-08 meshflow->hiveflow
        # rebrand: UserPoolName forces CloudFormation replacement, which would destroy
        # this pool and every existing admin user. Pin to the already-deployed name.
        pool_name = f"meshflow-platform-admin-{environment}".lower()
        admin_login_url = f"https://{admin_hostname}/admin/login"

        pool_email = configure_portal_user_pool_email(
            self,
            id_prefix="PlatformAdmin",
            ui_config=ui_config,
            region=Stack.of(self).region,
            create_identity=False,
        )
        user_pool_kwargs: dict[str, Any] = {
            "user_pool_name": pool_name,
            "sign_in_aliases": cognito.SignInAliases(username=True, email=True),
            "auto_verify": cognito.AutoVerifiedAttrs(email=True),
            "standard_attributes": cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            "password_policy": cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            "account_recovery": cognito.AccountRecovery.EMAIL_ONLY,
            "user_invitation": cognito.UserInvitationConfig(
                email_subject="Your HiveFlowAI platform admin account",
                email_body=(
                    "You have been invited to the HiveFlowAI platform admin site.\n\n"
                    "Username: {username}\n"
                    "Temporary password: {####}\n\n"
                    f"Sign in at {admin_login_url} and set a new password when prompted."
                ),
            ),
            "removal_policy": RemovalPolicy.RETAIN,
        }
        if pool_email is not None:
            user_pool_kwargs["email"] = pool_email

        user_pool = cognito.UserPool(
            self,
            "PlatformAdminUserPool",
            **user_pool_kwargs,
        )

        # Keep in sync with hiveflow.dna.web.cognito_core.ADMIN_GROUP_NAME.
        cognito.CfnUserPoolGroup(
            self,
            "PlatformAdminsGroup",
            user_pool_id=user_pool.user_pool_id,
            group_name="PlatformAdmins",
            description="Members may access the HiveFlowAI platform admin panel.",
        )

        user_pool_client = user_pool.add_client(
            "PlatformAdminUserPoolClient",
            user_pool_client_name=f"{pool_name}-web",
            auth_flows=cognito.AuthFlow(
                admin_user_password=True,
                user_password=True,
            ),
            generate_secret=False,
        )

        session_secret = secretsmanager.Secret(
            self,
            "PlatformAdminSessionSecret",
            # Pinned to the pre-rebrand name — Secret.Name forces CFN replacement,
            # which would orphan the already-deployed secret. See pool_name above.
            secret_name=f"meshflow-platform-admin-session-{environment}",
            description=f"HiveFlowAI platform admin session signing secret for {environment}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=48,
                exclude_punctuation=True,
            ),
        )

        return {
            "user_pool": user_pool,
            "user_pool_client": user_pool_client,
            "session_secret": session_secret,
        }

    def _create_admin_ui_lambda(
        self,
        *,
        lambda_runtime: HiveFlowLambdaRuntime,
        environment: str,
        portal_resources: dict[str, Any],
        admin_hostname: str,
        config_bucket: s3.IBucket,
    ) -> _lambda.Function:
        user_pool: cognito.UserPool = portal_resources["user_pool"]
        user_pool_client: cognito.UserPoolClient = portal_resources["user_pool_client"]
        session_secret: secretsmanager.Secret = portal_resources["session_secret"]

        scrape_fn = f"platform-{environment}-bc-source-docs-scrape"
        relationships_fn = f"platform-{environment}-bc-source-docs-relationships"
        tags_fn = f"platform-{environment}-bc-source-docs-tags"
        job_functions = [scrape_fn, relationships_fn, tags_fn]

        environment_vars = {
            "HIVEFLOW_UI_MODE": "admin",
            "HIVEFLOW_PLATFORM_UI": "true",
            "HIVEFLOW_ENVIRONMENT": environment,
            "HIVEFLOW_PORTAL_COOKIE_SECURE": "true",
            # Host-only cookies — do not share .hive-flow-ai.com with client portal sessions.
            # Separate cookie name so a portal session on .hive-flow-ai.com cannot clobber admin auth.
            "HIVEFLOW_SESSION_COOKIE": "hiveflow_admin_session",
            "HIVEFLOW_COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "HIVEFLOW_COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
            "HIVEFLOW_PORTAL_DEFAULT_CLIENT_ID": "platform",
            "HIVEFLOW_PORTAL_SESSION_SECRET_ARN": session_secret.secret_arn,
            "HIVEFLOW_ADMIN_HOSTNAME": admin_hostname,
            "HIVEFLOW_ADMIN_USERNAME": "GlobalAdmin",
            "HIVEFLOW_SOURCE_DOCS_SCRAPE_FUNCTION": scrape_fn,
            "HIVEFLOW_SOURCE_DOCS_RELATIONSHIPS_FUNCTION": relationships_fn,
            "HIVEFLOW_SOURCE_DOCS_TAGS_FUNCTION": tags_fn,
            "HIVEFLOW_PRIMARY_SITE_URL": f"https://{admin_hostname}",
            "HIVEFLOW_CONFIG_S3_URI": config_bucket.s3_url_for_object("config.yaml"),
        }

        ui_fn = _lambda.Function(
            self,
            "PlatformAdminUiServeFunction",
            function_name=f"platform-{environment}-platform-admin-serve",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="hiveflow.dna.web.lambda_handler.ui_handler",
            timeout=Duration.seconds(30),
            memory_size=512,
            description=(
                f"Platform admin site for {environment} — GlobalAdmin job runners "
                f"(bundle {UI_BUNDLE_REVISION})"
            ),
            code=lambda_runtime.code,
            layers=lambda_runtime.layers,
            environment=environment_vars,
        )

        config_bucket.grant_read_write(ui_fn, "config.yaml")

        session_secret.grant_read(ui_fn)
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminRespondToAuthChallenge",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminListGroupsForUser",
                    "cognito-idp:ListUsers",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:ForgotPassword",
                    "cognito-idp:ConfirmForgotPassword",
                ],
                resources=["*"],
            )
        )

        account = Stack.of(self).account
        region = Stack.of(self).region
        function_arns = [
            f"arn:aws:lambda:{region}:{account}:function:{name}" for name in job_functions
        ]
        log_group_arns = [
            f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{name}:*"
            for name in job_functions
        ]
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration"],
                resources=function_arns,
            )
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogStreams", "logs:FilterLogEvents", "logs:GetLogEvents"],
                resources=log_group_arns,
            )
        )

        provisioner_project = f"hiveflow-client-provision-{environment}"
        environment_vars["HIVEFLOW_PROVISIONING_PROJECT"] = provisioner_project
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "codebuild:StartBuild",
                    "codebuild:BatchGetBuilds",
                    "codebuild:BatchGetProjects",
                ],
                resources=[
                    f"arn:aws:codebuild:{region}:{account}:project/{provisioner_project}",
                ],
            )
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:ListStacks",
                ],
                resources=["*"],
            )
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hiveflow-*",
                ],
            )
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket", "s3:GetObject", "s3:HeadObject"],
                resources=[
                    f"arn:aws:s3:::hiveflow-*",
                    f"arn:aws:s3:::hiveflow-*/*",
                ],
            )
        )
        # Onboarding step 4: kick off and monitor per-client ingest/DNA Step Functions.
        client_pipeline_state_machines = (
            f"arn:aws:states:{region}:{account}:stateMachine:*-{environment}-*"
        )
        client_pipeline_executions = (
            f"arn:aws:states:{region}:{account}:execution:*-{environment}-*:*"
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "states:StartExecution",
                    "states:ListExecutions",
                ],
                resources=[client_pipeline_state_machines],
            )
        )
        ui_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution"],
                resources=[client_pipeline_executions],
            )
        )
        return ui_fn
