from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.external_context.ingest import publish_bounded_cohort
from personal_knowledge.external_context.migrate import migrate
from personal_knowledge.external_context.registry import source_definitions
from personal_knowledge.external_context.schema import checksum
from personal_knowledge.external_context.snapshots import activate_snapshot, prepare_snapshot, validate_snapshot
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBindingError,
    create_decision_context_binding,
    validate_decision_context_binding,
)


def _personal(path: Path, snapshot_id: str = "personal-1") -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    manifest = {"schema_version": 1, "members": {}, "eval_gate_ref": None}
    digest = checksum(manifest)
    con.execute("INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
                (snapshot_id, __import__("json").dumps(manifest, sort_keys=True, separators=(",", ":")), digest,
                 "validated", None, "now", "now"))
    con.execute("UPDATE serving_authority SET active_snapshot_id=?,activated_at='now' WHERE singleton_id=1", (snapshot_id,))
    con.commit(); con.close()


def _external(path: Path, *, lifecycle: str = "current", region: str = "global") -> str:
    migrate(path, write=True)
    source = source_definitions()[0]
    manifest = {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": region,
        "observed_at": "2026-07-18T08:00:00Z", "ingested_at": "2026-07-18T08:00:00Z",
        "observations": [{"key": "release", "kind": "official_release", "value": {"version": "3.14"},
                          "publication_time": "2026-07-18T08:00:00Z", "valid_from": "2026-07-18T08:00:00Z",
                          "valid_to": "2026-07-18T08:30:00Z" if lifecycle == "stale" else None, "region": region}],
        "facts": [{"key": "release", "subject": "python", "predicate": "latest_release", "value": "3.14",
                   "valid_from": "2026-07-18T08:00:00Z", "valid_to": "2026-07-18T08:30:00Z" if lifecycle == "stale" else None,
                   "region": region, "source_quality": .98, "fact_confidence": .97,
                   "observation_keys": ["release"]}],
    }
    published = publish_bounded_cohort(path, manifest, input_manifest_checksum=checksum(manifest))
    snapshot = prepare_snapshot(path, published["fact_ids"], write=True)
    assert validate_snapshot(path, snapshot["snapshot_id"])["ok"]
    activate_snapshot(path, snapshot["snapshot_id"], write=True)
    return snapshot["snapshot_id"]


def test_binding_validates_both_exact_authorities_on_create_and_read(tmp_path: Path) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal(personal); _external(external)
    binding = create_decision_context_binding(
        personal, external, region="global", max_external_age_seconds=7200,
        now="2026-07-18T09:00:00Z",
    )
    result = validate_decision_context_binding(binding.to_dict(), personal, external, now="2026-07-18T09:30:00Z")
    assert result["binding"]["binding_hash"] == binding.binding_hash
    con = sqlite3.connect(personal)
    assert not {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} & {
        "external_facts", "external_snapshots", "external_snapshot_authority",
    }
    con.close()


def test_binding_rejects_hash_tamper_personal_drift_and_external_drift(tmp_path: Path) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal(personal); _external(external)
    binding = create_decision_context_binding(
        personal, external, region="global", max_external_age_seconds=7200, now="2026-07-18T09:00:00Z")
    payload = binding.to_dict(); payload["external_snapshot_hash"] = "0" * 64
    with pytest.raises(DecisionContextBindingError, match="binding_hash_mismatch"):
        validate_decision_context_binding(payload, personal, external, now="2026-07-18T09:00:00Z")
    _personal(tmp_path / "other.sqlite", "personal-2")
    with pytest.raises(DecisionContextBindingError, match="personal_authority_drift"):
        validate_decision_context_binding(binding, tmp_path / "other.sqlite", external, now="2026-07-18T09:00:00Z")
    # A different independently active external snapshot makes the old binding unusable.
    source = source_definitions()[0]
    manifest = {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": "global",
        "observed_at": "2026-07-18T08:10:00Z", "ingested_at": "2026-07-18T08:10:00Z",
        "observations": [{"key": "security", "kind": "official_release", "value": {"version": "3.14.1"},
                          "publication_time": "2026-07-18T08:10:00Z", "valid_from": "2026-07-18T08:10:00Z",
                          "valid_to": None, "region": "global"}],
        "facts": [{"key": "security", "subject": "python", "predicate": "security_release", "value": "3.14.1",
                   "valid_from": "2026-07-18T08:10:00Z", "valid_to": None, "region": "global",
                   "source_quality": .98, "fact_confidence": .97, "observation_keys": ["security"]}],
    }
    published = publish_bounded_cohort(external, manifest, input_manifest_checksum=checksum(manifest))
    second = prepare_snapshot(external, published["fact_ids"], write=True)
    validate_snapshot(external, second["snapshot_id"])
    activate_snapshot(external, second["snapshot_id"], write=True)
    with pytest.raises(DecisionContextBindingError, match="external_authority_drift"):
        validate_decision_context_binding(binding, personal, external, now="2026-07-18T09:00:00Z")


@pytest.mark.parametrize(
    ("region", "now", "lifecycle", "code"),
    [("cn", "2026-07-18T09:00:00Z", "current", "external_region_mismatch"),
     ("global", "2026-07-19T09:00:00Z", "current", "external_snapshot_stale"),
     ("global", "2026-07-18T09:00:00Z", "stale", "external_fact_expired")],
)
def test_region_freshness_and_lifecycle_fail_closed(tmp_path: Path, region: str, now: str,
                                                    lifecycle: str, code: str) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal(personal); _external(external, lifecycle=lifecycle)
    with pytest.raises(DecisionContextBindingError, match=code):
        create_decision_context_binding(personal, external, region=region,
                                        max_external_age_seconds=7200, now=now)


def test_oldest_source_watermark_controls_snapshot_freshness(tmp_path: Path) -> None:
    personal, external = tmp_path / "personal.sqlite", tmp_path / "external.sqlite"
    _personal(personal)
    _external(external)
    source = source_definitions()[1]
    manifest = {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": "global",
        "observed_at": "2026-07-18T06:00:00Z", "ingested_at": "2026-07-18T06:00:00Z",
        "observations": [{"key": "node", "kind": "official_release", "value": {"version": "24"},
                          "publication_time": "2026-07-18T06:00:00Z", "valid_from": "2026-07-18T06:00:00Z",
                          "valid_to": None, "region": "global"}],
        "facts": [{"key": "node", "subject": "nodejs", "predicate": "latest_release", "value": "24",
                   "valid_from": "2026-07-18T06:00:00Z", "valid_to": None, "region": "global",
                   "source_quality": .98, "fact_confidence": .97, "observation_keys": ["node"]}],
    }
    publish_bounded_cohort(external, manifest, input_manifest_checksum=checksum(manifest))
    combined = prepare_snapshot(external, write=True)
    assert validate_snapshot(external, combined["snapshot_id"])["ok"]
    activate_snapshot(external, combined["snapshot_id"], write=True)
    with pytest.raises(DecisionContextBindingError, match="external_snapshot_stale"):
        create_decision_context_binding(
            personal, external, region="global", max_external_age_seconds=7200,
            now="2026-07-18T09:00:00Z",
        )
