"""Public marketing pages for HiveFlowAI."""

from __future__ import annotations

from collections.abc import Callable

from werkzeug.wrappers import Request, Response

from hiveflow.dna.web.routing_helpers import _app_url
from hiveflow.dna.web.templating import render_template
from hiveflow.dna.web.theme import page_header, render_public_page

PUBLIC_NAV = (
    ("/", "Home"),
    ("/platform", "Platform"),
    ("/pricing", "Pricing"),
    ("/portal/login", "Client login"),
)

PRICING_PLANS = (
    {
        "title": "HiveFlowAI · DNA Beta",
        "featured": True,
        "offer_badge": "Current offer",
        "price": "$0",
        "price_unit": "implementation",
        "price_sub": "$100 / month",
        "bullets": (
            "Starter KPI library on your Business Central data",
            "Governed definition pack and client reporting portal",
            "Direct product feedback channel — limited beta slots",
        ),
    },
    {
        "title": "HiveFlowAI · DNA (GA target)",
        "featured": False,
        "offer_badge": None,
        "price": "$5,000",
        "price_unit": "implementation",
        "price_sub": "$1,000 / month",
        "bullets": (
            "Self-service KPIs and reports via documented requirements",
            "Version-controlled DNA + Reporting engines",
            "Target pricing — subject to delivery cost at scale",
        ),
    },
    {
        "title": "HiveFlow Signals",
        "featured": False,
        "offer_badge": None,
        "price": "$4,000",
        "price_unit": "activation",
        "price_sub": "$600 / month",
        "bullets": (
            "Ranked exception queues and operational briefings",
            "Up to 3 systems and 5 named users",
            "Best for HVAC, thin-stack, and to-do-first workflows",
        ),
    },
)


def _url(request: Request) -> Callable[[str], str]:
    return lambda path: _app_url(request, path)


def _public_response(request: Request, *, title: str, active_path: str, body: str) -> Response:
    return Response(
        render_public_page(
            title=title, active_path=active_path, body=body, nav_links=PUBLIC_NAV, url=_url(request)
        ),
        mimetype="text/html",
    )


def render_landing(request: Request) -> Response:
    url = _url(request)
    body = render_template(
        "public/landing.html", pricing_url=url("/pricing"), platform_url=url("/platform")
    )
    return _public_response(request, title="Home", active_path="/", body=body)


def render_platform(request: Request) -> Response:
    body = page_header(
        "Platform",
        "Four governed layers — from raw source data to client-ready reporting — with clear separation between data refresh and semantic change.",
        eyebrow="How it works",
    )
    body += render_template("public/platform.html")
    return _public_response(request, title="Platform", active_path="/platform", body=body)


def render_pricing(request: Request) -> Response:
    body = page_header(
        "Pricing",
        "DNA is in beta — early customers help shape the product. GA pricing is the target once the platform is production-ready.",
        eyebrow="Plans",
    )
    body += render_template("public/pricing.html", plans=PRICING_PLANS)
    return _public_response(request, title="Pricing", active_path="/pricing", body=body)
