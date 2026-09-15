from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.intelligence.analysis.migrate import FULL_SCHEMA_SQL, TABLES, inspect_schema, migrate
from personal_knowledge.intelligence.analysis.runs import AnalysisRunError, plan_run, publish_run
from personal_knowledge.intelligence.analysis.schema import (
    AnalysisClaim, AnalysisSchemaError, CandidateDraft, EvidenceReference,
    ProviderReceipt, SCHEMA_VERSION, checksum, stable_id,
)
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding, DecisionContextPolicy,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "governance" / "policies" / "decision_analysis.yaml"


def _binding() -> DecisionContextBinding:
    draft = DecisionContextBinding(
        personal_snapshot_id="personal-1", personal_snapshot_hash="1" * 64,
        external_snapshot_id="external-1", external_snapshot_hash="2" * 64,
        policy=DecisionContextPolicy("global", 7200),
        bound_at="2026-07-18T12:00:00Z", binding_hash="",
    )
    return replace(draft, binding_hash=checksum(draft.core()))


def _claim() -> AnalysisClaim:
    ref = EvidenceReference(
        authority_id="a.personal_change", record_type="change", record_id="change-1",
        record_checksum="3" * 64, snapshot_id="personal-1", snapshot_hash="1" * 64,
    )
    core = {"claim_id": "claim-1", "claim_type": "factual", "statement": "Project capacity is constrained.",
            "evidence": [{"authority_id": ref.authority_id, "record_type": ref.record_type,
                          "record_id": ref.record_id, "record_checksum": ref.record_checksum,
                          "snapshot_id": ref.snapshot_id, "snapshot_hash": ref.snapshot_hash}]}
    return AnalysisClaim("claim-1", "factual", core["statement"], (ref,), checksum(core))


def _draft() -> CandidateDraft:
    return CandidateDraft(
        domain="project", status="candidate",
        options=({"option_id": "o1", "title": "Narrow scope", "benefits": ["focus"],
                  "costs": ["defer"], "risks": ["delay"], "opportunity_cost": ["feature"],
                  "reversibility": "high"},),
        no_action_baseline={"benefits": [], "costs": ["continued load"], "risks": ["slip"],
                            "opportunity_cost": ["focus"], "reversibility": "high"},
        assumptions=("capacity remains stable",), uncertainty=("delivery variance",),
        missing_information=("latest estimate",), stop_conditions=("budget exceeded",),
        abstain_reasons=(),
    )


def _run():
    request = {"goal": "ship bounded project", "confirmation_id": "uc-1"}
    response = {"candidate": "structured", "claim_ids": ["claim-1"]}
    receipt = ProviderReceipt(
        provider="replay", model="fixture", prompt_version="decision-analysis-v1",
        schema_version=SCHEMA_VERSION, policy_version="decision-analysis-policy-v1",
        temperature=0.0, max_output_tokens=1024, input_tokens=10, output_tokens=20,
        cost_amount=0.0, cost_currency="USD", latency_ms=1,
        request_checksum=checksum(request), response_checksum=checksum(response), status="completed",
    )
    return plan_run(binding=_binding(), policy_path=POLICY, request_manifest=request,
                    response_manifest=response, candidate=_draft(), claims=[_claim()], receipt=receipt)


def _counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        return {table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in TABLES}
    finally:
        con.close()


def test_migration_is_dry_run_first_idempotent_and_append_only(tmp_path: Path) -> None:
    db = tmp_path / "analysis.sqlite"
    dry = migrate(db)
    assert dry["dry_run"] and not db.exists()
    assert migrate(db, write=True)["migrated"]
    assert migrate(db, write=True)["no_op"]
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_analysis_%_no_%'").fetchone()[0] == 12
    con.close()


