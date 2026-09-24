"""Strict, deterministic Project Capability Registry loader."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_PATH = ROOT / "governance" / "manifests" / "capabilities" / "project-capabilities.json"
SCHEMA = "project-capability-registry-v1"
PROFILES = frozenset({"production", "operator", "test"})
PRIVACY = frozenset({"R0", "R1", "R2"})
AUTHORITIES = frozenset({"knowledge", "retrieval", "state", "external", "decision", "action_outcome", "evidence", "wiki", "data_quality", "system", "warehouse"})
SIDE_EFFECTS = frozenset({"none", "candidate", "derived", "canonical", "promotion", "rollback"})


class CapabilityRegistryError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def operation_checksum(operation: Mapping[str, Any]) -> str:
    return _digest({key: value for key, value in operation.items() if key != "checksum"})


def registry_checksum(registry: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in registry.items() if key != "checksum"}
    return _digest(payload)


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise CapabilityRegistryError(code, detail)


def validate_registry(registry: Mapping[str, Any]) -> dict[str, Any]:
    _require(isinstance(registry, Mapping), "registry_type")
    _require(registry.get("schema") == SCHEMA, "registry_schema")
    _require(isinstance(registry.get("version"), str) and registry["version"].count(".") == 2, "registry_version")
    operations = registry.get("operations")
    _require(isinstance(operations, list) and operations, "operations_required")
    _require(registry.get("checksum") == registry_checksum(registry), "registry_checksum_drift")
    ids: set[str] = set()
    aliases: set[str] = set()
    normalized: list[dict[str, Any]] = []
    required = {"id", "version", "checksum", "title", "description", "input_schema", "output_schema", "profiles", "privacy_ceiling", "authority_class", "side_effect_class", "timeout_ms", "budget", "idempotency", "confirmation", "receipt_schema", "status"}
    for operation in operations:
        _require(isinstance(operation, Mapping), "operation_type")
        _require(set(operation) >= required, "operation_fields")
        op = dict(operation)
        identifier = op.get("id")
        _require(isinstance(identifier, str) and "." in identifier and identifier == identifier.lower(), "operation_id")
        _require(identifier not in ids, "duplicate_operation_id", identifier)
        ids.add(identifier)
        profiles = op.get("profiles")
        _require(isinstance(profiles, list) and profiles and set(profiles) <= PROFILES and len(profiles) == len(set(profiles)), "profile_invalid", identifier)
        _require(op.get("privacy_ceiling") in PRIVACY, "privacy_invalid", identifier)
        _require(op.get("authority_class") in AUTHORITIES, "authority_invalid", identifier)
        _require(op.get("side_effect_class") in SIDE_EFFECTS, "side_effect_invalid", identifier)
        _require(isinstance(op.get("timeout_ms"), int) and 0 < op["timeout_ms"] <= 120000, "timeout_invalid", identifier)
        budget = op.get("budget")
        _require(isinstance(budget, Mapping) and isinstance(budget.get("max_bytes"), int) and budget["max_bytes"] > 0 and budget.get("provider_calls") == 0, "budget_invalid", identifier)
        idem = op.get("idempotency")
        _require(isinstance(idem, Mapping) and idem.get("required") is True and idem.get("scope") in {"task", "session", "operation"}, "idempotency_invalid", identifier)
        confirmation = op.get("confirmation")
        _require(isinstance(confirmation, Mapping) and isinstance(confirmation.get("required"), bool), "confirmation_invalid", identifier)
        _require(op.get("status") in {"active", "deprecated"}, "status_invalid", identifier)
        _require(op.get("checksum") == operation_checksum(op), "operation_checksum_drift", identifier)
        if op["side_effect_class"] == "none":
            _require(op["confirmation"]["required"] is False, "read_confirmation_escalation", identifier)
        for alias in op.get("aliases", []):
            _require(isinstance(alias, Mapping) and alias.get("deprecated") is True and isinstance(alias.get("name"), str), "alias_invalid", identifier)
            name = alias["name"]
            _require(name not in ids and name not in aliases, "duplicate_alias", name)
            aliases.add(name)
        normalized.append(op)
    _require(ids.isdisjoint(aliases), "alias_id_collision")
    return {**dict(registry), "operations": normalized}


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityRegistryError("registry_unavailable") from exc
    return validate_registry(value)


def operations_for_profile(registry: Mapping[str, Any], profile: str = "production") -> list[dict[str, Any]]:
    _require(profile in PROFILES, "profile_unknown", profile)
    validated = validate_registry(registry)
    return [dict(operation) for operation in validated["operations"] if profile in operation["profiles"] and operation["status"] == "active"]


def descriptor_snapshot(registry: Mapping[str, Any], profile: str = "production") -> dict[str, Any]:
    selected = operations_for_profile(registry, profile)
    descriptors = [{"name": operation["id"], "input_schema": operation["input_schema"], "output_schema": operation["output_schema"], "authority_class": operation["authority_class"], "privacy_ceiling": operation["privacy_ceiling"], "side_effect_class": operation["side_effect_class"], "timeout_ms": operation["timeout_ms"], "budget": operation["budget"], "receipt_schema": operation["receipt_schema"], "source_checksum": operation["checksum"], "aliases": operation.get("aliases", [])} for operation in selected]
    return {"schema": "project-capability-descriptors-v1", "profile": profile, "registry_checksum": registry_checksum(registry), "operations": descriptors}


__all__ = ["CapabilityRegistryError", "DEFAULT_REGISTRY_PATH", "SCHEMA", "descriptor_snapshot", "load_registry", "operation_checksum", "operations_for_profile", "registry_checksum", "validate_registry"]
