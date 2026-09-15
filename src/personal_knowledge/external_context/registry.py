"""Strict two-source registry and metadata-only local read service."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, ROOT
from .schema import SourceDefinition, canonical_json, checksum


DEFAULT_REGISTRY = ROOT / "governance" / "policies" / "external_sources.yaml"
INTERFACE_SCHEMA_VERSION = "external_context_interface_v1"
REQUIRED_FIELDS = frozenset({
    "id", "authority_role", "owner", "source_type", "topic", "license",
    "provenance", "region", "publication_time_policy", "valid_time_policy",
    "observed_time_policy", "ingestion_time_policy", "quality_policy_version",
    "endpoint", "retention_policy",
})
BODY_KEYS = frozenset({
    "body", "content", "raw", "raw_text", "full_text", "html", "markdown",
    "document", "prompt", "response_text", "evidence_quote",
})
SECRET_KEYS = frozenset({"api_key", "access_token", "token", "password", "secret", "cookie"})
SOURCE_ID_RE = re.compile(r"^ext\.[a-z][a-z0-9_]*$")
ALLOWED_SOURCE_HOSTS = {
    "ext.python_releases": "www.python.org",
    "ext.nodejs_releases": "nodejs.org",
}
ALLOWED_SOURCE_TYPES = frozenset({"official_release_index"})
ALLOWED_REGIONS = frozenset({"global"})
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|secret|cookie)\s*[:=]\s*\S+"
)


class ExternalSourceRegistryError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExternalSourceRegistryError("registry_unreadable", str(exc)) from exc
    if not isinstance(value, dict):
        raise ExternalSourceRegistryError("invalid_registry_root")
    return value


def _walk(value: Any, path: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            found.append((child_path, child))
            found.extend(_walk(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk(child, f"{path}[{index}]"))
    return found


def validate_registry(doc: Mapping[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    sources = doc.get("sources")
    if not isinstance(sources, list):
        return [{"code": "invalid_sources", "detail": "sources must be a list"}]
    if len(sources) != 2:
        issues.append({"code": "source_count", "detail": "exactly two sources are required"})
    ids: set[str] = set()
    roles: set[str] = set()
    for index, raw in enumerate(sources):
        label = str(raw.get("id") if isinstance(raw, Mapping) else index)
        if not isinstance(raw, Mapping):
            issues.append({"code": "invalid_source", "detail": label})
            continue
        missing = sorted(REQUIRED_FIELDS - set(raw))
        if missing:
            issues.append({"code": "missing_fields", "detail": f"{label}:{','.join(missing)}"})
        source_id = str(raw.get("id") or "")
        role = str(raw.get("authority_role") or "")
        if not SOURCE_ID_RE.fullmatch(source_id):
            issues.append({"code": "invalid_source_id", "detail": source_id})
        if source_id not in ALLOWED_SOURCE_HOSTS:
            issues.append({"code": "source_not_allowlisted", "detail": source_id})
        if source_id in ids:
            issues.append({"code": "duplicate_source", "detail": source_id})
        ids.add(source_id)
        if not role or role in roles:
            issues.append({"code": "duplicate_authority", "detail": role})
        roles.add(role)
        if raw.get("topic") != "project/technology":
            issues.append({"code": "invalid_topic", "detail": label})
        if raw.get("source_type") not in ALLOWED_SOURCE_TYPES:
            issues.append({"code": "invalid_source_type", "detail": label})
        if raw.get("region") not in ALLOWED_REGIONS:
            issues.append({"code": "invalid_region", "detail": label})
        for field in (
            "owner", "source_type", "license", "provenance", "region",
            "publication_time_policy", "valid_time_policy", "observed_time_policy",
            "ingestion_time_policy", "quality_policy_version", "retention_policy",
        ):
            if field in raw and (not isinstance(raw[field], str) or not raw[field].strip()):
                issues.append({"code": "invalid_metadata", "detail": f"{label}:{field}"})
        endpoint = str(raw.get("endpoint") or "")
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query:
            issues.append({"code": "invalid_endpoint", "detail": label})
        elif parts.hostname != ALLOWED_SOURCE_HOSTS.get(source_id):
            issues.append({"code": "endpoint_not_allowlisted", "detail": label})
        for key_path, value in _walk(raw):
            key = key_path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
            if key in BODY_KEYS:
                issues.append({"code": "body_like_field", "detail": f"{label}:{key_path}"})
            if key in SECRET_KEYS:
                issues.append({"code": "secret_like_key", "detail": f"{label}:{key_path}"})
            if isinstance(value, str) and SECRET_VALUE_RE.search(value):
                issues.append({"code": "secret_like_value", "detail": f"{label}:{key_path}"})
    return issues


def _definition(raw: Mapping[str, Any]) -> SourceDefinition:
    public = {key: deepcopy(raw[key]) for key in sorted(REQUIRED_FIELDS)}
    digest = checksum(public)
    return SourceDefinition(
        source_id=str(raw["id"]), authority_role=str(raw["authority_role"]),
        owner=str(raw["owner"]), source_type=str(raw["source_type"]),
        topic=str(raw["topic"]), license=str(raw["license"]),
        provenance=str(raw["provenance"]), region=str(raw["region"]),
        publication_time_policy=str(raw["publication_time_policy"]),
        valid_time_policy=str(raw["valid_time_policy"]),
        observed_time_policy=str(raw["observed_time_policy"]),
        ingestion_time_policy=str(raw["ingestion_time_policy"]),
        quality_policy_version=str(raw["quality_policy_version"]),
        endpoint=str(raw["endpoint"]), retention_policy=str(raw["retention_policy"]),
        definition_checksum=digest,
    )


def source_definitions(path: Path = DEFAULT_REGISTRY) -> tuple[SourceDefinition, ...]:
    doc = load_registry(path)
    issues = validate_registry(doc)
    if issues:
        raise ExternalSourceRegistryError("registry_invalid", canonical_json(issues))
    return tuple(_definition(raw) for raw in doc["sources"])


def registry_checksum(path: Path = DEFAULT_REGISTRY) -> str:
    return checksum([definition.__dict__ for definition in source_definitions(path)])


def public_source_metadata(source: SourceDefinition) -> dict[str, Any]:
    return dict(source.__dict__)


class ExternalContextService:
    """One metadata-only backend shared by all local Phase 28-01 commands."""

    def __init__(self, registry_path: Path = DEFAULT_REGISTRY, db_path: Path = EXTERNAL_CONTEXT_DB) -> None:
        self.registry_path = Path(registry_path)
        self.db_path = Path(db_path)

    @staticmethod
    def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
            "ok": False, "status": "error", "error": {"code": code, "detail": detail},
            "privacy": {"metadata_only": True, "private_bodies": 0, "copyrighted_bodies": 0},
        }

    @staticmethod
    def _success(operation: str, data: Mapping[str, Any], *, empty: bool = False) -> dict[str, Any]:
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
            "ok": True, "status": "empty" if empty else "success", "data": dict(data),
            "privacy": {"metadata_only": True, "private_bodies": 0, "copyrighted_bodies": 0},
        }

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        try:
            if operation == "sources.list":
                items = [public_source_metadata(item) for item in source_definitions(self.registry_path)]
                return self._success(operation, {
                    "items": items, "total_available": len(items),
                    "registry_checksum": registry_checksum(self.registry_path),
                }, empty=not items)
            if operation == "sources.get":
                source_id = str(params.get("source_id") or "")
                source = next((item for item in source_definitions(self.registry_path) if item.source_id == source_id), None)
                if source is None:
                    return self._error(operation, "source_not_found", source_id)
                return self._success(operation, {"source": public_source_metadata(source)})
            if operation == "schema.status":
                from .migrate import inspect_schema
                return self._success(operation, inspect_schema(self.db_path, self.registry_path))
            return self._error(operation, "unknown_operation", operation)
        except ExternalSourceRegistryError as exc:
            return self._error(operation, exc.code, exc.detail)


__all__ = [
    "DEFAULT_REGISTRY", "ExternalContextService", "ExternalSourceRegistryError",
    "INTERFACE_SCHEMA_VERSION", "load_registry", "public_source_metadata",
    "registry_checksum", "source_definitions", "validate_registry",
]
