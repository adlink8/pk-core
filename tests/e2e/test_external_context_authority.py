from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.external_context.doctor import doctor_external_context
from personal_knowledge.external_context.ingest import publish_bounded_cohort
from personal_knowledge.external_context.migrate import migrate
from personal_knowledge.external_context.registry import DEFAULT_REGISTRY, load_registry, source_definitions
from personal_knowledge.external_context.schema import checksum
from personal_knowledge.external_context.service import ExternalContextService
from personal_knowledge.external_context.snapshots import (
    activate_snapshot,
    forward_restore_snapshot,
    get_active_snapshot,
    prepare_snapshot,
    rollback_snapshot,
    validate_snapshot,
)
from personal_knowledge.intelligence.decision.context_binding import create_decision_context_binding


NOW = "2026-07-18T12:00:00Z"


def _personal(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    manifest = {"schema_version": 1, "members": {}, "eval_gate_ref": None}
    digest = checksum(manifest)
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        ("personal-e2e", json.dumps(manifest, sort_keys=True, separators=(",", ":")),
         digest, "validated", None, "now", "now"),
    )
    con.execute(
        "UPDATE serving_authority SET active_snapshot_id='personal-e2e',activated_at='now' WHERE singleton_id=1"
    )
    con.commit(); con.close()


def _manifest(source_index: int, version: str, minute: int, *, predicate: str | None = None,
              value: str | None = None) -> dict:
    source = source_definitions()[source_index]
    at = f"2026-07-18T10:{minute:02d}:00Z"
    subject = "python" if source_index == 0 else "nodejs"
    fact_value = value or version
    return {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": "global",
        "observed_at": at, "ingested_at": at,
        "observations": [{
            "key": f"{subject}-{version}-{minute}", "kind": "official_release",
            "value": {"version": version, "status": "stable"},
            "publication_time": at, "valid_from": at, "valid_to": None, "region": "global",
        }],
        "facts": [{
            "key": f"{subject}-{version}-{minute}", "subject": subject,
            "predicate": predicate or f"release_at_{minute}", "value": fact_value,
            "valid_from": at, "valid_to": None, "region": "global",
            "source_quality": .98, "fact_confidence": .97,
            "observation_keys": [f"{subject}-{version}-{minute}"],
        }],
    }


def _publish(path: Path, manifest: dict) -> str:
    return publish_bounded_cohort(
        path, manifest, input_manifest_checksum=checksum(manifest)
    )["fact_ids"][0]


def _authority_state(path: Path, *, external: bool) -> tuple:
    con = sqlite3.connect(path)
    try:
        if external:
            return tuple(con.execute(
                "SELECT authority_sequence,snapshot_id,snapshot_hash,action,previous_snapshot_id,activation_event_id "
                "FROM external_snapshot_authority ORDER BY authority_sequence"
            ).fetchall())
        return tuple(con.execute(
            "SELECT singleton_id,active_snapshot_id,activated_at,activation_event_id FROM serving_authority"
        ).fetchall())
    finally:
        con.close()


def _setup(tmp_path: Path) -> tuple[Path, Path, str, str]:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal(personal); migrate(external, write=True)
    old_facts = [_publish(external, _manifest(0, "3.14.0", 0)),
                 _publish(external, _manifest(1, "24.4.0", 1))]
    old = prepare_snapshot(external, old_facts, write=True, prepared_at="2026-07-18T10:10:00Z")
    assert validate_snapshot(external, old["snapshot_id"], occurred_at="2026-07-18T10:11:00Z")["ok"]
    activate_snapshot(external, old["snapshot_id"], write=True, occurred_at="2026-07-18T10:12:00Z")
    new_facts = [_publish(external, _manifest(0, "3.14.1", 20)),
                 _publish(external, _manifest(1, "24.4.1", 21))]
    new = prepare_snapshot(external, new_facts, write=True, prepared_at="2026-07-18T10:30:00Z")
    assert validate_snapshot(external, new["snapshot_id"], occurred_at="2026-07-18T10:31:00Z")["ok"]
    activate_snapshot(external, new["snapshot_id"], write=True, occurred_at="2026-07-18T10:32:00Z")
    return personal, external, old["snapshot_id"], new["snapshot_id"]


def _by_id(report: dict) -> dict[str, dict]:
    return {item["check_id"]: item for item in report["checks"]}


