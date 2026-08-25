"""HiveFlowAI presentation layer — dark dashboard theme and layout helpers."""

from __future__ import annotations

import hashlib
import html
import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from markupsafe import Markup

from hiveflow.dna.web.templating import render_template

BRAND_NAME = "HiveFlowAI"
TAGLINE = "Connect. Unify. Reveal."
PRODUCT_SUBTITLE = "Operational intelligence · governed metrics"

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Legacy alias kept for tests importing NAV_LINKS
NAV_LINKS = (
    ("/portal/governance", "Governance"),
)

MIME_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".js": "application/javascript",
    ".ico": "image/x-icon",
}

# Content types that must be base64-encoded for API Gateway REST + awsgi.
BINARY_STATIC_CONTENT_TYPES = frozenset(
    mime for mime in MIME_TYPES.values() if mime.startswith("image/")
)


@lru_cache(maxsize=None)
def _static_asset_version(filename: str) -> str:
    try:
        data = (STATIC_DIR / filename).read_bytes()
    except OSError:
        return "0"
    return hashlib.sha256(data).hexdigest()[:10]


def _static_url(url: Callable[[str], str], filename: str) -> str:
    """Static asset URL with a content-hash cache-buster.

    Prod serves static responses with `Cache-Control: public, max-age=86400`.
    A prior version of this cache-buster used the file's mtime, but Lambda
    deployment zips normalize every file's mtime to a fixed epoch
    (1980-01-01) for reproducible asset hashing — so in AWS the `?v=` value
    was identical on every deploy regardless of content changes, and a
    real edit to theme.css (or any static asset) would sit invisible behind
    the browser's 24h cache. Hashing the file's bytes instead guarantees the
    URL changes exactly when — and only when — the content does.
    """
    version = _static_asset_version(filename)
    return url(f"/static/{filename}?v={version}")


def brand_home_href(url: Callable[[str], str]) -> str:
    """Marketing site root — use primary hostname on reporting subdomains."""
    primary = os.getenv("HIVEFLOW_PRIMARY_SITE_URL", "").strip().rstrip("/")
    if primary:
        return f"{primary}/"
    return url("/")


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _side_nav_link_active(href: str, active_path: str) -> bool:
    href_norm = href.rstrip("/") or "/"
    path_norm = active_path.split("?")[0].rstrip("/") or "/"
    return href_norm == path_norm


def _nav_abbrev(label: str) -> str:
    words = [part for part in str(label).split() if part]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    cleaned = str(label).strip()
    return cleaned[:2].upper() if cleaned else "•"


def _nav_item_has_active_descendant(item: Any, active_path: str) -> bool:
    if _side_nav_link_active(item[0], active_path):
        return True
    if len(item) > 2:
        return any(_nav_item_has_active_descendant(child, active_path) for child in item[2])
    return False


