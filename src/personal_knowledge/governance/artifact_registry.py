"""Typed D/S/R/A artifact registry validation.

Tracked registry files contain metadata only. Runtime versions and private payloads
remain in private SQLite/data roots.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

import yaml

from personal_knowledge.core.project_paths import ROOT


DEFAULT_REGISTRY = ROOT / "governance" / "policies" / "artifact_layers.yaml"
LAYERS = frozenset({"D", "S", "R", "A"})
PRIVACY_CLASSES = frozenset({"R1", "R2", "R3", "R4"})
REQUIRED_FIELDS = frozenset(
    {
        "id", "layer", "kind", "authority_role", "producer", "consumers",
        "privacy", "evidence_parent", "lifecycle", "version_source",
    }
)
_ID_RE = re.compile(r"^[dsra]\.[a-z][a-z0-9_]*$")
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s${][^\s]*"
)
_PAYLOAD_FIELDS = frozenset(
    {"content", "body", "raw_text", "evidence_quote", "prompt", "response_text"}
)


@dataclass(frozen=True)
class RegistryIssue:
    code: str
    message: str
    artifact_id: str | None = None


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("artifact registry root must be a mapping")
    return doc


def _dependency_allowed(parent_layer: str, child_layer: str) -> bool:
    # Evidence/dependency must point down or stay in the same layer. R and A are
    # terminal consumers and cannot become evidence parents for D/S truth.
    allowed_parents = {
        "D": {"D"},
        "S": {"D", "S"},
        "R": {"D", "S", "R"},
        "A": {"D", "S", "R", "A"},
    }
    return parent_layer in allowed_parents.get(child_layer, set())


def validate_registry(doc: dict[str, Any]) -> list[RegistryIssue]:
    issues: list[RegistryIssue] = []
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, list):
        return [RegistryIssue("invalid_root", "artifacts must be a list")]

    ids: dict[str, dict[str, Any]] = {}
    roles: dict[str, str] = {}
    for raw in artifacts:
        if not isinstance(raw, dict):
            issues.append(RegistryIssue("invalid_entry", "artifact entry must be a mapping"))
            continue
        aid = str(raw.get("id") or "")
        missing = sorted(REQUIRED_FIELDS - set(raw))
        if missing:
            issues.append(RegistryIssue("missing_fields", f"missing: {', '.join(missing)}", aid or None))
        if not _ID_RE.fullmatch(aid):
            issues.append(RegistryIssue("invalid_id", "id must use d./s./r./a. namespace", aid or None))
        if aid in ids:
            issues.append(RegistryIssue("duplicate_id", "artifact id is duplicated", aid))
        else:
            ids[aid] = raw
        layer = str(raw.get("layer") or "")
        if layer not in LAYERS or (aid and aid[0].upper() != layer):
            issues.append(RegistryIssue("invalid_layer", "layer must match id namespace", aid or None))
        role = str(raw.get("authority_role") or "")
        if role in roles:
            issues.append(RegistryIssue("duplicate_authority", f"role already owned by {roles[role]}", aid or None))
        elif role:
            roles[role] = aid
        if raw.get("privacy") not in PRIVACY_CLASSES:
            issues.append(RegistryIssue("invalid_privacy", "privacy must be R1..R4", aid or None))
        if not isinstance(raw.get("consumers"), list):
            issues.append(RegistryIssue("invalid_consumers", "consumers must be a list", aid or None))
        payload_keys = sorted(_PAYLOAD_FIELDS & set(raw))
        if payload_keys:
            issues.append(RegistryIssue("private_payload", f"payload fields forbidden: {', '.join(payload_keys)}", aid or None))
        if _SECRET_RE.search(yaml.safe_dump(raw, allow_unicode=True)):
            issues.append(RegistryIssue("secret_like_value", "secret-like value is forbidden", aid or None))

    for aid, raw in ids.items():
        parent = str(raw.get("evidence_parent") or "")
        # External raw source roots are intentionally not tracked as product artifacts.
        if parent in {"d.agentsview_snapshot", "d.google_raw", "d.external_public_source"}:
            continue
        target = ids.get(parent)
        if target is None:
            issues.append(RegistryIssue("unknown_dependency", f"unknown evidence_parent: {parent}", aid))
            continue
        if not _dependency_allowed(str(target.get("layer")), str(raw.get("layer"))):
            issues.append(RegistryIssue("invalid_dependency", f"{raw.get('layer')} cannot depend on {target.get('layer')}", aid))

    for required in doc.get("required_serving_roles") or []:
        if required not in roles:
            issues.append(RegistryIssue("missing_serving_role", f"required role not registered: {required}"))
    return issues


def registry_report(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    doc = load_registry(path)
    issues = validate_registry(doc)
    return {
        "ok": not issues,
        "path": str(path),
        "artifacts": len(doc.get("artifacts") or []),
        "issues": [issue.__dict__ for issue in issues],
    }


def artifact_ids(doc: dict[str, Any]) -> Iterable[str]:
    return (str(row["id"]) for row in doc.get("artifacts") or [] if isinstance(row, dict) and row.get("id"))
