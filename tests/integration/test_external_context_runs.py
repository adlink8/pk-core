from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.external_context.ingest import (
    ExternalIngestError,
    authority_fingerprint,
    publish_bounded_cohort,
)
from personal_knowledge.external_context.migrate import migrate
from personal_knowledge.external_context.registry import source_definitions
from personal_knowledge.external_context.schema import checksum


def _manifest() -> dict:
    source = source_definitions()[0]
    return {
        "schema_version": "external_context_import_v1",
        "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version,
        "region": "global",
        "observed_at": "2026-07-18T08:00:00Z",
        "ingested_at": "2026-07-18T08:01:00Z",
        "observations": [{
            "key": "python-3.14.0", "kind": "official_release",
            "value": {"version": "3.14.0", "status": "stable"},
            "publication_time": "2026-07-17T12:00:00Z",
            "valid_from": "2026-07-17T12:00:00Z", "valid_to": None,
            "region": "global",
        }],
        "facts": [{
            "key": "python-latest", "subject": "python", "predicate": "latest_release",
            "value": "3.14.0", "valid_from": "2026-07-17T12:00:00Z", "valid_to": None,
            "region": "global", "source_quality": 0.98, "fact_confidence": 0.97,
            "observation_keys": ["python-3.14.0"],
        }],
    }


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "external.sqlite"
    migrate(path, write=True)
    return path


def _publish(db: Path, manifest: dict, **kwargs):
    return publish_bounded_cohort(
        db, manifest, input_manifest_checksum=checksum(manifest), **kwargs,
    )


def test_valid_cohort_commits_once_and_replay_is_noop(tmp_path: Path) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    first = _publish(db, manifest)
    after = authority_fingerprint(db)
    second = _publish(db, manifest)
    assert first["published"] is True
    assert second == {"run_id": first["run_id"], "published": False, "no_op": True}
    assert authority_fingerprint(db) == after
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM external_import_runs").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM external_observations").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM external_facts").fetchone()[0] == 1
    con.close()


def test_declared_manifest_checksum_is_exact(tmp_path: Path) -> None:
    db = _db(tmp_path)
    before = authority_fingerprint(db)
    with pytest.raises(ExternalIngestError, match="manifest_checksum_mismatch"):
        publish_bounded_cohort(db, _manifest(), input_manifest_checksum="0" * 64)
    assert authority_fingerprint(db) == before


@pytest.mark.parametrize("field", ["source_definition_checksum", "quality_policy_version"])
def test_stale_registry_binding_fails_without_writes(tmp_path: Path, field: str) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    manifest[field] = "0" * 64 if field.endswith("checksum") else "external-source-quality-v0"
    before = authority_fingerprint(db)
    with pytest.raises(ExternalIngestError):
        _publish(db, manifest)
    assert authority_fingerprint(db) == before


@pytest.mark.parametrize("fault_at", ["after_observations", "after_facts"])
def test_fault_injection_rolls_back_entire_publication(tmp_path: Path, fault_at: str) -> None:
    db = _db(tmp_path)
    before = authority_fingerprint(db)
    with pytest.raises(RuntimeError, match="fault injection"):
        _publish(db, _manifest(), fault_at=fault_at)
    assert authority_fingerprint(db) == before


def test_conflicting_real_source_fact_appends_conflict_events(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = _manifest()
    first_result = _publish(db, first)
    second = deepcopy(first)
    second["observed_at"] = "2026-07-18T09:00:00Z"
    second["ingested_at"] = "2026-07-18T09:01:00Z"
    second["observations"][0]["key"] = "python-3.15.0"
    second["observations"][0]["value"]["version"] = "3.15.0"
    second["facts"][0]["key"] = "python-latest-conflicting"
    second["facts"][0]["value"] = "3.15.0"
    second["facts"][0]["observation_keys"] = ["python-3.15.0"]
    second_result = _publish(db, second)
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT COUNT(*) FROM external_lifecycle_events WHERE event_type='conflicted'"
    ).fetchone()[0] == 2
    assert con.execute(
        "SELECT lifecycle FROM external_facts WHERE fact_id=?", (second_result["fact_ids"][0],)
    ).fetchone()[0] == "conflict"
    assert first_result["fact_ids"][0] != second_result["fact_ids"][0]
    con.close()


def test_expired_fact_is_published_with_stale_event(tmp_path: Path) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    manifest["facts"][0]["valid_to"] = "2026-07-18T07:00:00Z"
    manifest["observations"][0]["valid_to"] = "2026-07-18T07:00:00Z"
    result = _publish(db, manifest)
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT lifecycle FROM external_facts WHERE fact_id=?", (result["fact_ids"][0],)
    ).fetchone()[0] == "stale"
    assert con.execute(
        "SELECT event_type FROM external_lifecycle_events WHERE fact_id=? ORDER BY sequence",
        (result["fact_ids"][0],),
    ).fetchall() == [("created",), ("staled",)]
    con.close()