def test_migration_repairs_legacy_global_evidence_payload_uniqueness(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    legacy_sql = FULL_SCHEMA_SQL.replace(
        "payload_checksum TEXT NOT NULL CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL,\n"
        "    UNIQUE(claim_id,evidence_ordinal)",
        "payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL,\n"
        "    UNIQUE(claim_id,evidence_ordinal)",
        1,
    )
    con = sqlite3.connect(db)
    con.executescript(legacy_sql)
    con.close()
    assert inspect_schema(db)["schema_state"] == "legacy"
    dry = migrate(db)
    assert dry["would_repair_evidence_uniqueness"]
    assert migrate(db, write=True)["migrated"]
    assert inspect_schema(db)["schema_state"] == "applied"


def test_strict_contracts_reject_missing_support_forbidden_fields_and_bad_shape() -> None:
    with pytest.raises(AnalysisSchemaError, match="factual_claim_evidence_required"):
        AnalysisClaim("c", "factual", "unsupported", (), "0" * 64)
    with pytest.raises(AnalysisSchemaError, match="domain_forbidden"):
        replace(_draft(), domain="medical")
    with pytest.raises(AnalysisSchemaError, match="forbidden_field"):
        replace(_draft(), options=({"option_id": "o1", "title": "x", "benefits": [], "costs": [],
                                    "risks": [], "opportunity_cost": [], "reversibility": "high",
                                    "command": "deploy"},))


def test_publish_defaults_to_dry_run_replays_idempotently_and_verifies_checksums(tmp_path: Path) -> None:
    db = tmp_path / "analysis.sqlite"
    migrate(db, write=True)
    run = _run()
    dry = publish_run(db, run, policy_path=POLICY)
    assert dry["dry_run"] and _counts(db) == {table: 0 for table in TABLES}
    first = publish_run(db, run, policy_path=POLICY, write=True)
    after = _counts(db)
    replay = publish_run(db, run, policy_path=POLICY, write=True)
    assert first["written"] and replay["existing"] and not replay["written"]
    assert _counts(db) == after == {
        "analysis_runs": 1, "analysis_candidates": 1, "analysis_claims": 1,
        "analysis_evidence_refs": 1, "analysis_provider_receipts": 1, "analysis_events": 1,
    }
    with pytest.raises(AnalysisRunError, match="response_checksum_mismatch"):
        publish_run(db, replace(run, response_manifest={"tampered": True}), policy_path=POLICY, write=True)


def test_publish_allows_multiple_claims_to_reuse_the_same_evidence(tmp_path: Path) -> None:
    db = tmp_path / "analysis.sqlite"
    migrate(db, write=True)
    first = _claim()
    second_core = {
        "claim_id": "claim-2", "claim_type": "factual",
        "statement": "The constrained capacity affects delivery options.",
        "evidence": [{
            "authority_id": item.authority_id, "record_type": item.record_type,
            "record_id": item.record_id, "record_checksum": item.record_checksum,
            "snapshot_id": item.snapshot_id, "snapshot_hash": item.snapshot_hash,
        } for item in first.evidence],
    }
    second = AnalysisClaim(
        second_core["claim_id"], second_core["claim_type"], second_core["statement"],
        first.evidence, checksum(second_core),
    )
    request = {"goal": "ship bounded project", "confirmation_id": "uc-2"}
    response = {"candidate": "structured", "claim_ids": ["claim-1", "claim-2"]}
    receipt = ProviderReceipt(
        provider="replay", model="fixture", prompt_version="decision-analysis-v1",
        schema_version=SCHEMA_VERSION, policy_version="decision-analysis-policy-v1",
        temperature=0.0, max_output_tokens=1024, input_tokens=10, output_tokens=20,
        cost_amount=0.0, cost_currency="USD", latency_ms=1,
        request_checksum=checksum(request), response_checksum=checksum(response), status="completed",
    )
    run = plan_run(
        binding=_binding(), policy_path=POLICY, request_manifest=request,
        response_manifest=response, candidate=_draft(), claims=(first, second), receipt=receipt,
    )
    published = publish_run(db, run, policy_path=POLICY, write=True)
    assert published["written"]
    assert _counts(db)["analysis_evidence_refs"] == 2


def test_replay_rejects_offline_child_payload_tamper(tmp_path: Path) -> None:
    db = tmp_path / "analysis.sqlite"
    migrate(db, write=True)
    run = _run()
    publish_run(db, run, policy_path=POLICY, write=True)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_analysis_provider_receipts_no_update")
    con.execute(
        "UPDATE analysis_provider_receipts SET payload_json=? WHERE run_id=?",
        ('{"status":"tampered"}', run.run_id),
    )
    con.execute(
        "CREATE TRIGGER trg_analysis_provider_receipts_no_update "
        "BEFORE UPDATE ON analysis_provider_receipts "
        "BEGIN SELECT RAISE(ABORT, 'analysis_provider_receipts is append-only'); END"
    )
    con.commit()
    con.close()
    with pytest.raises(AnalysisRunError, match="existing_run_checksum_mismatch: receipt"):
        publish_run(db, run, policy_path=POLICY, write=True)


@pytest.mark.parametrize("fault_at", ["after_run", "after_candidate", "after_claims", "after_receipt", "after_event"])
def test_fault_injection_is_atomic_and_source_authorities_are_untouched(tmp_path: Path, fault_at: str) -> None:
    analysis = tmp_path / "analysis.sqlite"
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    personal.write_bytes(b"personal-authority-sentinel")
    external.write_bytes(b"external-authority-sentinel")
    before = (hashlib.sha256(personal.read_bytes()).hexdigest(), hashlib.sha256(external.read_bytes()).hexdigest())
    migrate(analysis, write=True)
    with pytest.raises(RuntimeError, match="injected"):
        publish_run(analysis, _run(), policy_path=POLICY, write=True, fault_at=fault_at)
    assert _counts(analysis) == {table: 0 for table in TABLES}
    after = (hashlib.sha256(personal.read_bytes()).hexdigest(), hashlib.sha256(external.read_bytes()).hexdigest())
    assert after == before
