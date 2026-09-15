from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, UNIFIED_DB
from personal_knowledge.external_context.registry import (
    load_registry,
    registry_checksum,
    source_definitions,
    validate_registry,
)


def _codes(doc: dict) -> set[str]:
    return {item["code"] for item in validate_registry(doc)}


def _write(path: Path, doc: dict) -> None:
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_two_source_registry_is_stable_metadata_only_and_separate() -> None:
    doc = load_registry()
    assert validate_registry(doc) == []
    assert len(doc["sources"]) == len(source_definitions()) == 2
    assert registry_checksum() == registry_checksum()
    assert EXTERNAL_CONTEXT_DB != UNIFIED_DB
    assert EXTERNAL_CONTEXT_DB.name == "external_context.sqlite"
    rendered = yaml.safe_dump(doc).lower()
    assert not any(f"{key}:" in rendered for key in ("body", "content", "raw_text", "api_key"))


def test_policy_change_changes_registry_checksum(tmp_path: Path) -> None:
    doc = load_registry()
    original = tmp_path / "original.yaml"
    changed = tmp_path / "changed.yaml"
    _write(original, doc)
    edited = deepcopy(doc)
    edited["sources"][0]["quality_policy_version"] = "external-source-quality-v2"
    _write(changed, edited)
    assert registry_checksum(original) != registry_checksum(changed)


def test_invalid_private_duplicate_and_incomplete_definitions_fail_closed() -> None:
    doc = load_registry()
    doc["sources"][1]["authority_role"] = doc["sources"][0]["authority_role"]
    doc["sources"][0]["body"] = "copyrighted text"
    doc["sources"][0]["credential"] = "api_key=do-not-store-this"
    del doc["sources"][0]["license"]
    assert {
        "duplicate_authority", "body_like_field", "secret_like_value", "missing_fields",
    } <= _codes(doc)


def test_missing_region_or_time_policy_and_extra_source_fail_closed() -> None:
    doc = load_registry()
    doc["sources"].append(deepcopy(doc["sources"][0]))
    doc["sources"][0]["region"] = ""
    del doc["sources"][1]["valid_time_policy"]
    assert {"source_count", "invalid_metadata", "missing_fields", "duplicate_source"} <= _codes(doc)


def test_unknown_source_type_region_and_endpoint_fail_closed() -> None:
    doc = load_registry()
    doc["sources"][0]["source_type"] = "blog"
    doc["sources"][0]["region"] = "unknown"
    doc["sources"][0]["endpoint"] = "https://example.invalid/releases"
    assert {
        "invalid_source_type", "invalid_region", "endpoint_not_allowlisted",
    } <= _codes(doc)
