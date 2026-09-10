"""AWS client seam for tenant-scoped credentials.

Data access (S3 lake, Athena, Glue, Step Functions, the source-docs gold Lambda)
must run with the *tenant's* credentials in the multi-tenant portal, not the
shared Lambda's own role. Callers get their boto3 clients from here; a request
that has assumed a tenant role installs that session via ``use_boto_session`` and
every client built for the duration of the ``with`` block uses it. With no
session installed (Glue jobs, single-tenant Lambdas, local dev, tests) the
default boto3 credential chain is used, exactly as before.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Any, Iterator

_boto_session: ContextVar[Any | None] = ContextVar("hiveflow_boto_session", default=None)


def current_boto_session() -> Any | None:
    """The boto3 Session installed for the current context, or None."""
    return _boto_session.get()


@contextlib.contextmanager
def use_boto_session(session: Any | None) -> Iterator[None]:
    """Install ``session`` as the AWS client source for the duration of the block."""
    token = _boto_session.set(session)
    try:
        yield
    finally:
        _boto_session.reset(token)


def _client(service: str, *, region: str | None = None) -> Any:
    import boto3

    kwargs: dict[str, Any] = {"region_name": region} if region else {}
    session = _boto_session.get()
    if session is not None:
        return session.client(service, **kwargs)
    return boto3.client(service, **kwargs)


def s3_client(*, region: str | None = None) -> Any:
    return _client("s3", region=region)


def athena_client(*, region: str | None = None) -> Any:
    return _client("athena", region=region)


def glue_client(*, region: str | None = None) -> Any:
    return _client("glue", region=region)


def sfn_client(*, region: str | None = None) -> Any:
    return _client("stepfunctions", region=region)


def lambda_client(*, region: str | None = None) -> Any:
    return _client("lambda", region=region)
