from __future__ import annotations

from copy import deepcopy

from personal_knowledge.governance.artifact_registry import load_registry, validate_registry


def _codes(doc: dict) -> set[str]:
    return {issue.code for issue in validate_registry(doc)}


def test_default_registry_is_complete_and_metadata_only() -> None:
    doc = load_registry()
    assert validate_registry(doc) == []
    assert set(doc["required_serving_roles"]) <= {
        row["authority_role"] for row in doc["artifacts"]
    }


def test_duplicate_authority_and_invalid_layer_fail_closed() -> None:
    doc = load_registry()
    duplicate = deepcopy(doc["artifacts"][0])
    duplicate["id"] = "s.duplicate"
    duplicate["layer"] = "D"
    doc["artifacts"].append(duplicate)
    assert {"duplicate_authority", "invalid_layer"} <= _codes(doc)


def test_missing_metadata_unknown_dependency_and_payload_fail_closed() -> None:
    doc = load_registry()
    row = doc["artifacts"][3]
    del row["producer"]
    row["evidence_parent"] = "d.missing"
    row["content"] = "private body"
    assert {"missing_fields", "unknown_dependency", "private_payload"} <= _codes(doc)


def test_secret_like_values_fail_closed() -> None:
    doc = load_registry()
    doc["artifacts"][0]["version_source"] = "api_key=not-a-real-key"
    assert "secret_like_value" in _codes(doc)