def _side_nav_tree(
    items: tuple[Any, ...],
    active_path: str,
    url: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Build a plain nested-dict tree for the recursive Jinja side-nav macro.

    Mirrors the old `_render_side_nav_item` recursion (open/active/ancestor
    logic) but precomputes it in Python so the template stays pure markup.
    """
    nodes: list[dict[str, Any]] = []
    for item in items:
        href = item[0]
        label = item[1]
        children: tuple[Any, ...] = item[2] if len(item) > 2 else ()
        is_active = _side_nav_link_active(href, active_path)
        descendant_active = any(
            _nav_item_has_active_descendant(child, active_path) for child in children
        )
        nodes.append(
            {
                "href": url(href),
                "label": label,
                "abbrev": _nav_abbrev(label),
                "active": is_active,
                "is_ancestor": descendant_active and not is_active,
                "open": descendant_active or is_active,
                "children": _side_nav_tree(children, active_path, url) if children else [],
            }
        )
    return nodes


def _flatten_nav_paths(data_menu: tuple[Any, ...]) -> set[str]:
    paths: set[str] = set()

    def _walk(item: Any) -> None:
        paths.add(item[0])
        if len(item) > 2:
            for child in item[2]:
                _walk(child)

    for entry in data_menu:
        _walk(entry)
    return paths


def _nav_items(
    active_path: str,
    url: Callable[[str], str],
    nav_links: tuple[tuple[str, str], ...],
    *,
    data_menu: tuple[Any, ...] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if data_menu:
        data_paths = _flatten_nav_paths(data_menu)
        data_root = data_menu[0][0]
        items.append(
            {
                "href": url(data_root),
                "label": "Reporting",
                "active": active_path in data_paths,
            }
        )
    for href, label in nav_links:
        items.append({"href": url(href), "label": label, "active": href == active_path})
    return items


def page_header(title: str, subtitle: str = "", *, eyebrow: str = "") -> str:
    return render_template("_page_header.html", title=title, subtitle=subtitle, eyebrow=eyebrow)


def badge_row(*badges: tuple[str, bool]) -> str:
    return render_template("_badge_row.html", badges=badges)


def empty_state(title: str, message: str) -> str:
    return render_template("_empty_state.html", title=title, message=message)


def render_page(
    *,
    title: str,
    active_path: str,
    body: str,
    page_title: str | None = None,
    url: Callable[[str], str] | None = None,
    nav_links: tuple[tuple[str, str], ...] | None = None,
) -> str:
    return render_public_page(
        title=title,
        active_path=active_path,
        body=body,
        page_title=page_title,
        url=url,
        nav_links=nav_links or NAV_LINKS,
    )


def _layout_shell(
    *,
    title: str,
    body: str,
    active_path: str,
    nav_links: tuple[tuple[str, str], ...],
    url: Callable[[str], str],
    page_title: str | None = None,
    topbar_extra: str = "",
    footer_left: str | None = None,
    client_accent: str | None = None,
    data_menu: tuple[Any, ...] | None = None,
    side_nav_title: str | None = None,
    side_nav_items: tuple[Any, ...] | None = None,
    side_nav_id: str | None = None,
    sidebar_active_path: str | None = None,
    charts_assets: str = "",
    brand_href: str | None = None,
) -> str:
    footer_text = footer_left or f"{BRAND_NAME} · {PRODUCT_SUBTITLE}"
    side_nav: dict[str, Any] | None = None
    if side_nav_title and side_nav_items and side_nav_id:
        side_nav = {
            "nav_id": side_nav_id,
            "title": side_nav_title,
            # NB: not "items" — dict.items() shadows a same-named key when
            # Jinja resolves `side_nav.items` via getattr-then-subscript.
            "links": _side_nav_tree(side_nav_items, sidebar_active_path or active_path, url),
        }
    home_href = brand_href if brand_href is not None else brand_home_href(url)
    return render_template(
        "_layout.html",
        window_title=page_title or title,
        brand_name=BRAND_NAME,
        tagline=TAGLINE,
        icon_url=_static_url(url, "hiveflowai-logo.svg"),
        icon_url_dark=_static_url(url, "hiveflowai-logo-reversed.svg"),
        favicon_url=_static_url(url, "hiveflowai-logo-mono.svg"),
        css_url=_static_url(url, "theme.css"),
        client_accent=client_accent,
        home_href=home_href,
        nav_items=_nav_items(active_path, url, nav_links, data_menu=data_menu),
        topbar_extra=Markup(topbar_extra),
        side_nav=side_nav,
        shell_class="shell shell-with-sidebar" if side_nav else "shell",
        body=Markup(body),
        footer_text=footer_text,
        charts_assets=Markup(charts_assets),
    )


def render_public_page(
    *,
    title: str,
    active_path: str,
    body: str,
    nav_links: tuple[tuple[str, str], ...],
    page_title: str | None = None,
    url: Callable[[str], str] | None = None,
) -> str:
    link = url or (lambda path: path)
    return _layout_shell(
        title=title,
        body=body,
        active_path=active_path,
        nav_links=nav_links,
        url=link,
        page_title=page_title,
    )


def render_portal_page(
    *,
    title: str,
    active_path: str,
    body: str,
    nav_links: tuple[tuple[str, str], ...],
    client: Any,
    page_title: str | None = None,
    url: Callable[[str], str] | None = None,
    data_menu: tuple[Any, ...] | None = None,
    side_nav_title: str | None = None,
    side_nav_items: tuple[Any, ...] | None = None,
    side_nav_id: str | None = None,
    sidebar_active_path: str | None = None,
    charts_assets: str = "",
) -> str:
    link = url or (lambda path: path)
    topbar_extra = f'<span class="portal-badge">{escape(client.display_name)}</span>'
    topbar_extra += f'<a class="nav-link" href="{escape(link("/portal/logout"))}">Sign out</a>'
    return _layout_shell(
        title=title,
        body=body,
        active_path=active_path,
        nav_links=nav_links,
        url=link,
        page_title=page_title,
        topbar_extra=topbar_extra,
        footer_left=f"{client.display_name} · Client portal",
        client_accent=getattr(client, "accent_color", None),
        data_menu=data_menu,
        side_nav_title=side_nav_title,
        side_nav_items=side_nav_items,
        side_nav_id=side_nav_id,
        sidebar_active_path=sidebar_active_path,
        charts_assets=charts_assets,
        brand_href=link("/portal"),
    )


def render_login_page(
    *,
    url: Callable[[str], str],
    error: str = "",
    success: str = "",
    next_path: str = "/portal",
    mode: str = "sign_in",
    username: str = "",
    session: str = "",
    client_id: str = "",
    client_id_locked: bool = False,
) -> str:
    login_query = {"next": next_path}
    if client_id:
        login_query["client_id"] = client_id
    if client_id_locked:
        login_query["client_id_locked"] = "1"
    forgot_href = url(f"/portal/login?{urlencode({**login_query, 'mode': 'forgot_password'})}")
    sign_in_href = url(f"/portal/login?{urlencode(login_query)}")

    if mode == "set_password":
        page_title = "Set password"
    elif mode == "forgot_password":
        page_title = "Forgot password"
    elif mode == "reset_password":
        page_title = "Reset password"
    else:
        page_title = "Client login"

    body = render_template(
        "login.html",
        mode=mode,
        error=error,
        success=success,
        next_path=next_path,
        username=username,
        session=session,
        client_id=client_id,
        client_id_locked=client_id_locked,
        show_client_id_field=mode in {"sign_in", "set_password"},
        login_post_url=url("/portal/login"),
        forgot_href=forgot_href,
        sign_in_href=sign_in_href,
        brand_home_href=brand_home_href(url),
    )
    return _layout_shell(
        title="Client login",
        body=body,
        active_path="/portal/login",
        nav_links=(("/", "Home"), ("/pricing", "Pricing"), ("/portal/login", "Client login")),
        url=url,
        page_title=page_title,
    )
