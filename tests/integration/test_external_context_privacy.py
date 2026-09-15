from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.external_context.ingest import (
    ExternalIngestError,
    authority_fingerprint,
    publish_bounded_cohort,
)
from personal_knowledge.external_context.lifecycle import (
    ExternalLifecycleError,
    append_lifecycle_event,
    project_fact_lifecycle,
)
from personal_knowledge.external_context.migrate import migrate
from personal_knowledge.external_context.registry import source_definitions
from personal_knowledge.external_context.schema import checksum


def _manifest() -> dict:
    source = source_definitions()[1]
    return {
        "schema_version": "external_context_import_v1", "source_id": source.source_id,
        "source_definition_checksum": source.definition_checksum,
        "quality_policy_version": source.quality_policy_version, "region": "global",
        "observed_at": "2026-07-18T08:00:00Z", "ingested_at": "2026-07-18T08:01:00Z",
        "observations": [{
            "key": "node-24", "kind": "official_release", "value": {"version": "24.0.0"},
            "publication_time": "2026-07-17T12:00:00Z", "valid_from": "2026-07-17T12:00:00Z",
            "valid_to": None, "region": "global",
        }],
        "facts": [{
            "key": "node-latest", "subject": "nodejs", "predicate": "latest_release",
            "value": "24.0.0", "valid_from": "2026-07-17T12:00:00Z", "valid_to": None,
            "region": "global", "source_quality": .98, "fact_confidence": .96,
            "observation_keys": ["node-24"],
        }],
    }


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "external.sqlite"
    migrate(db, write=True)
    return db


def _publish(db: Path, manifest: dict):
    return publish_bounded_cohort(db, manifest, input_manifest_checksum=checksum(manifest))


@pytest.mark.parametrize("bad_key", ["body", "content", "raw_text", "html", "article"])
def test_body_like_payload_is_rejected_without_authority_change(tmp_path: Path, bad_key: str) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    manifest["observations"][0]["value"][bad_key] = "copyrighted page"
    before = authority_fingerprint(db)
    with pytest.raises(ExternalIngestError, match="body_like_field"):
        _publish(db, manifest)
    assert authority_fingerprint(db) == before


@pytest.mark.parametrize("payload", [
    {"api_key": "not-stored"},
    {"label": "password=not-stored"},
])
def test_secret_payload_is_rejected(tmp_path: Path, payload: dict) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    manifest["observations"][0]["value"].update(payload)
    with pytest.raises(ExternalIngestError, match="secret_like"):
        _publish(db, manifest)


def test_manifest_total_size_is_bounded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    manifest["observations"][0]["value"] = {"items": ["x" * 1000] * 1100}
    with pytest.raises(ExternalIngestError, match="manifest_too_large"):
        _publish(db, manifest)


@pytest.mark.parametrize("mutation,code", [
    (lambda m: m.update(region="us"), "unsupported_region"),
    (lambda m: m.update(observed_at="2026-07-18"), "invalid_time"),
    (lambda m: m["facts"][0].update(observation_keys=["missing"]), "unresolved_provenance"),
])
def test_region_time_and_provenance_fail_closed(tmp_path: Path, mutation, code: str) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    mutation(manifest)
    with pytest.raises(ExternalIngestError, match=code):
        _publish(db, manifest)


def test_canonical_rows_are_structured_and_contain_no_raw_body(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _publish(db, _manifest())
    con = sqlite3.connect(db)
    stored = " ".join(str(value) for row in con.execute(
        "SELECT input_manifest_json FROM external_import_runs UNION ALL "
        "SELECT value_json FROM external_observations UNION ALL SELECT value_json FROM external_facts"
    ) for value in row).lower()
    assert all(term not in stored for term in ('"body"', '"content"', '"raw_text"', '"html"'))
    assert json.loads(con.execute("SELECT value_json FROM external_facts").fetchone()[0]) == "24.0.0"
    con.close()


def test_lifecycle_projection_is_reconstructable_and_terminal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _publish(db, _manifest())
    con = sqlite3.connect(db)
    fact_id = result["fact_ids"][0]
    projected = append_lifecycle_event(
        con, fact_id=fact_id, event_type="superseded", occurred_at="2026-07-18T09:00:00Z",
        payload={"replacement_fact_id": "ef_replacement"},
    )
    con.commit()
    assert projected.lifecycle == "superseded"
    assert project_fact_lifecycle(con, fact_id).head_checksum == projected.head_checksum
    with pytest.raises(ExternalLifecycleError, match="terminal_transition"):
        append_lifecycle_event(
            con, fact_id=fact_id, event_type="staled", occurred_at="2026-07-18T10:00:00Z",
        )
    con.close()


def test_lifecycle_checksum_tamper_is_detected_on_projection(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _publish(db, _manifest())
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_external_lifecycle_events_no_update")
    con.execute(
        "UPDATE external_lifecycle_events SET payload_checksum=? WHERE fact_id=?",
        ("0" * 64, result["fact_ids"][0]),
    )
    con.commit()
    with pytest.raises(ExternalLifecycleError, match="checksum_mismatch"):
        project_fact_lifecycle(con, result["fact_ids"][0])
    con.close()
