"""Shared Athena query helpers (validation SELECTs + materializing UNLOAD)."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

_LAYER_CATALOG_REF_RE = re.compile(
    r"\b(silver_stg|silver|gold)\.([a-zA-Z_][a-zA-Z0-9_]*)\b",
    re.IGNORECASE,
)


class AthenaQueryError(RuntimeError):
    """Athena query failed or was cancelled."""


def start_query(
    *,
    query: str,
    database: str,
    workgroup: str,
    region: str | None = None,
    client: Any | None = None,
) -> str:
    """Start an Athena query; return QueryExecutionId."""
    athena = client or _client(region)
    execution = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        WorkGroup=workgroup,
    )
    return str(execution["QueryExecutionId"])


def wait_query(
    execution_id: str,
    *,
    region: str | None = None,
    client: Any | None = None,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    """Poll until SUCCEEDED; raise AthenaQueryError on failure/timeout."""
    athena = client or _client(region)
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = athena.get_query_execution(QueryExecutionId=execution_id)
        status = response["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            return response["QueryExecution"]
        if state in {"FAILED", "CANCELLED"}:
            reason = status.get("StateChangeReason", state)
            raise AthenaQueryError(f"Athena query {state.lower()}: {reason}")
        if time.monotonic() >= deadline:
            raise AthenaQueryError(f"Athena query timed out after {timeout_seconds:.0f}s")
        time.sleep(poll_seconds)


def fetch_results(
    execution_id: str,
    *,
    region: str | None = None,
    client: Any | None = None,
    max_rows: int = 1000,
) -> dict[str, Any]:
    """Return ``{columns: [...], rows: [dict, ...]}`` for a succeeded query."""
    athena = client or _client(region)
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    next_token: str | None = None
    remaining = max(0, int(max_rows))
    header_consumed = False

    while remaining > 0 or not header_consumed:
        kwargs: dict[str, Any] = {
            "QueryExecutionId": execution_id,
            "MaxResults": min(1000, max(1, remaining + (0 if header_consumed else 1))),
        }
        if next_token:
            kwargs["NextToken"] = next_token
        page = athena.get_query_results(**kwargs)
        result_rows = page.get("ResultSet", {}).get("Rows") or []
        if not header_consumed and result_rows:
            columns = [cell.get("VarCharValue", "") for cell in result_rows[0].get("Data", [])]
            result_rows = result_rows[1:]
            header_consumed = True
        for row in result_rows:
            if remaining <= 0:
                break
            values = [cell.get("VarCharValue") for cell in row.get("Data", [])]
            item = {columns[i] if i < len(columns) else f"c{i}": values[i] for i in range(len(values))}
            rows.append(item)
            remaining -= 1
        next_token = page.get("NextToken")
        if not next_token or remaining <= 0:
            break

    return {"columns": columns, "rows": rows, "execution_id": execution_id}


def run_query(
    query: str,
    *,
    database: str,
    workgroup: str,
    region: str | None = None,
    client: Any | None = None,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 600.0,
    max_rows: int = 1000,
    fetch: bool = True,
) -> dict[str, Any]:
    """Start, wait, and optionally fetch results."""
    execution_id = start_query(
        query=query,
        database=database,
        workgroup=workgroup,
        region=region,
        client=client,
    )
    execution = wait_query(
        execution_id,
        region=region,
        client=client,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )
    payload: dict[str, Any] = {
        "execution_id": execution_id,
        "status": "SUCCEEDED",
        "statistics": execution.get("Statistics") or {},
    }
    if fetch:
        results = fetch_results(
            execution_id,
            region=region,
            client=client,
            max_rows=max_rows,
        )
        payload.update(results)
    return payload


def wrap_unload(select_sql: str, s3_output_prefix: str) -> str:
    """Wrap a SELECT (or SELECT body) as Athena UNLOAD to Parquet."""
    body = select_sql.strip().rstrip(";")
    # Allow full SELECT statements or parenthesized subqueries.
    if body.upper().startswith("SELECT") or body.upper().startswith("WITH"):
        inner = body
    else:
        inner = f"SELECT * FROM ({body})"
    location = s3_output_prefix.rstrip("/") + "/"
    return (
        f"UNLOAD ({inner})\n"
        f"TO '{location}'\n"
        f"WITH (format = 'PARQUET', compression = 'SNAPPY')"
    )


def materialize_select_to_prefix(
    select_sql: str,
    *,
    s3_output_prefix: str,
    database: str,
    workgroup: str,
    region: str | None = None,
    client: Any | None = None,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """UNLOAD a SELECT to an S3 prefix (Athena writes one or more Parquet parts)."""
    query = wrap_unload(select_sql, s3_output_prefix)
    return run_query(
        query,
        database=database,
        workgroup=workgroup,
        region=region,
        client=client,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        fetch=False,
    )


def normalize_athena_catalog_refs(
    sql: str,
    *,
    source: str,
    database: str | None = None,
) -> str:
    """Rewrite ``silver_stg.entity`` / ``silver.entity`` / ``gold.output`` refs to Glue names."""
    from hiveflow.project_config import catalog_table_name, dna_catalog_table_name

    src = source.strip().lower()
    body = sql.strip()

    def _replace(match: re.Match[str]) -> str:
        layer = match.group(1).lower()
        name = match.group(2)
        low = name.lower()
        if layer == "silver_stg":
            if low.startswith("silver_stg_") or low.startswith("silver_") or low.startswith("dna_"):
                return name
            return catalog_table_name("silver_stg", src, name)
        if layer == "silver":
            if low.startswith("silver_") or low.startswith("dna_"):
                return name
            return catalog_table_name("silver", src, name)
        if low.startswith("dna_"):
            return name
        return dna_catalog_table_name(name)

    body = _LAYER_CATALOG_REF_RE.sub(_replace, body)
    if database:
        body = re.sub(re.escape(database) + r"\.", "", body, flags=re.IGNORECASE)
    return body


_FROM_TABLE_RE = re.compile(
    r"\bFROM\s+([\w]+)(?:\s+(?:AS\s+)?([\w]+))?",
    re.IGNORECASE,
)
_JOIN_TABLE_RE = re.compile(
    r"\b(?:(?:INNER|LEFT(?:\s+OUTER)?|RIGHT(?:\s+OUTER)?|FULL(?:\s+OUTER)?|CROSS)\s+)?JOIN\s+"
    r"([\w]+)(?:\s+(?:AS\s+)?([\w]+))?\s+ON\s+",
    re.IGNORECASE,
)
_TAIL_CLAUSE_RE = re.compile(
    r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET)\b",
    re.IGNORECASE,
)


def inject_validation_filters(select_sql: str, filters: list[dict[str, str]]) -> str:
    """Apply session-only validation predicates to a KPI SELECT.

    ``filters`` items: ``{fact|table, field, value}``. Predicates are injected on the
    inner query (before ``GROUP BY`` / tail clauses) so grouped KPIs filter source rows.
    Does not mutate the original SQL string passed in by the caller.
    """
    body = select_sql.strip().rstrip(";")
    predicates = _validation_predicates(body, filters)
    if not predicates:
        return body
    where = " AND ".join(predicates)
    return _inject_where_clause(body, where)


def _validation_predicates(sql: str, filters: list[dict[str, str]]) -> list[str]:
    aliases = _table_aliases(sql)
    predicates: list[str] = []
    for item in filters:
        field = str(item.get("field") or "").strip()
        value = str(item.get("value") or "").strip()
        fact = str(item.get("fact") or item.get("table") or "").strip()
        if not field or not value:
            continue
        column = _qualify_validation_column(field, fact, aliases)
        if not _safe_ident(column):
            raise ValueError(f"Invalid filter field: {field!r}")
        predicates.append(f"{column} = {_sql_literal(value)}")
    return predicates


def _qualify_validation_column(
    field: str,
    fact: str,
    aliases: dict[str, str],
) -> str:
    if "." in field:
        return field
    if fact:
        alias = _alias_for_fact(fact, aliases)
        if alias:
            return f"{alias}.{field}"
    return field


def _alias_for_fact(fact: str, aliases: dict[str, str]) -> str:
    fact_low = fact.strip().lower()
    if not fact_low:
        return ""
    if fact_low in aliases:
        table = aliases[fact_low]
        return _preferred_alias(table, aliases)
    target_table = ""
    for table in set(aliases.values()):
        table_low = table.lower()
        if table_low == fact_low or table_low.endswith(f"_{fact_low}"):
            target_table = table_low
            break
    if not target_table:
        return ""
    return _preferred_alias(target_table, aliases)


def _preferred_alias(table: str, aliases: dict[str, str]) -> str:
    table_low = table.lower()
    candidates = [alias for alias, mapped in aliases.items() if mapped == table_low]
    short_aliases = [alias for alias in candidates if alias != table_low]
    if short_aliases:
        return sorted(short_aliases, key=len)[0]
    return table_low


def _table_aliases(sql: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for match in _FROM_TABLE_RE.finditer(sql):
        table = str(match.group(1) or "").strip()
        alias = str(match.group(2) or table).strip()
        if table:
            aliases[table.lower()] = table.lower()
        if alias:
            aliases[alias.lower()] = table.lower()
    for match in _JOIN_TABLE_RE.finditer(sql):
        table = str(match.group(1) or "").strip()
        alias = str(match.group(2) or table).strip()
        if table:
            aliases[table.lower()] = table.lower()
        if alias:
            aliases[alias.lower()] = table.lower()
    return aliases


def _paren_depth_at(sql: str, index: int) -> int:
    return sql[:index].count("(") - sql[:index].count(")")


def _find_outer_keyword(sql: str, keyword: str) -> int | None:
    pattern = re.compile(rf"\b{keyword}\b", re.IGNORECASE)
    for match in pattern.finditer(sql):
        if _paren_depth_at(sql, match.start()) == 0:
            return match.start()
    return None


def _find_tail_clause_start(sql: str) -> int | None:
    for match in _TAIL_CLAUSE_RE.finditer(sql):
        if _paren_depth_at(sql, match.start()) == 0:
            return match.start()
    return None


def _inject_where_clause(sql: str, where_clause: str) -> str:
    tail_start = _find_tail_clause_start(sql)
    head = sql[:tail_start].rstrip() if tail_start is not None else sql.rstrip()
    tail = sql[tail_start:].lstrip() if tail_start is not None else ""
    if _find_outer_keyword(head, "WHERE") is not None:
        head = f"{head} AND {where_clause}"
    else:
        head = f"{head} WHERE {where_clause}"
    if tail:
        return f"{head} {tail}"
    return head


def _safe_ident(name: str) -> bool:
    if not name or len(name) > 128:
        return False
    parts = name.split(".")
    if not parts or any(not part for part in parts):
        return False
    for part in parts:
        if not part[0].isalpha() and part[0] != "_":
            return False
        for ch in part[1:]:
            if not (ch.isalnum() or ch == "_"):
                return False
    return True


def _sql_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _client(region: str | None) -> Any:
    from hiveflow.storage.aws import athena_client

    return athena_client(region=region)
