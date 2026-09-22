from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from iam_grants import grant_bedrock_semantic_access


class GlobalAgentPipelinesStack(Stack):
    """Shared, multi-tenant AI-agent pipelines — one deployment per environment.

    Landing zone for agent-driven pipelines that any onboarded company can call
    without a per-company deployment (contrast with ``IngestStack``/``DnaStack``,
    which still hold genuinely per-client resources: the data bucket, OAuth
    secrets, and the tenant IAM role). Each pipeline here reaches a company's
    data by assuming its ``hiveflow-portal-tenant-{company}-{environment}``
    role (``hiveflow.tenant_credentials``) — the same isolation mechanism the
    multi-tenant ``PortalStack`` Lambda already uses.

    Today this holds only the Spreadsheet Engine; any future agent pipeline
    should land here too rather than being built per-company again.

    Deliberately does NOT take a live ``GlobalUiStack`` construct reference for
    the portal session secret (contrast with ``PortalStack``/``ReportingStack``,
    which do): needing that object would force ``app.py`` to construct
    ``GlobalUiStack`` — and pay for its "ui"-profile Lambda bundling — just to
    deploy this stack. Instead this stack imports the secret by its pinned name
    (``hiveflow.project_config.portal_session_secret_name``) so it can be
    deployed alone via ``-c scope=agent_pipelines``, skipping every sibling
    platform stack's bundling (confirmed dominant cost behind slow
    ``GlobalAgentPipelinesStack`` deploys — see ``infra/cdk_scope.py``).
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
        Tags.of(self).add("hiveflow:component", "global-agent-pipelines")
        Tags.of(self).add("hiveflow:environment", env)

        from hiveflow.project_config import (
            cost_allocation_tags,
            get_spreadsheet_engine_hostname,
            portal_session_secret_name,
        )

        for key, value in cost_allocation_tags("PLATFORM", env).items():
            Tags.of(self).add(key, value)

        # Imported by name (see class docstring), not passed in as a live
        # object — None only when the platform UI itself is disabled for this
        # environment (see spreadsheet_engine.create_spreadsheet_engine's
        # docstring for what that means for auth).
        portal_session_secret = (
            secretsmanager.Secret.from_secret_name_v2(
                self, "ImportedPortalSessionSecret", portal_session_secret_name(env)
            )
            if portal_ui_enabled
            else None
        )

        from spreadsheet_engine import create_spreadsheet_engine

        domain_config = ui_config.get("domain", {}) if isinstance(ui_config.get("domain"), dict) else {}
        create_spreadsheet_engine(
            self,
            "Spreadsheet",
            environment=env,
            hostname=get_spreadsheet_engine_hostname(ui_config),
            domain_config=domain_config,
            portal_session_secret=portal_session_secret,
            grant_bedrock=grant_bedrock_semantic_access,
        )