def test_two_source_reversible_authority_doctor_and_fault_isolation(tmp_path: Path) -> None:
    personal, external, old_id, new_id = _setup(tmp_path)
    rollback_snapshot(external, old_id, write=True, occurred_at="2026-07-18T10:40:00Z")
    forward_restore_snapshot(external, new_id, write=True, occurred_at="2026-07-18T10:41:00Z")
    active = get_active_snapshot(external)
    assert active and active["snapshot_id"] == new_id and active["action"] == "forward_restore"
    binding = create_decision_context_binding(
        personal, external, region="global", max_external_age_seconds=7200, now=NOW,
    )
    before_external = _authority_state(external, external=True)
    before_personal = _authority_state(personal, external=False)
    with pytest.raises(RuntimeError, match="injected"):
        rollback_snapshot(external, old_id, write=True, fault_at="after_authority")
    assert _authority_state(external, external=True) == before_external
    assert _authority_state(personal, external=False) == before_personal
    report = doctor_external_context(
        external, personal, now=NOW, max_age_seconds=7200, binding=binding,
    )
    checks = _by_id(report)
    assert report["ok"] and report["critical_fail"] == 0
    assert set(checks) == {
        "registry_projection", "sqlite_integrity", "active_manifest", "snapshot_event_chain",
        "watermarks_freshness", "conflict_state", "body_leakage", "authority_separation",
        "dual_binding_parity", "read_only_execution",
    }
    assert checks["watermarks_freshness"]["detail"]["source_ids"] == [
        "ext.nodejs_releases", "ext.python_releases",
    ]
    service = ExternalContextService(external).invoke("facts.list")
    assert service["ok"] and service["data"]["total_available"] == 2
    assert {item["snapshot_id"] for item in service["data"]["items"]} == {new_id}


def test_doctor_fails_closed_on_registry_drift_without_mutation(tmp_path: Path) -> None:
    personal, external, _, _ = _setup(tmp_path)
    drifted = load_registry(DEFAULT_REGISTRY)
    drifted["sources"][0]["license"] = "drifted-license"
    registry = tmp_path / "external_sources.yaml"
    registry.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    before = (_authority_state(external, external=True), _authority_state(personal, external=False))
    report = doctor_external_context(external, personal, registry_path=registry, now=NOW, max_age_seconds=7200)
    assert not report["ok"] and not _by_id(report)["registry_projection"]["ok"]
    assert before == (_authority_state(external, external=True), _authority_state(personal, external=False))


def test_doctor_detects_fk_manifest_watermark_and_body_tamper(tmp_path: Path) -> None:
    personal, external, _, new_id = _setup(tmp_path)
    con = sqlite3.connect(external)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP TRIGGER trg_external_observations_no_update")
    con.execute("UPDATE external_observations SET value_json=? WHERE observation_id=(SELECT MIN(observation_id) FROM external_observations)",
                (json.dumps({"body": "must-not-persist"}),))
    con.execute("DROP TRIGGER trg_external_snapshot_watermarks_no_update")
    con.execute("UPDATE external_snapshot_watermarks SET watermark_checksum=? WHERE snapshot_id=?",
                ("0" * 64, new_id))
    con.execute(
        "INSERT INTO external_snapshot_members VALUES (?,?,?,?,?,?)",
        (new_id, "missing-fact", "0" * 64, "current", "global",
         con.execute("SELECT watermark_id FROM external_snapshot_watermarks WHERE snapshot_id=? LIMIT 1", (new_id,)).fetchone()[0]),
    )
    con.commit(); con.close()
    report = doctor_external_context(external, personal, now=NOW, max_age_seconds=7200)
    checks = _by_id(report)
    assert not report["ok"]
    assert not checks["sqlite_integrity"]["ok"] and checks["sqlite_integrity"]["detail"]["foreign_key_violations"] >= 1
    assert not checks["active_manifest"]["ok"]
    assert not checks["body_leakage"]["ok"] and checks["body_leakage"]["detail"]["leak_count"] == 1


def test_doctor_detects_conflict_freshness_separation_and_binding_drift(tmp_path: Path) -> None:
    personal, external, _, _ = _setup(tmp_path)
    old_binding = create_decision_context_binding(
        personal, external, region="global", max_external_age_seconds=7200, now=NOW,
    )
    first = _publish(external, _manifest(0, "3.15.0", 40, predicate="latest_release", value="3.15.0"))
    second = _publish(external, _manifest(0, "3.16.0", 41, predicate="latest_release", value="3.16.0"))
    conflicted = prepare_snapshot(external, [first, second], write=True)
    assert validate_snapshot(external, conflicted["snapshot_id"])["ok"]
    activate_snapshot(external, conflicted["snapshot_id"], write=True)
    con = sqlite3.connect(personal)
    con.execute("CREATE TABLE external_facts(marker TEXT)")
    con.commit(); con.close()
    report = doctor_external_context(
        external, personal, now="2026-07-20T12:00:00Z", max_age_seconds=3600,
        binding=old_binding,
    )
    checks = _by_id(report)
    assert not checks["watermarks_freshness"]["ok"]
    assert not checks["conflict_state"]["ok"]
    assert not checks["authority_separation"]["ok"]
    assert not checks["dual_binding_parity"]["ok"]
