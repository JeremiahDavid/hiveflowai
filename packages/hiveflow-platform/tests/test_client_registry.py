"""Tests for client onboarding registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hiveflow.client_registry import (
    ClientCreateSpec,
    ClientRegistry,
    ConnectorSchedule,
    ConnectorSpec,
    DnaSpec,
    PortalClientSpec,
    StackDeployStatus,
    StackLifecycle,
    describe_stack_status,
    merge_stack_status_with_build,
    validate_client_create_spec,
)
from hiveflow.project_config import dna_stack_name, ingest_stack_name


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[3] / "config.yaml"
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_validate_client_create_spec_rejects_duplicate_company(config_path: Path) -> None:
    spec = ClientCreateSpec(
        company="poc",
        client_id="newco",
        environment="dev",
        connectors=(ConnectorSpec(source="dbc", entity_bundle="full"),),
        dna=DnaSpec(source="dbc"),
        portal=PortalClientSpec(display_name="New Co", reporting_hostname="newco"),
    )
    with pytest.raises(ValueError, match="already exists"):
        validate_client_create_spec(spec, path=config_path)


def test_create_client_writes_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "platform": {
                    "environments": {
                        "dev": {
                            "ui": {
                                "portal": {"clients": {}},
                            }
                        }
                    }
                },
                "companies": {},
            }
        ),
        encoding="utf-8",
    )
    registry = ClientRegistry(path=path)
    record = registry.create_client(
        ClientCreateSpec(
            company="acme",
            client_id="acme",
            environment="dev",
            connectors=(
                ConnectorSpec(
                    source="dbc",
                    entity_bundle="full",
                    schedule=ConnectorSchedule(hour=6, minute=30),
                ),
            ),
            dna=DnaSpec(enabled=True, source="dbc", schedule=ConnectorSchedule(hour=7, minute=0)),
            portal=PortalClientSpec(display_name="Acme Corp", reporting_hostname="acme"),
        )
    )
    assert record.company == "acme"
    assert record.client_id == "acme"
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "acme" in saved["companies"]
    assert "acme" in saved["platform"]["environments"]["dev"]["ui"]["portal"]["clients"]


def test_create_client_writes_initial_admin_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "platform": {
                    "environments": {
                        "dev": {
                            "ui": {
                                "portal": {"clients": {}},
                            }
                        }
                    }
                },
                "companies": {},
            }
        ),
        encoding="utf-8",
    )
    registry = ClientRegistry(path=path)
    registry.create_client(
        ClientCreateSpec(
            company="acme",
            client_id="acme",
            environment="dev",
            connectors=(ConnectorSpec(source="dbc", entity_bundle="full"),),
            dna=DnaSpec(source="dbc"),
            portal=PortalClientSpec(
                display_name="Acme Corp",
                reporting_hostname="acme",
                initial_admin_username="jane",
                initial_admin_email="jane@example.com",
            ),
        )
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    client_cfg = saved["platform"]["environments"]["dev"]["ui"]["portal"]["clients"]["acme"]
    assert client_cfg["initial_admin_username"] == "jane"
    assert client_cfg["initial_admin_email"] == "jane@example.com"


def test_create_client_writes_multiple_connectors(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "platform": {
                    "environments": {
                        "dev": {
                            "ui": {
                                "portal": {"clients": {}},
                            }
                        }
                    }
                },
                "companies": {},
            }
        ),
        encoding="utf-8",
    )
    registry = ClientRegistry(path=path)
    registry.create_client(
        ClientCreateSpec(
            company="acme",
            client_id="acme",
            environment="dev",
            connectors=(
                ConnectorSpec(source="dbc", entity_bundle="full", schedule=ConnectorSchedule(hour=6, minute=30)),
                ConnectorSpec(source="qbo", entity_bundle="full_accounting", schedule=ConnectorSchedule(hour=6, minute=0), tier="sandbox"),
            ),
            dna=DnaSpec(enabled=True, source="dbc"),
            portal=PortalClientSpec(display_name="Acme Corp", reporting_hostname="acme"),
        )
    )
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    env_config = saved["companies"]["acme"]["environments"]["dev"]
    assert "dbc" in env_config
    assert "qbo" in env_config
    assert env_config["dna"]["source"] == "dbc"


def test_update_client_rewrites_existing_entry(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "platform": {
                    "environments": {
                        "dev": {
                            "ui": {
                                "portal": {
                                    "clients": {
                                        "acme": {
                                            "display_name": "Acme Corp",
                                            "reporting_company": "acme",
                                            "reporting_hostname": "acme",
                                        }
                                    }
                                },
                            }
                        }
                    }
                },
                "companies": {
                    "acme": {
                        "environments": {
                            "dev": {
                                "aws": {"region": "us-east-2"},
                                "dbc": {"entity_bundle": "full"},
                                "dna": {"enabled": True, "source": "dbc"},
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = ClientRegistry(path=path)
    record = registry.update_client(
        ClientCreateSpec(
            company="acme",
            client_id="acme",
            environment="dev",
            connectors=(
                ConnectorSpec(
                    source="dbc",
                    entity_bundle="v1_accounting",
                    schedule=ConnectorSchedule(hour=7, minute=15),
                ),
            ),
            dna=DnaSpec(enabled=True, source="dbc"),
            portal=PortalClientSpec(display_name="Acme Distribution Co.", reporting_hostname="acme"),
        )
    )
    assert record.portal_display_name == "Acme Distribution Co."
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["companies"]["acme"]["environments"]["dev"]["dbc"]["entity_bundle"] == "v1_accounting"
    assert (
        saved["platform"]["environments"]["dev"]["ui"]["portal"]["clients"]["acme"]["display_name"]
        == "Acme Distribution Co."
    )


def test_get_client_matches_reporting_company_case_insensitively(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "platform": {
                    "environments": {
                        "dev": {
                            "ui": {
                                "portal": {
                                    "clients": {
                                        "poc2": {
                                            "display_name": "POC 2",
                                            "reporting_company": "poc2",
                                            "reporting_hostname": "poc2",
                                        }
                                    }
                                },
                            }
                        }
                    }
                },
                "companies": {
                    "poc2": {
                        "stack_name_company": "POC2",
                        "environments": {
                            "dev": {
                                "aws": {"region": "us-east-2"},
                                "dbc": {"entity_bundle": "full"},
                                "dna": {"enabled": True, "source": "dbc"},
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = ClientRegistry(path=path)
    record = registry.get_client("poc2", environment="dev", client_id="poc2")
    assert record is not None
    assert record.company == "poc2"
    assert record.client_id == "poc2"


def test_expected_stack_names_is_data_plane_only(config_path: Path) -> None:
    # The reporting UI is the shared PortalStack and DNS is the wildcard —
    # a client's expected stacks are just its data plane.
    registry = ClientRegistry(path=config_path)
    record = registry.get_client("poc2", environment="dev", client_id="poc2")
    assert record is not None
    names = registry.expected_stack_names(record)
    assert any(n.startswith("IngestStack-") for n in names)
    assert not any("ReportingStack" in n or "GlobalDnsStack" in n for n in names)


def test_merge_stack_status_with_build_masks_stale_complete() -> None:
    stacks = [
        StackDeployStatus(stack_name="IngestStack-poc2-dev", status=StackLifecycle.COMPLETE),
        StackDeployStatus(stack_name="DnaStack-poc2-dev", status=StackLifecycle.NOT_FOUND),
    ]
    build = {"status": "IN_PROGRESS", "current_phase": "PROVISIONING"}
    merged = merge_stack_status_with_build(stacks, build)
    assert all(item.status == StackLifecycle.IN_PROGRESS for item in merged)
    assert merged[0].status_reason == "CodeBuild provisioning…"


def test_merge_stack_status_with_build_keeps_failed_and_finished() -> None:
    stacks = [
        StackDeployStatus(stack_name="IngestStack-poc2-dev", status=StackLifecycle.COMPLETE),
        StackDeployStatus(stack_name="DnaStack-poc2-dev", status=StackLifecycle.FAILED, status_reason="boom"),
    ]
    in_progress = merge_stack_status_with_build(
        stacks,
        {"status": "IN_PROGRESS", "current_phase": "BUILD"},
    )
    assert in_progress[0].status == StackLifecycle.IN_PROGRESS
    assert in_progress[1].status == StackLifecycle.FAILED

    finished = merge_stack_status_with_build(
        stacks,
        {"status": "SUCCEEDED", "current_phase": "COMPLETED"},
    )
    assert finished == stacks


def test_describe_stack_status_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def describe_stacks(self, **kwargs):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ValidationError", "Message": "does not exist"}}, "DescribeStacks")

        def describe_stack_events(self, **kwargs):
            return {"StackEvents": []}

    class FakeBoto3:
        def client(self, name, region_name=None):
            return FakeClient()

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3())
    status = describe_stack_status("MissingStack-dev")
    assert status.status == StackLifecycle.NOT_FOUND


def test_stack_names_use_lowercase_company_slug(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"companies": {"acme": {"environments": {"dev": {}}}}}),
        encoding="utf-8",
    )
    assert ingest_stack_name("acme", "dev", path=path) == "IngestStack-acme-dev"
    assert dna_stack_name("acme", "Prod", path=path) == "DnaStack-acme-prod"


def test_stack_names_honor_legacy_stack_name_company(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "companies": {
                    "poc": {
                        "stack_name_company": "POC",
                        "environments": {"dev": {}},
                    },
                    "acme": {
                        "environments": {"dev": {}},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    assert ingest_stack_name("poc", "dev", path=path) == "IngestStack-POC-dev"
    assert dna_stack_name("POC", "dev", path=path) == "DnaStack-POC-dev"
    assert ingest_stack_name("acme", "dev", path=path) == "IngestStack-acme-dev"
