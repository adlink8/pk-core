from __future__ import annotations

from dataclasses import replace
import math

import pytest

from personal_knowledge.intelligence.analysis.candidates import CandidateParseError, parse_candidate_response
from personal_knowledge.intelligence.analysis.inputs import AnalysisInputError, ConfirmationEvent, build_confirmed_input
from personal_knowledge.intelligence.analysis.schema import EvidenceReference, SCHEMA_VERSION, checksum
from personal_knowledge.intelligence.decision.context_binding import DecisionContextBinding, DecisionContextPolicy


def _binding() -> DecisionContextBinding:
    draft = DecisionContextBinding(
        "p1", "1" * 64, "e1", "2" * 64,
        DecisionContextPolicy("global", 3600), "2026-07-18T09:00:00Z", "",
    )
    return replace(draft, binding_hash=checksum(draft.core()))


def _request(monkeypatch, **changes):
    binding = _binding()
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.inputs.validate_decision_context_binding",
        lambda value, personal, external, now=None: {"binding": binding.to_dict()},
    )
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.inputs.present_evidence_reference",
        lambda item, **kwargs: {"reference": __import__("dataclasses").asdict(item),
                               "evidence_type": item.record_type, "value": "bounded"},
    )
    values = dict(
        binding=binding, personal_db_path="personal.sqlite", external_db_path="external.sqlite",
        goal="Choose rollout", constraints=("no downtime",), weights={"safety": .7, "speed": .3},
        risk_budget="low", confirmation=ConfirmationEvent("c1", "2026-07-18T09:01:00Z", True),
        personal_evidence=(EvidenceReference("a.personal_change", "change", "p-record", "3" * 64, "p1", "1" * 64),),
        external_evidence=(EvidenceReference("s.external_fact", "fact", "e-record", "4" * 64, "e1", "2" * 64),),
    )
    values.update(changes)
    return build_confirmed_input(**values)


def _candidate(binding_hash: str, request_checksum: str) -> dict:
    tradeoffs = {
        "benefits": ["faster feedback"], "costs": ["operator time"],
        "risks": ["rollback"], "opportunity_cost": ["feature delay"],
        "reversibility": "high",
    }
    return {
        "schema_version": SCHEMA_VERSION, "binding_hash": binding_hash,
        "request_checksum": request_checksum, "domain": "project", "status": "candidate",
        "options": [{"option_id": "o1", "title": "Canary", **tradeoffs}],
        "no_action_baseline": tradeoffs, "assumptions": ["traffic is representative"],
        "uncertainty": ["exact adoption"], "missing_information": ["latest capacity"],
        "stop_conditions": ["error budget exceeded"], "abstain_reasons": [],
        "claims": [],
    }


def test_confirmed_input_binds_dual_snapshot_and_exact_evidence_allowlist(monkeypatch) -> None:
    request = _request(monkeypatch)
    manifest = request.request_manifest
    assert manifest["confirmation"]["confirmed"] is True
    assert manifest["risk_budget"] == "low" and sum(manifest["weights"].values()) == 1
    assert {item["authority_id"] for items in manifest["evidence_allowlist"].values() for item in items} == {
        "a.personal_change", "s.external_fact",
    }
    assert manifest["binding"]["personal_snapshot_id"] == "p1"
    assert manifest["binding"]["external_snapshot_id"] == "e1"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"confirmation": {"event_id": "c1", "confirmed_at": "now", "confirmed": False, "actor": "user"}},
         "user_confirmation_required"),
        ({"weights": {"safety": .8, "speed": .3}}, "weights_not_normalized"),
        ({"risk_budget": "high"}, "risk_budget_forbidden"),
        ({"personal_evidence": ()}, "evidence_allowlist_required"),
        ({"weights": {"safety": math.nan, "speed": math.inf}}, "weights_invalid"),
        ({"confirmation": {"event_id": "c1", "confirmed_at": "now", "confirmed": True, "actor": "user"}},
         "confirmation_event_invalid"),
    ],
)
def test_input_fails_closed_before_generation(monkeypatch, changes: dict, code: str) -> None:
    with pytest.raises(AnalysisInputError, match=code):
        _request(monkeypatch, **changes)


def test_binding_read_validation_failure_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.inputs.validate_decision_context_binding",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stale")),
    )
    with pytest.raises(AnalysisInputError, match="binding_validation_failed"):
        build_confirmed_input(
            binding=_binding(), personal_db_path="p", external_db_path="e", goal="goal",
            constraints=("constraint",), weights={"only": 1.0}, risk_budget="low",
            confirmation=ConfirmationEvent("c", "2026-07-18T09:01:00Z", True), personal_evidence=(), external_evidence=(),
        )


def test_complete_candidate_and_no_action_baseline_parse_strictly(monkeypatch) -> None:
    request = _request(monkeypatch)
    draft = parse_candidate_response(
        _candidate(_binding().binding_hash, request.request_checksum),
        expected_binding_hash=_binding().binding_hash, expected_request_checksum=request.request_checksum,
    )
    assert draft.options[0]["option_id"] == "o1"
    assert draft.no_action_baseline["opportunity_cost"]


def test_model_claims_require_exact_checksum_and_typed_evidence(monkeypatch) -> None:
    request = _request(monkeypatch)
    payload = _candidate(_binding().binding_hash, request.request_checksum)
    evidence = request.request_manifest["evidence_allowlist"]["external"][0]
    core = {"claim_id": "claim-1", "claim_type": "factual",
            "statement": "The external release is current.", "evidence": [evidence]}
    payload["claims"] = [core]
    from personal_knowledge.intelligence.analysis.candidates import parse_candidate_package
    _, claims = parse_candidate_package(
        payload, expected_binding_hash=_binding().binding_hash,
        expected_request_checksum=request.request_checksum,
    )
    assert claims[0].evidence[0].record_id == evidence["record_id"]
    assert claims[0].claim_checksum == checksum(core)
    payload["claims"][0]["evidence"][0]["record_checksum"] = "invalid"
    with pytest.raises(CandidateParseError, match="candidate_claim_invalid"):
        parse_candidate_package(
            payload, expected_binding_hash=_binding().binding_hash,
            expected_request_checksum=request.request_checksum,
        )


@pytest.mark.parametrize("mutation", ["missing_baseline_field", "extra_command", "empty_tradeoff", "wrong_request"])
def test_malformed_or_unbounded_candidate_fails_closed(monkeypatch, mutation: str) -> None:
    request = _request(monkeypatch)
    payload = _candidate(_binding().binding_hash, request.request_checksum)
    if mutation == "missing_baseline_field":
        payload["no_action_baseline"].pop("costs")
    elif mutation == "extra_command":
        payload["options"][0]["command"] = "deploy"
    elif mutation == "empty_tradeoff":
        payload["options"][0]["risks"] = []
    else:
        payload["request_checksum"] = "0" * 64
    with pytest.raises(CandidateParseError):
        parse_candidate_response(payload, expected_binding_hash=_binding().binding_hash,
                                 expected_request_checksum=request.request_checksum)
