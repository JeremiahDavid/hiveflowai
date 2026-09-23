"""Black-box route-completeness smoke test for `create_app`.

For each HIVEFLOW_UI_MODE, GET every registered static (non-<param>) path and
assert dispatch reaches a real handler — not a 404 (route never registered /
dropped) and not a 500 (dispatch found the endpoint but the handler blew up,
usually because it wasn't wired with the right closures). A redirect to login,
a 401/403, or a 200 are all fine; the point is only that routing itself works.

This is the regression harness for splitting `app.py`'s ~90 routes into
per-surface modules (marketing / portal / admin) — it is cheap insurance
against silently dropping or mis-registering an endpoint during that move,
without hand-writing a bespoke test per route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.test import Client

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import create_app
from hiveflow.project_config import load_project_config

# GET-safe, non-parameterized paths per ui_mode, read directly off the Rule
# lists in app.py's create_app(). Dynamic-segment routes (<company>, <job_id>,
# <output_id>, <path:subpath>, ...) and POST-only routes are intentionally
# excluded — this test only proves static GET dispatch is intact.

ADMIN_PATHS = [
    "/",
    "/admin",
    "/admin/",
    "/admin/login",
    "/admin/logout",
    "/admin/architecture",
    "/admin/onboarding",
    "/admin/onboarding/new",
]

GLOBAL_PATHS = [
    "/",
    "/platform",
    "/pricing",
    "/portal/login",
    "/portal/logout",
    "/portal",
    "/portal/",
]

# DNA/Agents/Governance/Source Browser now live entirely on the DNA Engine
# subdomain (see docs/dna-engine.md, hiveflow.dna_engine.web) — their route
# dispatch is covered by test_dna_engine_web.py, not here. "/portal/admin/*"
# and "/definitions"/"/semantics" are cross-subdomain redirects intercepted
# before the router even runs (see app.py's DNA_ENGINE_LEGACY_REDIRECTS).
REPORTING_PATHS = [
    "/portal/login",
    "/portal/logout",
    "/portal",
    "/portal/",
    "/portal/executive",
    "/portal/revenue",
    "/portal/revenue-trend",
    "/portal/chart-demo",
    "/api/pack",
    "/api/manifest",
    "/api/reporting/pages",
    "/api/reporting/catalog",
]

NON_ROUTABLE = {404, 500}


def _app(tmp_path: Path, *, ui_mode: str):
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    return create_app(
        settings,
        company="POC",
        environment="dev",
        env_config=env_config,
        ui_mode=ui_mode,
    )


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_admin_routes_are_dispatchable(tmp_path: Path, path: str) -> None:
    client = Client(_app(tmp_path, ui_mode="admin"))
    response = client.get(path)
    assert response.status_code not in NON_ROUTABLE, f"{path} -> {response.status_code}"


@pytest.mark.parametrize("path", GLOBAL_PATHS)
def test_global_routes_are_dispatchable(tmp_path: Path, path: str) -> None:
    client = Client(_app(tmp_path, ui_mode="global"))
    response = client.get(path)
    assert response.status_code not in NON_ROUTABLE, f"{path} -> {response.status_code}"


@pytest.mark.parametrize("path", REPORTING_PATHS)
def test_reporting_routes_are_dispatchable(tmp_path: Path, path: str) -> None:
    client = Client(_app(tmp_path, ui_mode="reporting"))
    response = client.get(path)
    assert response.status_code not in NON_ROUTABLE, f"{path} -> {response.status_code}"


@pytest.mark.parametrize("path", sorted(set(GLOBAL_PATHS) | set(REPORTING_PATHS)))
def test_full_mode_routes_are_dispatchable(tmp_path: Path, path: str) -> None:
    # "full" is the union of global + reporting (used for local dev only, never
    # deployed) — admin routes are deliberately excluded from it, see app.py's
    # `if resolved_ui_mode == "admin":` gate.
    client = Client(_app(tmp_path, ui_mode="full"))
    response = client.get(path)
    assert response.status_code not in NON_ROUTABLE, f"{path} -> {response.status_code}"
