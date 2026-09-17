from __future__ import annotations

VALID_CDK_SCOPES = frozenset({"all", "ingest", "platform"})


def resolve_cdk_scope(*, context: str | None = None, env: str | None = None) -> str:
    scope = (context or env or "all").strip().lower()
    if scope not in VALID_CDK_SCOPES:
        allowed = ", ".join(sorted(VALID_CDK_SCOPES))
        raise ValueError(f"Invalid CDK scope {scope!r}. Expected one of: {allowed}")
    return scope
