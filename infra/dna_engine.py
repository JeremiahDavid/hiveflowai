"""DNA Engine: catalog/pack browser, governance (compile/validate/publish/
restore), data profile, model mapping, source docs browser, and KPI
Generator — one Lambda, its own subdomain, its own app (see
``hiveflow.dna_engine.web``). Split out of the multi-tenant portal's shared
Lambda (``PortalStack``) so this content's blast radius, iteration speed, and
Bedrock/Cognito permission surface are independent of the client-facing
Reporting Engine.

Like Spreadsheet Engine (see ``infra/spreadsheet_engine.py``, the pattern
this mirrors), it reaches a company's data by assuming its
``hiveflow-portal-tenant-{company}-{environment}`` role
(``hiveflow.tenant_credentials``) rather than holding a standing per-company
S3 grant, and it trusts the real portal's session cookie
(``hiveflow.dna_engine.web.auth``) rather than maintaining its own login.

Unlike Spreadsheet Engine, this app also needs Cognito admin actions
(governance/users invites and role changes) and Bedrock (KPI Generator
drafting) on its own role, and self-invokes itself (not a sibling Lambda)
for the KPI Generator's async generation step — ``AWS_LAMBDA_FUNCTION_NAME``
already resolves to this function's own name at runtime, so no separate
self-invoke-target wiring is needed the way Spreadsheet Engine's
materialize hop requires.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from lambda_bundle import hiveflow_lambda_runtime
from ui_domain import attach_admin_subdomain, import_hosted_zone


def _create_execution_role(
    scope: Construct,
    *,
    environment: str,
    portal_user_pool: cognito.IUserPool | None,
) -> iam.Role:
    """DNA Engine's own execution role.

    A dedicated, predictably-named role (not shared with Spreadsheet Engine's
    ``GlobalAgentPipelinesStack`` role) — its trust/permission surface is
    materially different (Cognito admin actions, Bedrock), and each
    company's tenant role still needs to trust it with one predictable
    trust-policy entry.
    """
    from hiveflow.project_config import dna_engine_role_name

    role = iam.Role(
        scope,
        "DnaEngineLambdaRole",
        role_name=dna_engine_role_name(environment),
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            ),
        ],
    )
    account = Stack.of(scope).account
    env_slug = environment.strip().lower()
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{account}:role/hiveflow-portal-tenant-*-{env_slug}"],
        )
    )
    if portal_user_pool is not None:
        # Governance/users invites, role changes, and the is_admin check every
        # authenticated request makes — see hiveflow.dna.web.portal.cognito
        # and docs/dna-engine.md. Login itself stays the shell's job
        # (PortalStack keeps AdminInitiateAuth/AdminRespondToAuthChallenge/
        # AdminGetUser); DNA Engine only ever validates an already-issued
        # session cookie, never authenticates a user itself.
        role.add_to_policy(
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
    return role


def create_dna_engine(
    scope: Construct,
    construct_id: str,
    *,
    environment: str,
    hostname: str,
    domain_config: dict[str, Any],
    portal_session_secret: secretsmanager.ISecret | None,
    portal_user_pool: cognito.IUserPool | None,
    portal_user_pool_client: cognito.IUserPoolClient | None,
    grant_bedrock: Any,
) -> dict[str, Any]:
    """Build the DNA Engine Lambda, its REST API, and (if a hosted zone is
    configured) its subdomain.

    ``portal_session_secret``/``portal_user_pool``/``portal_user_pool_client``
    are all ``None`` when the platform UI itself is disabled for this
    environment — DNA Engine still deploys, but auth can never succeed,
    matching Spreadsheet Engine's "no portal means no portal-gated feature"
    contract rather than silently falling back to an unsigned/no-auth mode.
    """
    prefix = construct_id
    env = environment.strip().lower()
    zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
    primary_hostname = (
        str(domain_config.get("primary_hostname", zone_name)).strip().lower().rstrip(".")
    )
    role = _create_execution_role(scope, environment=env, portal_user_pool=portal_user_pool)
    grant_bedrock(role)

    function_name = f"dna-engine-{env}-serve"

    environment_vars: dict[str, str] = {
        "HIVEFLOW_ENVIRONMENT": env,
        "HIVEFLOW_TENANT_ASSUME_ROLE": "1",
        "HIVEFLOW_BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    if zone_name:
        environment_vars["HIVEFLOW_PORTAL_COOKIE_DOMAIN"] = f".{zone_name}"
    if primary_hostname:
        environment_vars["HIVEFLOW_PRIMARY_SITE_URL"] = f"https://{primary_hostname}"
    if portal_session_secret is not None:
        environment_vars["HIVEFLOW_PORTAL_SESSION_SECRET_ARN"] = portal_session_secret.secret_arn
        portal_session_secret.grant_read(role)
    if portal_user_pool is not None and portal_user_pool_client is not None:
        environment_vars["HIVEFLOW_COGNITO_USER_POOL_ID"] = portal_user_pool.user_pool_id
        environment_vars["HIVEFLOW_COGNITO_CLIENT_ID"] = portal_user_pool_client.user_pool_client_id

    lambda_runtime = hiveflow_lambda_runtime(scope, profile="dna_engine")
    fn = _lambda.Function(
        scope,
        f"{prefix}Function",
        function_name=function_name,
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.dna_engine.web.lambda_handler.handler",
        # Room for the async KPI Generator self-invoke path, not just the
        # HTTP request/response (API Gateway itself caps that at ~29s).
        timeout=Duration.minutes(5),
        memory_size=1536,
        description=f"DNA Engine UI + KPI Generator worker for {env}",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        role=role,
        environment=environment_vars,
    )

    # KPI Generator's async self-invoke (run_kpi_generation_job) — built from
    # the literal function_name, not fn.function_arn (a CloudFormation
    # token): a policy on this function's own role referencing that token
    # creates a circular dependency (Function -> Role -> Policy -> Function),
    # same reasoning spreadsheet_engine.py's self-invoke grant documents.
    self_invoke_arn = Stack.of(scope).format_arn(
        service="lambda",
        resource="function",
        resource_name=function_name,
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[self_invoke_arn],
        )
    )

    web_api = apigateway.RestApi(
        scope,
        f"{prefix}WebApi",
        rest_api_name=f"hiveflow-dna-engine-{env}".lower(),
        description=f"DNA Engine UI for {env}",
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
    integration = apigateway.LambdaIntegration(fn, proxy=True, allow_test_invoke=False)
    web_api.root.add_method("ANY", integration)
    web_api.root.add_proxy(default_integration=integration, any_method=True)

    full_hostname = f"{hostname}.{zone_name}" if zone_name else hostname
    hosted_zone = import_hosted_zone(scope, domain_config)
    if hosted_zone is not None and zone_name:
        attach_admin_subdomain(
            scope,
            rest_api_id=web_api.rest_api_id,
            hosted_zone=hosted_zone,
            zone_name=zone_name,
            admin_hostname=hostname,
        )
        # Same-stack/same-deploy ordering fix as spreadsheet_engine.py's
        # identical block — attach_admin_subdomain's BasePathMapping has no
        # implicit CloudFormation ordering against this API's own
        # Deployment/Stage when both are created in this one deploy.
        for child in scope.node.find_all():
            if isinstance(child, apigateway.CfnBasePathMapping):
                child.add_dependency(web_api.deployment_stage.node.default_child)

    CfnOutput(scope, "DnaEngineFunctionName", value=fn.function_name)
    CfnOutput(scope, "DnaEngineSiteUrl", value=f"https://{full_hostname}/")
    CfnOutput(scope, "DnaEngineApiGatewayUrl", value=web_api.url)

    return {
        "web_api": web_api,
        "function": fn,
        "role": role,
    }
