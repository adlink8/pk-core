from __future__ import annotations

import json

import pytest

from personal_knowledge.intelligence.cli import run_acceptance
from personal_knowledge.intelligence.runs import PersonalStateValidationError, plan_run
from personal_knowledge.intelligence.state_projection import ProjectionError, normalize_candidates
from tests.contract.test_personal_state_interfaces import _service
from tests.integration.test_personal_state_runs import StubResolver, _assertion, _database


def test_inference_as_fact_is_rejected_before_analysis(tmp_path) -> None:
    db_path = _database(tmp_path)
    run = plan_run(
        db_path, [_assertion()], producer_version="fixture", input_manifest={"source": "x"},
        resolver=StubResolver(),
    )
    candidate = {
        "snapshot_id": run.snapshot.snapshot_id,
        "snapshot_hash": run.snapshot.snapshot_hash,
        "assertion_kind": "goal",
        "provenance_class": "fact",
        "derivation": "synthesis",
        "subject": "user", "domain": "work", "scope": "personal", "predicate": "target",
        "value": "D", "valid_from": "2026-07-18T00:00:00Z",
        "observed_at": "2026-07-18T00:00:00Z", "confidence": 0.8,
        "uncertainty_reason": "inferred",
        "evidence": [{
            "snapshot_id": run.snapshot.snapshot_id,
            "snapshot_hash": run.snapshot.snapshot_hash,
            "ref": "ku1", "artifact_type": "knowledge_unit",
            "serving_role": "canonical_knowledge", "artifact_version_id": "av1",
            "privacy_class": "R4",
        }],
    }
    with pytest.raises(ProjectionError, match="provenance_rule_violation"):
        normalize_candidates([candidate], snapshot=run.snapshot)


@pytest.mark.parametrize(
    "resolver,code",
    [
        (StubResolver(status="missing", eligible=False), "evidence_ineligible"),
        (StubResolver(metadata={"note": "password=never-store-this"}), "secret_payload"),
    ],
)
def test_ineligible_or_secret_evidence_fails_closed(tmp_path, resolver, code) -> None:
    db_path = _database(tmp_path)
    with pytest.raises(PersonalStateValidationError, match=code):
        plan_run(
            db_path, [_assertion()], producer_version="fixture",
            input_manifest={"source": "privacy-fixture"}, resolver=resolver,
        )


def test_acceptance_output_has_no_private_body_fields(tmp_path) -> None:
    db_path, _, _, _ = _service(tmp_path)
    result = run_acceptance(db_path, pointer_path=tmp_path / "missing.txt")
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        '"content"', '"body"', '"raw_text"', '"answer"', '"question"',
        '"evidence_quote"', '"prompt"', '"response_text"',
    ):
        assert forbidden not in serialized
    assert result["private_bodies"] == 0
