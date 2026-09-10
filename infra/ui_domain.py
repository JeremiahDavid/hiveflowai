from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Fn, RemovalPolicy, aws_apigateway as apigateway
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from constructs import Construct

from hiveflow.dna.web.domain_names import dns_record_name, expand_hostnames


def _sanitize_id(hostname: str) -> str:
    return hostname.replace(".", "-").replace("*", "star")


def _create_base_path_mapping(
    scope: Construct,
    construct_id: str,
    *,
    domain: apigateway.DomainName,
    rest_api_id: str,
    stage_name: str = "prod",
) -> apigateway.CfnBasePathMapping:
    """Map a custom domain to a REST API stage by name (no cross-stack stage export)."""
    return apigateway.CfnBasePathMapping(
        scope,
        construct_id,
        domain_name=domain.domain_name,
        rest_api_id=rest_api_id,
        stage=stage_name,
    )


def attach_custom_domain(
    scope: Construct,
    *,
    rest_api_id: str,
    domain_config: dict[str, Any],
    stage_name: str = "prod",
    manage_base_path_mappings: bool = True,
) -> dict[str, Any]:
    """Attach Route53 + ACM + API Gateway custom domain mappings for the UI."""
    zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
    if not zone_name:
        raise ValueError("ui.domain.zone_name is required when ui.domain is configured")

    primary_hostname = str(domain_config.get("primary_hostname", zone_name)).strip().lower().rstrip(".")
    alternate_raw = domain_config.get("alternate_hostnames", ["www"])
    if not isinstance(alternate_raw, list):
        alternate_raw = ["www"]

    hostnames = expand_hostnames(
        zone_name=zone_name,
        primary_hostname=primary_hostname,
        alternate_hostnames=[str(item) for item in alternate_raw],
    )

    hosted_zone_id = str(domain_config.get("hosted_zone_id", "")).strip()
    create_hosted_zone = bool(domain_config.get("create_hosted_zone", not hosted_zone_id))

    if hosted_zone_id:
        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            scope,
            "ImportedHostedZone",
            hosted_zone_id=hosted_zone_id,
            zone_name=zone_name,
        )
    elif create_hosted_zone:
        hosted_zone = route53.PublicHostedZone(
            scope,
            "PublicHostedZone",
            zone_name=zone_name,
            comment=f"HiveFlowAI public site and client portal ({zone_name})",
        )
        hosted_zone.apply_removal_policy(RemovalPolicy.RETAIN)
    else:
        raise ValueError(
            "ui.domain requires hosted_zone_id or create_hosted_zone: true "
            "so CDK can manage DNS records and ACM validation"
        )

    certificate = acm.Certificate(
        scope,
        "SiteCertificate",
        domain_name=primary_hostname,
        subject_alternative_names=[name for name in hostnames if name != primary_hostname],
        validation=acm.CertificateValidation.from_dns(hosted_zone),
    )
    certificate.apply_removal_policy(RemovalPolicy.RETAIN)

    custom_urls: list[str] = []
    for index, hostname in enumerate(hostnames):
        domain = apigateway.DomainName(
            scope,
            f"CustomDomain{_sanitize_id(hostname)}",
            domain_name=hostname,
            certificate=certificate,
            endpoint_type=apigateway.EndpointType.REGIONAL,
        )
        if manage_base_path_mappings:
            _create_base_path_mapping(
                scope,
                f"BasePathMapping{_sanitize_id(hostname)}",
                domain=domain,
                rest_api_id=rest_api_id,
                stage_name=stage_name,
            )
        route53.ARecord(
            scope,
            f"AliasRecord{_sanitize_id(hostname)}",
            zone=hosted_zone,
            record_name=dns_record_name(hostname, zone_name),
            target=route53.RecordTarget.from_alias(route53_targets.ApiGatewayDomain(domain)),
        )
        custom_urls.append(f"https://{hostname}/")

    if create_hosted_zone and not hosted_zone_id:
        CfnOutput(
            scope,
            "Route53NameServers",
            value=Fn.join(", ", hosted_zone.hosted_zone_name_servers),
            description=(
                "Update your Squarespace domain nameservers to these Route 53 values "
                "to delegate DNS for the custom domain"
            ),
        )
        for index in range(4):
            CfnOutput(
                scope,
                f"Route53NameServer{index + 1}",
                value=Fn.select(index, hosted_zone.hosted_zone_name_servers),
                description=f"Route 53 nameserver {index + 1} for Squarespace delegation",
            )

    CfnOutput(
        scope,
        "PrimarySiteUrl",
        value=f"https://{primary_hostname}/",
        description="Primary HiveFlowAI site URL after DNS delegation completes",
    )
    CfnOutput(
        scope,
        "CustomDomainHostnames",
        value=", ".join(hostnames),
        description="Hostnames mapped to the HiveFlowAI UI API",
    )

    return {
        "zone_name": zone_name,
        "primary_hostname": primary_hostname,
        "hostnames": hostnames,
        "custom_urls": custom_urls,
        "hosted_zone": hosted_zone,
        "certificate": certificate,
    }


