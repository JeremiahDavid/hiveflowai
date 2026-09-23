from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from iam_grants import grant_bedrock_semantic_access


class GlobalDnaEngineStack(Stack):
    """DNA Engine — catalog/governance/data-profile/model-mapping/source-docs/
    KPI Generator, on its own subdomain/Lambda — one deployment per environment.

    Split out of the multi-tenant portal (see ``PortalStack``, now Reporting
    Engine-only) the same way ``GlobalAgentPipelinesStack`` split Spreadsheet
    Engine out: its own stack/scope so it can be iterated on and deployed
    without bundling every other platform stack. A separate stack from
    ``GlobalAgentPipelinesStack`` (not folded in, despite that stack's own
    docstring inviting future agent pipelines) because DNA Engine's Lambda
    profile is materially heavier (full DNA stack + Bedrock + Cognito admin
    actions) than Spreadsheet Engine's slim one — keeping them separate
    preserves fast, independent iteration for each.

    Deliberately does NOT take live ``GlobalUiStack`` construct references
    (contrast with ``PortalStack``, which does): the session secret is
    imported by its pinned name (same trick ``GlobalAgentPipelinesStack``
    uses), and the Cognito user pool/client are imported by SSM parameter
    name (``hiveflow.project_config.portal_user_pool_id_parameter_name`` /
    ``..._client_id_parameter_name``, published by ``GlobalUiStack``) rather
    than by live object reference — a ``CfnOutput`` alone can't be read
    cross-stack without a hard ``Fn::ImportValue`` dependency, which is
    exactly the coupling this avoids. This lets ``-c scope=dna_engine`` alone
    deploy without constructing ``GlobalUiStack`` and paying for its "ui"
    Lambda bundling.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        portal_ui_enabled: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env = environment.strip().lower()
        Tags.of(self).add("hiveflow:component", "global-dna-engine")
        Tags.of(self).add("hiveflow:environment", env)

        from hiveflow.project_config import (
            cost_allocation_tags,
            get_dna_engine_hostname,
            portal_session_secret_name,
            portal_user_pool_client_id_parameter_name,
            portal_user_pool_id_parameter_name,
        )

        for key, value in cost_allocation_tags("PLATFORM", env).items():
            Tags.of(self).add(key, value)

        # Imported by name/parameter (see class docstring) — None only when
        # the platform UI itself is disabled for this environment.
        portal_session_secret = (
            secretsmanager.Secret.from_secret_name_v2(
                self, "ImportedPortalSessionSecret", portal_session_secret_name(env)
            )
            if portal_ui_enabled
            else None
        )
        portal_user_pool = None
        portal_user_pool_client = None
        if portal_ui_enabled:
            from aws_cdk import aws_ssm as ssm

            user_pool_id = ssm.StringParameter.value_for_string_parameter(
                self, portal_user_pool_id_parameter_name(env)
            )
            user_pool_client_id = ssm.StringParameter.value_for_string_parameter(
                self, portal_user_pool_client_id_parameter_name(env)
            )
            portal_user_pool = cognito.UserPool.from_user_pool_id(
                self, "ImportedPortalUserPool", user_pool_id
            )
            portal_user_pool_client = cognito.UserPoolClient.from_user_pool_client_id(
                self, "ImportedPortalUserPoolClient", user_pool_client_id
            )

        from dna_engine import create_dna_engine

        domain_config = ui_config.get("domain", {}) if isinstance(ui_config.get("domain"), dict) else {}
        create_dna_engine(
            self,
            "DnaEngine",
            environment=env,
            hostname=get_dna_engine_hostname(ui_config),
            domain_config=domain_config,
            portal_session_secret=portal_session_secret,
            portal_user_pool=portal_user_pool,
            portal_user_pool_client=portal_user_pool_client,
            grant_bedrock=grant_bedrock_semantic_access,
        )
