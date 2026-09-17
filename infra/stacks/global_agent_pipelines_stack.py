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
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        portal_session_secret: secretsmanager.ISecret | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env = environment.strip().lower()
        Tags.of(self).add("hiveflow:component", "global-agent-pipelines")
        Tags.of(self).add("hiveflow:environment", env)

        from hiveflow.project_config import cost_allocation_tags, get_spreadsheet_engine_hostname

        for key, value in cost_allocation_tags("PLATFORM", env).items():
            Tags.of(self).add(key, value)

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
