from __future__ import annotations

from copy import deepcopy

from personal_knowledge.governance.artifact_registry import load_registry, validate_registry


def test_tracked_artifact_registry_is_complete_and_valid() -> None:
    doc = load_registry()
    assert validate_registry(doc) == []
    roles = {row["authority_role"] for row in doc["artifacts"]}
    assert set(doc["required_serving_roles"]) <= roles


def test_duplicate_authority_is_rejected() -> None:
    doc = deepcopy(load_registry())
    doc["artifacts"][1]["authority_role"] = doc["artifacts"][0]["authority_role"]
    assert "duplicate_authority" in {issue.code for issue in validate_registry(doc)}


def test_upward_dependency_and_private_payload_are_rejected() -> None:
    doc = deepcopy(load_registry())
    canonical = next(row for row in doc["artifacts"] if row["id"] == "d.canonical_message")
    canonical["evidence_parent"] = "r.knowledge_index"
    canonical["content"] = "private text must not be tracked"
    codes = {issue.code for issue in validate_registry(doc)}
    assert {"invalid_dependency", "private_payload"} <= codes
