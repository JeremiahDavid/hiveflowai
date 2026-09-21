"""Spreadsheet Engine: single-Lambda web app + async self-invoke worker.

Global/shared across every company, like the Step Functions pipeline this
replaced (see git history for ``infra/spreadsheet_pipeline.py``) — one
deployment invoked by any onboarded company, reaching that company's data by
assuming its ``hiveflow-portal-tenant-{company}-{environment}`` role
(``hiveflow.tenant_credentials``) rather than holding a standing per-company
S3 grant. No Step Functions: the web Lambda self-invokes asynchronously for
long-running per-table AI calls (see ``hiveflow.spreadsheet_lab.worker``),
reusing the pattern already shipping in the portal's KPI Generator.

Auth is delegated to the real portal too: this app validates the same signed
``hiveflow_portal_session`` cookie the portal issues (see
``hiveflow.spreadsheet_lab.web.auth``) rather than maintaining its own login,
which is why this function needs the portal's session secret and cookie
domain threaded in from ``GlobalUiStack``.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from lambda_bundle import HiveFlowLambdaRuntime, hiveflow_lambda_runtime
from ui_domain import attach_admin_subdomain, import_hosted_zone


def _create_shared_execution_role(scope: Construct, *, environment: str) -> iam.Role:
    """One execution role shared by both Spreadsheet Engine Lambdas.

    A single, predictably-named role (rather than one auto-generated role per
    function) is what lets a single trust-policy entry on each company's
    tenant role (``hiveflow-portal-tenant-{company}-{environment}``) cover
    every Lambda here — both the web/worker Lambda and the materialize
    Lambda need to assume a company's role at runtime, so both share it.
    """
    from hiveflow.project_config import agent_pipelines_role_name

    role = iam.Role(
        scope,
        "SpreadsheetLambdaRole",
        role_name=agent_pipelines_role_name(environment),
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
    return role


def create_spreadsheet_engine(
    scope: Construct,
    construct_id: str,
    *,
    environment: str,
    hostname: str,
    domain_config: dict[str, Any],
    portal_session_secret: secretsmanager.ISecret | None,
    grant_bedrock: Any,
) -> dict[str, Any]:
    """Build the Spreadsheet Engine web/worker Lambda, its materialize-only
    sibling, a REST API, and (if a hosted zone is configured) its subdomain.

    ``portal_session_secret`` is ``None`` when the platform UI itself is
    disabled for this environment — in that case the Spreadsheet Engine still
    deploys, but auth can never succeed (no session secret to validate
    against), matching "no portal means no portal-gated feature" rather than
    silently falling back to an unsigned/no-auth mode.
    """
    prefix = construct_id
    env = environment.strip().lower()
    zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
    primary_hostname = (
        str(domain_config.get("primary_hostname", zone_name)).strip().lower().rstrip(".")
    )
    shared_role = _create_shared_execution_role(scope, environment=env)
    grant_bedrock(shared_role)

    materialize_function_name = f"spreadsheet-engine-{env}-materialize"
    materialize_runtime = hiveflow_lambda_runtime(scope, profile="spreadsheet_lab_materialize")
    materialize_fn = _lambda.Function(
        scope,
        f"{prefix}MaterializeFunction",
        function_name=materialize_function_name,
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet_lab.materialize_lab.lambda_handler",
        timeout=Duration.minutes(2),
        memory_size=1024,
        description=f"Spreadsheet Engine materialize-only worker for {env}",
        code=materialize_runtime.code,
        layers=materialize_runtime.layers,
        role=shared_role,
        environment={
            "HIVEFLOW_ENVIRONMENT": env,
            "HIVEFLOW_TENANT_ASSUME_ROLE": "1",
        },
    )

    environment_vars: dict[str, str] = {
        "HIVEFLOW_ENVIRONMENT": env,
        "HIVEFLOW_TENANT_ASSUME_ROLE": "1",
        "HIVEFLOW_BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        # The literal name (not materialize_fn.function_name, a CloudFormation
        # token) — both functions share one execution role, so a token
        # reference here plus the grant below would close a circular
        # dependency through that role's policy (confirmed by a real deploy
        # failure: "Circular dependency between resources" naming both
        # functions and SpreadsheetLambdaRoleDefaultPolicy).
        "HIVEFLOW_SPREADSHEET_LAB_MATERIALIZE_FUNCTION": materialize_function_name,
    }
    if zone_name:
        environment_vars["HIVEFLOW_PORTAL_COOKIE_DOMAIN"] = f".{zone_name}"
    if primary_hostname:
        environment_vars["HIVEFLOW_PRIMARY_SITE_URL"] = f"https://{primary_hostname}"
    if portal_session_secret is not None:
        environment_vars["HIVEFLOW_PORTAL_SESSION_SECRET_ARN"] = portal_session_secret.secret_arn

    lambda_runtime = hiveflow_lambda_runtime(scope, profile="spreadsheet_lab")
    function_name = f"spreadsheet-engine-{env}-serve"
    ui_fn = _lambda.Function(
        scope,
        f"{prefix}Function",
        function_name=function_name,
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet_lab.web.lambda_handler.handler",
        # Room for the async self-invoke table-task path (see worker.py) — not
        # just the HTTP request/response, which API Gateway itself caps at
        # ~29s regardless of this value.
        timeout=Duration.minutes(5),
        memory_size=1024,
        description=f"Spreadsheet Engine web UI + table-task worker for {env}",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        role=shared_role,
        environment=environment_vars,
    )

    if portal_session_secret is not None:
        portal_session_secret.grant_read(shared_role)

    # Self-invoke for the async table-task worker path (worker._invoke_self_async).
    # Built from the literal function_name (format_arn is pure string
    # construction from account/region), NOT ui_fn.function_arn — that
    # attribute is a CloudFormation GetAtt reference, and attaching a policy
    # that references a function's own ARN to that SAME function's role
    # creates a genuine circular dependency (Function -> Role -> Policy ->
    # Function) that CloudFormation refuses to deploy.
    self_invoke_arn = Stack.of(scope).format_arn(
        service="lambda",
        resource="function",
        resource_name=function_name,
        # Lambda ARNs are colon-separated (arn:...:function:name), unlike the
        # slash-separated default format_arn() otherwise assumes.
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )
    shared_role.add_to_policy(
        iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[self_invoke_arn],
        )
    )

    # Synchronous cross-Lambda call for the parquet write (worker.run_materialize).
    # Same reasoning as the self-invoke grant above: built from the literal
    # materialize_function_name, NOT materialize_fn.grant_invoke(ui_fn) (which
    # would reference materialize_fn.function_arn, a token) — both functions
    # share shared_role, so a token reference here closes a circular
    # dependency through that role's policy.
    materialize_invoke_arn = Stack.of(scope).format_arn(
        service="lambda",
        resource="function",
        resource_name=materialize_function_name,
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )
    shared_role.add_to_policy(
        iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[materialize_invoke_arn],
        )
    )

    web_api = apigateway.RestApi(
        scope,
        f"{prefix}WebApi",
        rest_api_name=f"hiveflow-spreadsheet-engine-{env}".lower(),
        description=f"Spreadsheet Engine UI for {env}",
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
        # attach_admin_subdomain's BasePathMapping targets the API by a plain
        # rest_api_id STRING — correct for its normal caller (GlobalDnsStack,
        # which always runs as a later, separate deploy against an
        # already-existing API from a prior stack), but here it's created in
        # the SAME stack/deploy as web_api, so CloudFormation has no implicit
        # ordering against the API's own Deployment/Stage. Force the
        # ordering explicitly rather than touching the shared ui_domain.py
        # helper.
        for child in scope.node.find_all():
            if isinstance(child, apigateway.CfnBasePathMapping):
                child.add_dependency(web_api.deployment_stage.node.default_child)

    CfnOutput(scope, "SpreadsheetEngineFunctionName", value=ui_fn.function_name)
    CfnOutput(scope, "SpreadsheetEngineMaterializeFunctionName", value=materialize_fn.function_name)
    CfnOutput(scope, "SpreadsheetEngineSiteUrl", value=f"https://{full_hostname}/")
    CfnOutput(scope, "SpreadsheetEngineApiGatewayUrl", value=web_api.url)

    return {
        "web_api": web_api,
        "ui_function": ui_fn,
        "materialize_function": materialize_fn,
        "shared_role": shared_role,
    }
