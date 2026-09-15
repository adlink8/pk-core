from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from personal_knowledge.intelligence.analysis.evidence import EvidenceGateError, validate_claim_evidence
from personal_knowledge.intelligence.analysis.schema import AnalysisClaim, EvidenceReference, checksum
from personal_knowledge.intelligence.decision.context_binding import DecisionContextBinding, DecisionContextPolicy


def _binding() -> DecisionContextBinding:
    draft = DecisionContextBinding(
        "p1", "1" * 64, "e1", "2" * 64,
        DecisionContextPolicy("global", 3600), "2026-07-18T09:00:00Z", "",
    )
    return replace(draft, binding_hash=checksum(draft.core()))


def _personal_db(path: Path, *, provenance: str = "fact") -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE personal_state_assertions(assertion_id TEXT,run_id TEXT,payload_checksum TEXT,provenance_class TEXT)")
    con.execute("INSERT INTO personal_state_assertions VALUES ('a1','run1',?,?)", ("3" * 64, provenance))
    con.commit(); con.close()


def _claim(ref: EvidenceReference, claim_type: str = "factual") -> AnalysisClaim:
    return AnalysisClaim("c1", claim_type, "supported statement", (ref,), "9" * 64)


def _patch_binding(monkeypatch) -> None:
    binding = _binding()
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.evidence.validate_decision_context_binding",
        lambda *args, **kwargs: {"binding": binding.to_dict()},
    )


def test_personal_record_resolves_exact_membership_checksum_and_type(tmp_path: Path, monkeypatch) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal_db(personal); external.write_bytes(b"external-sentinel")
    _patch_binding(monkeypatch)
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.evidence.resolve_cognition_reference",
        lambda *args, **kwargs: SimpleNamespace(
            authority_id="a.personal_change", record_id="a1", record_checksum="3" * 64,
            snapshot_id="p1", snapshot_hash="1" * 64,
        ),
    )
    ref = EvidenceReference("a.personal_change", "fact", "a1", "3" * 64, "p1", "1" * 64)
    resolved = validate_claim_evidence(
        (_claim(ref),), allowlist=(ref,), binding=_binding(),
        personal_db_path=personal, external_db_path=external,
    )
    assert resolved[0].evidence_type == "fact"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [("not_allowlisted", "claim_support_not_allowlisted"),
     ("wrong_checksum", "evidence_checksum_mismatch"),
     ("cross_snapshot", "evidence_snapshot_mismatch")],
)
def test_invented_cross_snapshot_or_checksum_drift_fails_stably(
    tmp_path: Path, monkeypatch, mutation: str, code: str,
) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal_db(personal); external.write_bytes(b"external")
    _patch_binding(monkeypatch)
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.evidence.resolve_cognition_reference",
        lambda *args, **kwargs: SimpleNamespace(
            authority_id="a.personal_change", record_id="a1", record_checksum="3" * 64,
            snapshot_id="p1", snapshot_hash="1" * 64,
        ),
    )
    ref = EvidenceReference(
        "a.personal_change", "fact", "a1", "4" * 64 if mutation == "wrong_checksum" else "3" * 64,
        "other" if mutation == "cross_snapshot" else "p1", "1" * 64,
    )
    allowlist = () if mutation == "not_allowlisted" else (ref,)
    with pytest.raises(EvidenceGateError, match=code):
        validate_claim_evidence(
            (_claim(ref),), allowlist=allowlist, binding=_binding(),
            personal_db_path=personal, external_db_path=external,
        )


def test_factual_claim_rejects_inference_support(tmp_path: Path, monkeypatch) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal_db(personal, provenance="inference"); external.write_bytes(b"external")
    _patch_binding(monkeypatch)
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.evidence.resolve_cognition_reference",
        lambda *args, **kwargs: SimpleNamespace(
            authority_id="a.personal_change", record_id="a1", record_checksum="3" * 64,
            snapshot_id="p1", snapshot_hash="1" * 64,
        ),
    )
    ref = EvidenceReference("a.personal_change", "inference", "a1", "3" * 64, "p1", "1" * 64)
    with pytest.raises(EvidenceGateError, match="claim_evidence_type_incompatible"):
        validate_claim_evidence(
            (_claim(ref),), allowlist=(ref,), binding=_binding(),
            personal_db_path=personal, external_db_path=external,
        )


def test_external_fact_uses_exact_active_snapshot_service(tmp_path: Path, monkeypatch) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    personal.write_bytes(b"personal"); external.write_bytes(b"external")
    _patch_binding(monkeypatch)

    class Service:
        def __init__(self, path): self.path = path
        def invoke(self, operation, **params):
            return {"ok": True, "data": {"fact_id": "f1", "fact_checksum": "4" * 64,
                    "snapshot_id": "e1", "snapshot_hash": "2" * 64}}

    monkeypatch.setattr("personal_knowledge.intelligence.analysis.evidence.ExternalContextService", Service)
    ref = EvidenceReference("s.external_fact", "external_fact", "f1", "4" * 64, "e1", "2" * 64)
    result = validate_claim_evidence(
        (_claim(ref),), allowlist=(ref,), binding=_binding(),
        personal_db_path=personal, external_db_path=external,
    )
    assert result[0].authority_id == "s.external_fact"

