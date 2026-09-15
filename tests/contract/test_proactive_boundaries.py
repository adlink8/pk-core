from __future__ import annotations

from pathlib import Path

import yaml

from personal_knowledge.intelligence.proactive.runs import REGISTRY_ID
from personal_knowledge.intelligence.proactive.schema import canonical_json


def test_registry_is_independent_non_serving_a_layer() -> None:
    policy = yaml.safe_load(Path("governance/policies/artifact_layers.yaml").read_text(encoding="utf-8"))
    entry = next(item for item in policy["artifacts"] if item["id"] == REGISTRY_ID)
    assert entry["layer"] == "A"
    assert entry["authority_role"] == "proactive_intelligence"
    assert entry["evidence_parent"] == "a.decision_feedback"
    assert entry["authority_role"] not in policy["required_serving_roles"]


def test_canonical_payload_rejects_private_or_action_authority() -> None:
    from personal_knowledge.intelligence.proactive.schema import validate_metadata_payload

    for payload in (
        {"body": "private"}, {"credential": "secret"}, {"webhook": "https://x"},
        {"command": "run"}, {"recipient": "someone"}, {"send_target": "mail"},
    ):
        try:
            validate_metadata_payload(payload)
        except ValueError as exc:
            assert "forbidden_payload" in str(exc)
        else:
            raise AssertionError(canonical_json(payload))


def test_candidate_contract_has_no_truth_or_delivery_authority() -> None:
    from dataclasses import fields
    from personal_knowledge.intelligence.proactive.schema import CandidateDraft, ProactiveCandidate

    prohibited = {"fact", "knowledge_unit", "approved", "executed", "command", "connector",
                  "recipient", "credential", "webhook", "send_target", "external_action"}
    assert not ({field.name for field in fields(CandidateDraft)} & prohibited)
    assert not ({field.name for field in fields(ProactiveCandidate)} & prohibited)


def test_control_contract_is_overlay_only_and_user_owned() -> None:
    from dataclasses import fields
    from personal_knowledge.intelligence.proactive.controls import ControlCommand

    names = {field.name for field in fields(ControlCommand)}
    assert {"target", "actor_class", "actor_identity_hash", "expected_sequence", "idempotency_key"} <= names
    assert not names & {"reviewer", "approve", "apply_lifecycle", "publish", "dispatch", "external_action"}
