"""Synth assertions for the shared multi-tenant PortalStack.

The serve role must have NO standing access to tenant data — the only route in
is ``sts:AssumeRole`` on the per-company ``hiveflow-portal-tenant-*`` roles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).resolve().parents[1] / "infra"
sys.path.insert(0, str(INFRA_DIR))

aws_cdk = pytest.importorskip("aws_cdk")
from aws_cdk import App, Stack  # noqa: E402
from aws_cdk import aws_cognito as cognito  # noqa: E402
from aws_cdk import aws_secretsmanager as secretsmanager  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

from stacks.portal_stack import PortalStack  # noqa: E402

_ENV = {"account": "123456789012", "region": "us-east-2"}
_UI_CONFIG = {"domain": {"zone_name": "hive-flow-ai.com", "primary_hostname": "hive-flow-ai.com"}}


@pytest.fixture(scope="module")
def template() -> Template:
    # Skip Docker/pip asset bundling during synth.
    app = App(context={"aws:cdk:bundling-stacks": []})
    deps = Stack(app, "Deps", env=_ENV)
    pool = cognito.UserPool(deps, "Pool")
    client = pool.add_client("Client")
    secret = secretsmanager.Secret(deps, "SessionSecret")

    stack = PortalStack(
        app,
        "PortalStack-dev",
        environment="dev",
        ui_config=_UI_CONFIG,
        portal_user_pool=pool,
        portal_user_pool_client=client,
        portal_session_secret=secret,
        domain_config=_UI_CONFIG["domain"],
        env=_ENV,
    )
    return Template.from_stack(stack)


def test_single_lambda_and_api(template: Template) -> None:
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "portal-dev-reporting-ui-serve",
            "Environment": {
                "Variables": Match.object_like(
                    {"HIVEFLOW_UI_MODE": "reporting_multitenant", "HIVEFLOW_TENANT_ASSUME_ROLE": "1"}
                )
            },
        },
    )


def test_serve_role_named_for_tenant_trust(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Role",
        {"RoleName": "hiveflow-portal-dev-serve-role"},
    )


def test_serve_policy_only_reaches_tenant_data_via_assume_role(template: Template) -> None:
    policies = template.find_resources("AWS::IAM::Policy")
    statements: list[dict] = []
    for pol in policies.values():
        statements.extend(pol["Properties"]["PolicyDocument"]["Statement"])

    blob = json.dumps(statements)
    # The only path to tenant data is assuming a per-company tenant role.
    assert "hiveflow-portal-tenant-*-dev" in blob
    # No standing data-plane grants on the serve role itself.
    for stmt in statements:
        actions = stmt.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        for act in actions:
            assert not act.startswith("athena:"), stmt
            assert not act.startswith("glue:"), stmt
            assert not act.startswith("states:"), stmt
            # S3 is limited to the platform config object, never a data bucket.
            if act.startswith("s3:"):
                assert act == "s3:GetObject", stmt


def test_canary_deployment_group(template: Template) -> None:
    template.resource_count_is("AWS::CodeDeploy::DeploymentGroup", 1)
    template.resource_count_is("AWS::Lambda::Alias", 1)
