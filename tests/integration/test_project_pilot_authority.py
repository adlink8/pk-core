from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL as PERSONAL_SCHEMA
from personal_knowledge.external_context.ingest import publish_bounded_cohort
from personal_knowledge.external_context.migrate import migrate as migrate_external
from personal_knowledge.external_context.registry import source_definitions
from personal_knowledge.external_context.schema import checksum
from personal_knowledge.external_context.snapshots import activate_snapshot, prepare_snapshot, validate_snapshot
from personal_knowledge.intelligence.analysis.migrate import migrate as migrate_analysis
from personal_knowledge.intelligence.analysis.runs import plan_run, publish_run
from personal_knowledge.intelligence.analysis.schema import CandidateDraft, ProviderReceipt, SCHEMA_VERSION
from personal_knowledge.intelligence.decision.context_binding import create_decision_context_binding
from personal_knowledge.intelligence.pilot.cases import admit_project_case
from personal_knowledge.intelligence.pilot.schema import TABLES, inspect_schema, migrate


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "governance/policies/decision_analysis.yaml"


def _personal(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(PERSONAL_SCHEMA)
    manifest = {"schema_version": 1, "members": {}, "eval_gate_ref": None}
    digest = checksum(manifest)
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        ("personal-1", __import__("json").dumps(manifest, sort_keys=True, separators=(",", ":")),
         digest, "validated", None, "now", "now"),
    )
    con.execute(
        "UPDATE serving_authority SET active_snapshot_id='personal-1',activated_at='now' WHERE singleton_id=1"
    )
    con.commit()
    con.close()


def _external(path: Path, *, version: str = "3.14.2") -> None:
    migrate_external(path, write=True)
    source = source_definitions()[0]
    manifest = {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": "global",
        "observed_at": "2026-07-18T08:00:00Z", "ingested_at": "2026-07-18T08:00:00Z",
        "observations": [{"key": f"release-{version}", "kind": "official_release", "value": {"version": version},
                          "publication_time": "2026-07-18T08:00:00Z", "valid_from": "2026-07-18T08:00:00Z",
                          "valid_to": None, "region": "global"}],
        "facts": [{"key": f"release-{version}", "subject": f"python-{version}", "predicate": "latest_release", "value": version,
                   "valid_from": "2026-07-18T08:00:00Z", "valid_to": None, "region": "global",
                   "source_quality": .99, "fact_confidence": .99,
                   "observation_keys": [f"release-{version}"]}],
    }
    published = publish_bounded_cohort(path, manifest, input_manifest_checksum=checksum(manifest))
    snapshot = prepare_snapshot(path, published["fact_ids"], write=True)
    assert validate_snapshot(path, snapshot["snapshot_id"])["ok"]
    activate_snapshot(path, snapshot["snapshot_id"], write=True)


