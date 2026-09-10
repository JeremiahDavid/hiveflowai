"""Governance config proposals — staged DNA/reporting edits before pin."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from hiveflow.compat import UTC
from difflib import SequenceMatcher, unified_diff
from typing import Any

import yaml

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import (
    read_json_artifact,
    read_text_artifact,
    write_json_artifact,
    write_text_artifact,
)
from hiveflow.storage.paths import (
    governance_proposal_conversation_key,
    governance_proposal_dna_key,
    governance_proposal_meta_key,
    governance_proposal_reporting_key,
    governance_proposals_prefix,
)

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(str(version).strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_semver(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def bump_patch_version(version: str) -> str:
    parsed = parse_semver(version)
    if not parsed:
        return "1.0.1"
    major, minor, patch = parsed
    return format_semver((major, minor, patch + 1))


def bump_minor_version(version: str) -> str:
    parsed = parse_semver(version)
    if not parsed:
        return "1.1.0"
    major, minor, _patch = parsed
    return format_semver((major, minor + 1, 0))


def bump_major_version(version: str) -> str:
    parsed = parse_semver(version)
    if not parsed:
        return "2.0.0"
    major, _minor, _patch = parsed
    return format_semver((major + 1, 0, 0))


def max_semver(*versions: str) -> str:
    """Return the highest valid semver among values; fall back to the first non-empty string."""
    best: tuple[int, int, int] | None = None
    best_raw = ""
    for raw in versions:
        text = str(raw or "").strip()
        if not text:
            continue
        if not best_raw:
            best_raw = text
        parsed = parse_semver(text)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best = parsed
            best_raw = text
    return best_raw


def classify_manual_version_bump(base_version: str, proposed_version: str) -> dict[str, str]:
    """Classify a manual governance version change.

    Patch bumps must be exactly the next +1 on the third segment.
    Minor (and major) bumps are allowed; patch resets to 0 and further
    versions continue from the new line.
    """
    base = str(base_version or "").strip()
    proposed = str(proposed_version or "").strip()
    if not proposed:
        return {
            "kind": "invalid",
            "error": "Version is required.",
            "warning": "",
            "suggested_patch": bump_patch_version(base),
            "suggested_minor": bump_minor_version(base),
            "suggested_major": bump_major_version(base),
        }
    if parse_semver(proposed) is None:
        return {
            "kind": "invalid",
            "error": "Version must be semver major.minor.patch (for example 1.0.1).",
            "warning": "",
            "suggested_patch": bump_patch_version(base),
            "suggested_minor": bump_minor_version(base),
            "suggested_major": bump_major_version(base),
        }

    base_parts = parse_semver(base)
    if base_parts is None:
        return {
            "kind": "patch",
            "error": "",
            "warning": "",
            "suggested_patch": proposed,
            "suggested_minor": proposed,
            "suggested_major": proposed,
        }

    next_patch = bump_patch_version(base)
    next_minor = bump_minor_version(base)
    next_major = bump_major_version(base)
    if proposed == next_patch:
        return {
            "kind": "patch",
            "error": "",
            "warning": "",
            "suggested_patch": next_patch,
            "suggested_minor": next_minor,
            "suggested_major": next_major,
        }
    if proposed == next_minor:
        return {
            "kind": "minor",
            "error": "",
            "warning": (
                f"Minor bump from v{base} to v{proposed}: the patch number resets to 0, "
                f"and all further versions will continue from v{proposed.rsplit('.', 1)[0]}.x."
            ),
            "suggested_patch": next_patch,
            "suggested_minor": next_minor,
            "suggested_major": next_major,
        }
    if proposed == next_major:
        return {
            "kind": "major",
            "error": "",
            "warning": (
                f"Major bump from v{base} to v{proposed}: minor and patch reset to 0, "
                f"and all further versions will continue from v{proposed.split('.', 1)[0]}.x.x."
            ),
            "suggested_patch": next_patch,
            "suggested_minor": next_minor,
            "suggested_major": next_major,
        }
    return {
        "kind": "invalid",
        "error": (
            f"Version must be the next patch ({next_patch}), next minor "
            f"({next_minor}), or next major ({next_major}). "
            f"Got v{proposed} from base v{base}."
        ),
        "warning": "",
        "suggested_patch": next_patch,
        "suggested_minor": next_minor,
        "suggested_major": next_major,
    }


def new_proposal_id() -> str:
    return uuid.uuid4().hex[:12]


def build_yaml_diff_lines(before: str, after: str) -> list[dict[str, Any]]:
    """Full-file diff rows for UI: context, del (removed), add (added).

    Replace hunks list removals before additions, like GitHub unified diffs.
    Changed rows share a positive ``hunk`` id for in-page navigation.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = SequenceMatcher(None, before_lines, after_lines)
    raw: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in before_lines[i1:i2]:
                raw.append({"kind": "context", "text": line})
        elif tag == "delete":
            for line in before_lines[i1:i2]:
                raw.append({"kind": "del", "text": line})
        elif tag == "insert":
            for line in after_lines[j1:j2]:
                raw.append({"kind": "add", "text": line})
        elif tag == "replace":
            for line in before_lines[i1:i2]:
                raw.append({"kind": "del", "text": line})
            for line in after_lines[j1:j2]:
                raw.append({"kind": "add", "text": line})

    hunk = 0
    prev_change = False
    for entry in raw:
        is_change = entry["kind"] in {"del", "add"}
        if is_change and not prev_change:
            hunk += 1
        entry["hunk"] = hunk if is_change else 0
        prev_change = is_change
    return raw


