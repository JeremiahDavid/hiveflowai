from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_route53 as route53
from constructs import Construct

from ui_domain import (
    attach_admin_subdomain,
    attach_client_subdomain,
    attach_custom_domain,
    attach_wildcard_portal_domain,
)


@dataclass(frozen=True)
class ReportingDnsTarget:
    """Reporting API and hostname to map under the platform hosted zone."""

    rest_api_id: str
    client_id: str
    reporting_hostname: str


@dataclass(frozen=True)
class AdminDnsTarget:
    """Platform admin API and hostname to map under the platform hosted zone."""

    rest_api_id: str
    admin_hostname: str


class GlobalDnsStack(Stack):
    """Route 53, ACM, and API Gateway custom domains — deploy separately from GlobalUiStack."""

    hosted_zone: route53.IHostedZone | None

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        ui_config: dict[str, Any],
        global_rest_api_id: str,
        reporting_targets: list[ReportingDnsTarget] | None = None,
        wildcard_portal_api_id: str | None = None,
        admin_target: AdminDnsTarget | None = None,
        manage_base_path_mappings: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.hosted_zone = None
        self._apply_cost_allocation_tags(environment)

        domain_cfg = ui_config.get("domain", {})
        if not isinstance(domain_cfg, dict):
            domain_cfg = {}

        domain_resources = attach_custom_domain(
            self,
            rest_api_id=global_rest_api_id,
            domain_config=domain_cfg,
            manage_base_path_mappings=manage_base_path_mappings,
        )
        self.hosted_zone = domain_resources.get("hosted_zone")
        zone_name = domain_resources["zone_name"]

        for target in reporting_targets or []:
            attach_client_subdomain(
                self,
                rest_api_id=target.rest_api_id,
                hosted_zone=self.hosted_zone,
                zone_name=zone_name,
                client_hostname=target.reporting_hostname or target.client_id,
                manage_base_path_mappings=manage_base_path_mappings,
            )

        if wildcard_portal_api_id and self.hosted_zone is not None:
            attach_wildcard_portal_domain(
                self,
                rest_api_id=wildcard_portal_api_id,
                hosted_zone=self.hosted_zone,
                zone_name=zone_name,
                manage_base_path_mappings=manage_base_path_mappings,
            )

        if admin_target is not None and self.hosted_zone is not None:
            attach_admin_subdomain(
                self,
                rest_api_id=admin_target.rest_api_id,
                hosted_zone=self.hosted_zone,
                zone_name=zone_name,
                admin_hostname=admin_target.admin_hostname,
                manage_base_path_mappings=manage_base_path_mappings,
            )

        if self.hosted_zone is not None:
            CfnOutput(self, "HostedZoneId", value=self.hosted_zone.hosted_zone_id)

    def _apply_cost_allocation_tags(self, environment: str) -> None:
        from hiveflow.project_config import cost_allocation_tags

        for key, value in cost_allocation_tags("PLATFORM", environment).items():
            Tags.of(self).add(key, value)