def setup_authorities(tmp_path: Path) -> dict[str, Path | str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    personal, external, analysis, pilot = (
        tmp_path / name for name in ("personal.sqlite", "external.sqlite", "analysis.sqlite", "pilot.sqlite")
    )
    _personal(personal)
    _external(external)
    binding = create_decision_context_binding(
        personal, external, region="global", max_external_age_seconds=7200,
        now="2026-07-18T09:00:00Z",
    )
    migrate_analysis(analysis, write=True)
    migrate(pilot, write=True)
    draft = CandidateDraft(
        domain="project", status="candidate",
        options=(
            {"option_id": "adopt-now", "title": "Adopt now", "benefits": ["speed"],
             "costs": ["risk"], "risks": ["compatibility"], "opportunity_cost": ["validation"],
             "reversibility": "high"},
            {"option_id": "validate-first", "title": "Validate then adopt", "benefits": ["confidence"],
             "costs": ["time"], "risks": ["delay"], "opportunity_cost": ["speed"],
             "reversibility": "high"},
        ),
        no_action_baseline={"benefits": ["no change"], "costs": ["delay"], "risks": ["stagnation"],
                            "opportunity_cost": ["learning"], "reversibility": "high"},
        assumptions=("local pilot",), uncertainty=("dependency support",),
        missing_information=("test outcome",), stop_conditions=("compatibility failure",),
        abstain_reasons=(),
    )
    request = {
        "schema_version": SCHEMA_VERSION, "binding": binding.to_dict(), "binding_hash": binding.binding_hash,
        "domain": "project", "goal": "Choose a compatible local runtime",
        "constraints": ["no deployment", "manual action only"],
        "weights": {"safety": .7, "speed": .3}, "risk_budget": "low",
        "confirmation": {"event_id": "analysis-confirm-1", "confirmed_at": "2026-07-18T09:00:00Z",
                         "confirmed": True, "actor": "user"},
    }
    response = {**asdict(draft), "binding_hash": binding.binding_hash,
                "request_checksum": checksum(request), "schema_version": SCHEMA_VERSION, "claims": []}
    receipt = ProviderReceipt(
        provider="replay", model="fixture", prompt_version="decision-analysis-v1",
        schema_version=SCHEMA_VERSION, policy_version="decision-analysis-policy-v1",
        temperature=0.0, max_output_tokens=1024, input_tokens=1, output_tokens=1,
        cost_amount=0.0, cost_currency="USD", latency_ms=1,
        request_checksum=checksum(request), response_checksum=checksum(response), status="completed",
    )
    run = plan_run(
        binding=binding, policy_path=POLICY, request_manifest=request,
        response_manifest=response, candidate=draft, claims=(), receipt=receipt,
    )
    assert publish_run(analysis, run, policy_path=POLICY, write=True)["written"]
    return {"personal": personal, "external": external, "analysis": analysis, "pilot": pilot,
            "run_id": run.run_id, "candidate_id": run.candidate.candidate_id}


def _counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        return {table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in TABLES}
    finally:
        con.close()


def _admit(env: dict[str, Path | str], **changes):
    values = dict(
        pilot_db_path=env["pilot"], analysis_db_path=env["analysis"],
        personal_db_path=env["personal"], external_db_path=env["external"],
        run_id=env["run_id"], candidate_id=env["candidate_id"],
        selected_option_id="validate-first", case_confirmation_event_id="case-confirm-1",
        write=True, now="2026-07-18T09:30:00Z",
    )
    values.update(changes)
    return admit_project_case(**values)


def test_migration_append_only_and_exact_lineage_publish(tmp_path: Path) -> None:
    env = setup_authorities(tmp_path)
    assert inspect_schema(env["pilot"])["append_only_trigger_count"] == 8
    first = _admit(env)
    replay = _admit(env)
    assert first.status == "candidate" and first.written
    assert replay.status == "candidate" and replay.existing
    assert _counts(env["pilot"]) == {
        "pilot_cases": 1, "pilot_recommendations": 1, "pilot_protocols": 1, "pilot_events": 1,
    }
    assert first.case is not None and first.recommendation is not None
    con = sqlite3.connect(env["pilot"])
    con.execute("PRAGMA foreign_keys=ON")
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE pilot_cases SET confirmation_event_id='tampered'")
    con.close()


def test_analysis_tamper_and_active_snapshot_drift_abstain_without_mutation(tmp_path: Path) -> None:
    tamper = setup_authorities(tmp_path / "tamper")
    con = sqlite3.connect(tamper["analysis"])
    con.execute("DROP TRIGGER trg_analysis_candidates_no_update")
    con.execute("UPDATE analysis_candidates SET payload_json='{}'")
    con.commit()
    con.close()
    result = _admit(tamper)
    assert result.status == "abstain" and result.reason_codes == ("analysis_candidate_checksum_mismatch",)
    assert not any(_counts(tamper["pilot"]).values())

    drift = setup_authorities(tmp_path / "drift")
    _external(drift["external"], version="3.14.3")
    result = _admit(drift)
    assert result.status == "abstain"
    assert result.reason_codes == ("external_authority_drift",)
    assert not any(_counts(drift["pilot"]).values())


def test_run_checksum_and_existing_child_tamper_fail_closed(tmp_path: Path) -> None:
    run_tamper = setup_authorities(tmp_path / "run")
    con = sqlite3.connect(run_tamper["analysis"])
    con.execute("DROP TRIGGER trg_analysis_runs_no_update")
    con.execute("UPDATE analysis_runs SET run_checksum=?", ("0" * 64,))
    con.commit(); con.close()
    result = _admit(run_tamper)
    assert result.status == "abstain" and result.reason_codes == ("analysis_run_checksum_mismatch",)

    child_tamper = setup_authorities(tmp_path / "child")
    assert _admit(child_tamper).written
    con = sqlite3.connect(child_tamper["pilot"])
    con.execute("DROP TRIGGER trg_pilot_protocols_no_delete")
    con.execute("DELETE FROM pilot_protocols")
    con.execute(
        "CREATE TRIGGER trg_pilot_protocols_no_delete BEFORE DELETE ON pilot_protocols "
        "BEGIN SELECT RAISE(ABORT, 'pilot_protocols is append-only'); END"
    )
    con.commit(); con.close()
    replay = _admit(child_tamper)
    assert replay.status == "abstain" and replay.reason_codes == ("existing_case_children_missing",)


@pytest.mark.parametrize("fault_at", ["after_case", "after_event"])
def test_fault_injection_is_atomic_and_source_authorities_unchanged(tmp_path: Path, fault_at: str) -> None:
    env = setup_authorities(tmp_path)
    before = {key: Path(env[key]).read_bytes() for key in ("personal", "external", "analysis")}
    with pytest.raises(RuntimeError, match="injected"):
        _admit(env, fault_at=fault_at)
    assert not any(_counts(env["pilot"]).values())
    assert {key: Path(env[key]).read_bytes() for key in before} == before