def attach_client_subdomain(
    scope: Construct,
    *,
    rest_api_id: str,
    hosted_zone: route53.IHostedZone,
    zone_name: str,
    client_hostname: str,
    stage_name: str = "prod",
    manage_base_path_mappings: bool = True,
) -> str:
    """Map a portal client subdomain (e.g. poc.hive-flow-ai.com) to a reporting API."""
    hostname = client_hostname.strip().lower().rstrip(".")
    if not hostname.endswith(zone_name):
        hostname = f"{hostname}.{zone_name}"

    certificate = acm.Certificate(
        scope,
        f"ClientCertificate{_sanitize_id(hostname)}",
        domain_name=hostname,
        validation=acm.CertificateValidation.from_dns(hosted_zone),
    )
    certificate.apply_removal_policy(RemovalPolicy.RETAIN)

    domain = apigateway.DomainName(
        scope,
        f"ClientDomain{_sanitize_id(hostname)}",
        domain_name=hostname,
        certificate=certificate,
        endpoint_type=apigateway.EndpointType.REGIONAL,
    )
    if manage_base_path_mappings:
        _create_base_path_mapping(
            scope,
            f"ClientBasePathMapping{_sanitize_id(hostname)}",
            domain=domain,
            rest_api_id=rest_api_id,
            stage_name=stage_name,
        )
    route53.ARecord(
        scope,
        f"ClientAliasRecord{_sanitize_id(hostname)}",
        zone=hosted_zone,
        record_name=dns_record_name(hostname, zone_name),
        target=route53.RecordTarget.from_alias(route53_targets.ApiGatewayDomain(domain)),
    )
    CfnOutput(
        scope,
        f"ReportingSiteUrl{_sanitize_id(hostname)}",
        value=f"https://{hostname}/",
        description=f"Client reporting dashboard URL ({hostname})",
    )
    return f"https://{hostname}/"


def attach_wildcard_portal_domain(
    scope: Construct,
    *,
    rest_api_id: str,
    hosted_zone: route53.IHostedZone,
    zone_name: str,
    stage_name: str = "prod",
    manage_base_path_mappings: bool = True,
) -> str:
    """Map ``*.{zone}`` to the single multi-tenant PortalStack API.

    API Gateway resolves an exact custom-domain name before this wildcard, so any
    still-attached per-client ``attach_client_subdomain`` mapping keeps winning
    until it is removed — that is the per-client cut-over lever.
    """
    hostname = f"*.{zone_name}"
    certificate = acm.Certificate(
        scope,
        "PortalWildcardCertificate",
        domain_name=hostname,
        validation=acm.CertificateValidation.from_dns(hosted_zone),
    )
    certificate.apply_removal_policy(RemovalPolicy.RETAIN)

    domain = apigateway.DomainName(
        scope,
        "PortalWildcardDomain",
        domain_name=hostname,
        certificate=certificate,
        endpoint_type=apigateway.EndpointType.REGIONAL,
    )
    if manage_base_path_mappings:
        _create_base_path_mapping(
            scope,
            "PortalWildcardBasePathMapping",
            domain=domain,
            rest_api_id=rest_api_id,
            stage_name=stage_name,
        )
    route53.ARecord(
        scope,
        "PortalWildcardAliasRecord",
        zone=hosted_zone,
        record_name=dns_record_name(hostname, zone_name),
        target=route53.RecordTarget.from_alias(route53_targets.ApiGatewayDomain(domain)),
    )
    CfnOutput(
        scope,
        "PortalWildcardSiteUrl",
        value=f"https://<client>.{zone_name}/",
        description="Multi-tenant portal — every client's reporting subdomain",
    )
    return hostname


def attach_admin_subdomain(
    scope: Construct,
    *,
    rest_api_id: str,
    hosted_zone: route53.IHostedZone,
    zone_name: str,
    admin_hostname: str,
    stage_name: str = "prod",
    manage_base_path_mappings: bool = True,
) -> str:
    """Map platform admin subdomain (e.g. admin.hive-flow-ai.com) to the admin API."""
    hostname = admin_hostname.strip().lower().rstrip(".")
    if not hostname.endswith(zone_name):
        hostname = f"{hostname}.{zone_name}"

    certificate = acm.Certificate(
        scope,
        f"AdminCertificate{_sanitize_id(hostname)}",
        domain_name=hostname,
        validation=acm.CertificateValidation.from_dns(hosted_zone),
    )
    certificate.apply_removal_policy(RemovalPolicy.RETAIN)

    domain = apigateway.DomainName(
        scope,
        f"AdminDomain{_sanitize_id(hostname)}",
        domain_name=hostname,
        certificate=certificate,
        endpoint_type=apigateway.EndpointType.REGIONAL,
    )
    if manage_base_path_mappings:
        _create_base_path_mapping(
            scope,
            f"AdminBasePathMapping{_sanitize_id(hostname)}",
            domain=domain,
            rest_api_id=rest_api_id,
            stage_name=stage_name,
        )
    route53.ARecord(
        scope,
        f"AdminAliasRecord{_sanitize_id(hostname)}",
        zone=hosted_zone,
        record_name=dns_record_name(hostname, zone_name),
        target=route53.RecordTarget.from_alias(route53_targets.ApiGatewayDomain(domain)),
    )
    CfnOutput(
        scope,
        "AdminSiteUrl",
        value=f"https://{hostname}/",
        description="Platform admin site URL",
    )
    return f"https://{hostname}/"


def import_hosted_zone(
    scope: Construct,
    domain_config: dict[str, Any],
    *,
    construct_id: str = "ImportedHostedZone",
) -> route53.IHostedZone | None:
    """Reference an existing Route 53 zone without creating or modifying DNS records."""
    zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
    hosted_zone_id = str(domain_config.get("hosted_zone_id", "")).strip()
    if not zone_name or not hosted_zone_id:
        return None
    return route53.HostedZone.from_hosted_zone_attributes(
        scope,
        construct_id,
        hosted_zone_id=hosted_zone_id,
        zone_name=zone_name,
    )