def unified_yaml_diff(before: str, after: str, *, from_label: str, to_label: str) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    if before_lines and not before_lines[-1].endswith("\n"):
        before_lines[-1] += "\n"
    if after_lines and not after_lines[-1].endswith("\n"):
        after_lines[-1] += "\n"
    return "".join(
        unified_diff(
            before_lines,
            after_lines,
            fromfile=from_label,
            tofile=to_label,
            n=0,
            lineterm="",
        )
    )


def dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def normalize_yaml_for_compare(text: str) -> str:
    """Canonicalize YAML for equality checks, ignoring the top-level version field."""
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return text.strip()
    if not isinstance(payload, dict):
        return text.strip()
    normalized = dict(payload)
    normalized.pop("version", None)
    return dump_yaml(normalized)


def yaml_content_changed(before: str, after: str) -> bool:
    return normalize_yaml_for_compare(before) != normalize_yaml_for_compare(after)


def load_open_proposal_id(settings: DnaSettings) -> str | None:
    """Return the most recent open proposal id, if any."""
    pack_id = settings.dna_config_id
    prefix = governance_proposals_prefix(pack_id).rstrip("/") + "/"
    candidates: list[tuple[str, str]] = []

    if settings.s3_bucket:
        from hiveflow.storage.aws import s3_client

        client = s3_client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = str(obj.get("Key") or "")
                if not key.endswith("/meta.json"):
                    continue
                proposal_id = key[len(prefix) :].split("/", 1)[0]
                meta = read_json_artifact(settings, key)
                if meta and meta.get("status") in {"open", "running"}:
                    candidates.append((str(meta.get("updated_at") or meta.get("created_at") or ""), proposal_id))
    else:
        from hiveflow.storage.paths import prefix_path

        root = prefix_path(settings.data_dir, governance_proposals_prefix(pack_id))
        if root.is_dir():
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                meta = read_json_artifact(
                    settings, governance_proposal_meta_key(pack_id, child.name)
                )
                if meta and meta.get("status") in {"open", "running"}:
                    candidates.append(
                        (str(meta.get("updated_at") or meta.get("created_at") or ""), child.name)
                    )

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def load_proposal(settings: DnaSettings, proposal_id: str) -> dict[str, Any] | None:
    pack_id = settings.dna_config_id
    meta = read_json_artifact(settings, governance_proposal_meta_key(pack_id, proposal_id))
    if not meta:
        return None
    dna_text = read_text_artifact(settings, governance_proposal_dna_key(pack_id, proposal_id)) or ""
    reporting_text = (
        read_text_artifact(settings, governance_proposal_reporting_key(pack_id, proposal_id)) or ""
    )
    conversation = read_json_artifact(
        settings, governance_proposal_conversation_key(pack_id, proposal_id)
    ) or {"messages": []}
    return {
        "meta": meta,
        "dna_yaml": dna_text,
        "reporting_yaml": reporting_text,
        "conversation": conversation,
    }


def save_proposal(
    settings: DnaSettings,
    *,
    proposal_id: str,
    meta: dict[str, Any],
    dna_yaml: str,
    reporting_yaml: str,
    conversation: dict[str, Any],
) -> dict[str, Any]:
    pack_id = settings.dna_config_id
    now = datetime.now(UTC).isoformat()
    meta = dict(meta)
    meta.setdefault("proposal_id", proposal_id)
    meta.setdefault("created_at", now)
    meta["updated_at"] = now
    meta["pack_id"] = pack_id
    meta["company"] = settings.company

    write_json_artifact(settings, governance_proposal_meta_key(pack_id, proposal_id), meta)
    write_text_artifact(
        settings,
        governance_proposal_dna_key(pack_id, proposal_id),
        dna_yaml,
        content_type="application/yaml; charset=utf-8",
    )
    write_text_artifact(
        settings,
        governance_proposal_reporting_key(pack_id, proposal_id),
        reporting_yaml,
        content_type="application/yaml; charset=utf-8",
    )
    write_json_artifact(
        settings,
        governance_proposal_conversation_key(pack_id, proposal_id),
        conversation,
    )
    return meta


def proposal_diffs(
    *,
    base_dna_yaml: str,
    base_reporting_yaml: str,
    proposed_dna_yaml: str,
    proposed_reporting_yaml: str,
    base_version: str,
    next_version: str,
    dna_base_version: str | None = None,
    dna_next_version: str | None = None,
    reporting_base_version: str | None = None,
    reporting_next_version: str | None = None,
) -> dict[str, str]:
    dna_from = dna_base_version or base_version
    dna_to = dna_next_version or next_version
    reporting_from = reporting_base_version or base_version
    reporting_to = reporting_next_version or next_version
    dna_diff = ""
    reporting_diff = ""
    if yaml_content_changed(base_dna_yaml, proposed_dna_yaml):
        dna_diff = unified_yaml_diff(
            base_dna_yaml,
            proposed_dna_yaml,
            from_label=f"dna@v{dna_from}",
            to_label=f"dna@v{dna_to}",
        )
    if yaml_content_changed(base_reporting_yaml, proposed_reporting_yaml):
        reporting_diff = unified_yaml_diff(
            base_reporting_yaml,
            proposed_reporting_yaml,
            from_label=f"reporting@v{reporting_from}",
            to_label=f"reporting@v{reporting_to}",
        )
    return {"dna": dna_diff, "reporting": reporting_diff}
