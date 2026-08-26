from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from hiveflow.provisioning import provisioning_project_name


class ProvisioningStack(Stack):
    """CodeBuild project that runs scoped cdk deploy for client onboarding."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        config_bucket: s3.IBucket | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        env = environment.strip().lower()
        Tags.of(self).add("Environment", env)
        Tags.of(self).add("Application", "hiveflow")

        project_root = Path(__file__).resolve().parents[2]
        buildspec_path = project_root / "infra" / "provisioning" / "buildspec.yml"

        role = iam.Role(
            self,
            "ProvisionerRole",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            description=f"HiveFlow client provisioning CodeBuild role ({env})",
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
        )

        log_group = logs.LogGroup(
            self,
            "ProvisionerLogs",
            log_group_name=f"/hiveflow/provisioning/{env}",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        project_name = provisioning_project_name(env)
        build_environment_kwargs: dict[str, Any] = {
            "build_image": codebuild.LinuxBuildImage.STANDARD_7_0,
            "privileged": True,
            "compute_type": codebuild.ComputeType.LARGE,
        }
        if config_bucket is not None:
            build_environment_kwargs["environment_variables"] = {
                "HIVEFLOW_CONFIG_S3_URI": codebuild.BuildEnvironmentVariable(
                    value=config_bucket.s3_url_for_object("config.yaml"),
                ),
            }
            config_bucket.grant_read(role, "config.yaml")

        github_owner = str(self.node.try_get_context("hiveflowGithubOwner") or "JeremiahDavid")
        # GitHub repo renamed glue -> hiveflowai (2026-08). CodeBuild stores the
        # resolved owner/repo, so GitHub's rename redirect does NOT cover it --
        # ProvisioningStack must be redeployed for this to take effect.
        # Override without a code change: -c hiveflowGithubRepo=...
        github_repo = str(self.node.try_get_context("hiveflowGithubRepo") or "hiveflowai")
        github_connection_arn = str(
            self.node.try_get_context("hiveflowGithubConnectionArn") or ""
        ).strip()

        self.project = codebuild.Project(
            self,
            "ClientProvisioner",
            project_name=project_name,
            role=role,
            timeout=Duration.hours(2),
            queued_timeout=Duration.minutes(30),
            build_spec=codebuild.BuildSpec.from_asset(str(buildspec_path)),
            source=codebuild.Source.git_hub(
                owner=github_owner,
                repo=github_repo,
                webhook=False,
            ),
            environment=codebuild.BuildEnvironment(**build_environment_kwargs),
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=log_group, enabled=True),
            ),
            description=f"Deploy ingest/DNA/reporting stacks for new HiveFlow clients ({env})",
        )

        cfn_project = self.project.node.default_child
        if isinstance(cfn_project, codebuild.CfnProject):
            # On-demand provisioner: no push webhooks or GitHub commit status posts.
            cfn_project.add_property_override("Source.ReportBuildStatus", False)
            if github_connection_arn:
                cfn_project.add_property_override(
                    "Source.Auth",
                    {
                        "Type": "CODECONNECTIONS",
                        "Resource": github_connection_arn,
                    },
                )
                role.add_to_policy(
                    iam.PolicyStatement(
                        actions=["codeconnections:UseConnection"],
                        resources=[github_connection_arn],
                    )
                )

        CfnOutput(self, "ProvisioningProjectName", value=project_name)
        CfnOutput(self, "ProvisioningProjectArn", value=self.project.project_arn)
