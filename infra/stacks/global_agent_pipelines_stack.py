from __future__ import annotations

from aws_cdk import CfnOutput, Stack, Tags
from constructs import Construct

from iam_grants import grant_bedrock_semantic_access
from lambda_bundle import hiveflow_lambda_runtime

_DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class GlobalAgentPipelinesStack(Stack):
    """Shared, multi-tenant AI-agent pipelines — one deployment per environment.

    Landing zone for agent-driven pipelines that any onboarded company can call
    without a per-company deployment (contrast with ``IngestStack``/``DnaStack``,
    which still hold genuinely per-client resources: the data bucket, OAuth
    secrets, and the tenant IAM role). Each pipeline here is invoked with
    ``company`` as part of its input and reaches that company's data by
    assuming its ``hiveflow-portal-tenant-{company}-{environment}`` role
    (``hiveflow.tenant_credentials``) for the duration of the call — the same
    isolation mechanism the multi-tenant ``PortalStack`` Lambda already uses.

    Today this holds only the Spreadsheet Engine; any future agent pipeline
    should land here too rather than being built per-company again.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env = environment.strip().lower()
        Tags.of(self).add("hiveflow:component", "global-agent-pipelines")
        Tags.of(self).add("hiveflow:environment", env)

        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags("PLATFORM", env).items():
            Tags.of(self).add(key, value)

        from spreadsheet_pipeline import create_spreadsheet_pipeline

        lambda_runtime = hiveflow_lambda_runtime(self)
        spreadsheet_resources = create_spreadsheet_pipeline(
            self,
            "Spreadsheet",
            environment=env,
            lambda_runtime=lambda_runtime,
            common_env={
                "HIVEFLOW_ENVIRONMENT": env,
                "HIVEFLOW_BEDROCK_MODEL_ID": _DEFAULT_BEDROCK_MODEL_ID,
            },
            grant_bedrock=grant_bedrock_semantic_access,
        )

        CfnOutput(
            self,
            "SpreadsheetAnalyzeStateMachineArn",
            value=spreadsheet_resources["state_machine"].state_machine_arn,
        )
        CfnOutput(
            self,
            "SpreadsheetAnalyzeStateMachineName",
            value=spreadsheet_resources["state_machine"].state_machine_name,
        )
