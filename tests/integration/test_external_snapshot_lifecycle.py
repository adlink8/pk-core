from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.external_context.ingest import publish_bounded_cohort
from personal_knowledge.external_context.migrate import migrate
from personal_knowledge.external_context.registry import source_definitions
from personal_knowledge.external_context.schema import checksum
from personal_knowledge.external_context.snapshots import (
    activate_snapshot,
    forward_restore_snapshot,
    get_active_snapshot,
    prepare_snapshot,
    rollback_snapshot,
    validate_snapshot,
)


def _manifest(version: str, at: str) -> dict:
    source = source_definitions()[0]
    return {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": "global",
        "observed_at": at, "ingested_at": at,
        "observations": [{
            "key": f"python-{version}", "kind": "official_release", "value": {"version": version},
            "publication_time": at, "valid_from": at, "valid_to": None, "region": "global",
        }],
        "facts": [{
            "key": f"python-release-{version}", "subject": "python", "predicate": f"release_{version}",
            "value": version, "valid_from": at, "valid_to": None, "region": "global",
            "source_quality": .98, "fact_confidence": .97,
            "observation_keys": [f"python-{version}"],
        }],
    }


def _cohort(db: Path, version: str, at: str) -> str:
    manifest = _manifest(version, at)
    result = publish_bounded_cohort(db, manifest, input_manifest_checksum=checksum(manifest))
    return result["fact_ids"][0]


def test_prepare_validate_activate_rollback_and_forward_restore_are_exact(tmp_path: Path) -> None:
    db = tmp_path / "external.sqlite"
    migrate(db, write=True)
    first_fact = _cohort(db, "3.14", "2026-07-18T08:00:00Z")
    second_fact = _cohort(db, "3.15", "2026-07-18T09:00:00Z")
    dry = prepare_snapshot(db, [first_fact])
    assert dry["dry_run"] and not dry["written"] and get_active_snapshot(db) is None
    first = prepare_snapshot(db, [first_fact], write=True, prepared_at="2026-07-18T10:00:00Z")
    assert validate_snapshot(db, first["snapshot_id"], occurred_at="2026-07-18T10:01:00Z")["ok"]
    assert activate_snapshot(db, first["snapshot_id"])["written"] is False
    activate_snapshot(db, first["snapshot_id"], write=True, occurred_at="2026-07-18T10:02:00Z")
    second = prepare_snapshot(db, [second_fact], write=True, prepared_at="2026-07-18T10:03:00Z")
    assert validate_snapshot(db, second["snapshot_id"], occurred_at="2026-07-18T10:04:00Z")["ok"]
    activate_snapshot(db, second["snapshot_id"], write=True, occurred_at="2026-07-18T10:05:00Z")
    rollback_snapshot(db, first["snapshot_id"], write=True, occurred_at="2026-07-18T10:06:00Z")
    assert get_active_snapshot(db)["snapshot_id"] == first["snapshot_id"]
    forward_restore_snapshot(db, second["snapshot_id"], write=True, occurred_at="2026-07-18T10:07:00Z")
    active = get_active_snapshot(db)
    assert (active["snapshot_id"], active["snapshot_hash"]) == (second["snapshot_id"], second["snapshot_hash"])
    con = sqlite3.connect(db)
    assert con.execute("SELECT action FROM external_snapshot_authority ORDER BY authority_sequence").fetchall() == [
        ("activate",), ("activate",), ("rollback",), ("forward_restore",),
    ]
    assert con.execute("SELECT COUNT(*) FROM external_snapshots").fetchone()[0] == 2
    con.close()


@pytest.mark.parametrize("fault_at", ["before_event", "after_event", "after_authority"])
def test_switch_fault_rolls_back_event_and_authority(tmp_path: Path, fault_at: str) -> None:
    db = tmp_path / "external.sqlite"
    migrate(db, write=True)
    fact = _cohort(db, "3.14", "2026-07-18T08:00:00Z")
    snapshot = prepare_snapshot(db, [fact], write=True)
    validate_snapshot(db, snapshot["snapshot_id"])
    with pytest.raises(RuntimeError, match="injected"):
        activate_snapshot(db, snapshot["snapshot_id"], write=True, fault_at=fault_at)
    assert get_active_snapshot(db) is None
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM external_snapshot_authority").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM external_snapshot_events WHERE event_type='activated'").fetchone()[0] == 0
    con.close()


def test_manifest_tamper_fails_without_authority_change(tmp_path: Path) -> None:
    db = tmp_path / "external.sqlite"
    migrate(db, write=True)
    fact = _cohort(db, "3.14", "2026-07-18T08:00:00Z")
    snapshot = prepare_snapshot(db, [fact], write=True)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_external_snapshots_no_update")
    con.execute("UPDATE external_snapshots SET manifest_json='{}' WHERE snapshot_id=?", (snapshot["snapshot_id"],))
    con.commit(); con.close()
    refused = validate_snapshot(db, snapshot["snapshot_id"])
    assert not refused["ok"] and "snapshot_hash_mismatch" in refused["errors"]
    assert get_active_snapshot(db) is None
