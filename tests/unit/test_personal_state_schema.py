from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import (
    SCHEMA_SQL,
)
from personal_knowledge.intelligence.schema import canonical_json


_DIGEST = hashlib.sha256(b"fixture").hexdigest()


def _database(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "personal-state.sqlite")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    return con


def _seed_snapshot(
    con: sqlite3.Connection,
    *,
    snapshot_id: str = "ss1",
    snapshot_hash: str = "snapshot-hash-1",
    artifact_version_id: str = "av1",
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO artifact_registry_entries VALUES (?,?,?,?,?,?)",
        ("a.personal_change", "A", "personal_change_analysis", "R4", "definition", "now"),
    )
    registry_id = f"s.fixture.{artifact_version_id}"
    role = f"fixture_{artifact_version_id}"
    con.execute(
        "INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)",
        (registry_id, "S", role, "R4", "definition", "now"),
    )
    con.execute(
        "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            artifact_version_id,
            registry_id,
            "v1",
            f"checksum-{artifact_version_id}",
            "sqlite_table",
            "fixture",
            "published",
            "R4",
            None,
            None,
            "{}",
            "now",
        ),
    )
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, "{}", snapshot_hash, "validated", "gate", "now", "now"),
    )
    con.execute(
        "INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)",
        (snapshot_id, role, artifact_version_id),
    )


def _insert_run(
    con: sqlite3.Connection,
    run_id: str,
    snapshot_id: str,
    snapshot_hash: str,
) -> None:
    con.execute(
        "INSERT INTO personal_state_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "a.personal_change",
            snapshot_id,
            snapshot_hash,
            "producer-v1",
            "{}",
            _DIGEST,
            "{}",
            hashlib.sha256(run_id.encode()).hexdigest(),
            "committed",
            "now",
        ),
    )


def _insert_assertion(
    con: sqlite3.Connection,
    assertion_id: str,
    run_id: str,
    *,
    kind: str = "goal",
    provenance: str = "observation",
) -> None:
    con.execute(
        "INSERT INTO personal_state_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            assertion_id,
            run_id,
            kind,
            provenance,
            "subject",
            "work",
            "personal",
            "complete_target",
            '"D"',
            "2026-07-17T00:00:00Z",
            None,
            "2026-07-17T00:00:00Z",
            0.8,
            "",
            "current",
            "{}",
            _DIGEST,
            "now",
        ),
    )


def test_personal_state_schema_is_idempotent_and_fk_clean(tmp_path: Path) -> None:
    con = _database(tmp_path)
    con.executescript(SCHEMA_SQL)
    tables = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "personal_state_runs",
        "personal_state_publications",
        "personal_state_assertions",
        "personal_state_evidence",
        "personal_state_changes",
        "personal_state_risks",
    }.issubset(tables)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


@pytest.mark.parametrize(
    ("kind", "provenance"),
    [("recommendation", "fact"), ("goal", "guess")],
)
def test_assertion_kind_and_provenance_are_constrained(
    tmp_path: Path, kind: str, provenance: str
) -> None:
    con = _database(tmp_path)
    _seed_snapshot(con)
    _insert_run(con, "run1", "ss1", "snapshot-hash-1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_assertion(con, "assertion1", "run1", kind=kind, provenance=provenance)
    con.close()


def test_empty_snapshot_hash_and_dangling_evidence_are_rejected(tmp_path: Path) -> None:
    con = _database(tmp_path)
    _seed_snapshot(con)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(con, "bad-run", "ss1", "")

    _insert_run(con, "run1", "ss1", "snapshot-hash-1")
    _insert_assertion(con, "assertion1", "run1")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO personal_state_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "e1",
                "missing-assertion",
                "ss1",
                "snapshot-hash-1",
                "fixture_av1",
                "av1",
                "knowledge_unit",
                "ku1",
                "evidence-hash",
                "eligible",
                "R4",
                "now",
            ),
        )
    con.close()


def test_evidence_must_match_the_assertion_run_snapshot(tmp_path: Path) -> None:
    con = _database(tmp_path)
    _seed_snapshot(con)
    _seed_snapshot(
        con,
        snapshot_id="ss2",
        snapshot_hash="snapshot-hash-2",
        artifact_version_id="av2",
    )
    _insert_run(con, "run1", "ss1", "snapshot-hash-1")
    _insert_assertion(con, "assertion1", "run1")
    with pytest.raises(sqlite3.IntegrityError, match="evidence snapshot mismatch"):
        con.execute(
            "INSERT INTO personal_state_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "e1",
                "assertion1",
                "ss2",
                "snapshot-hash-2",
                "fixture_av2",
                "av2",
                "knowledge_unit",
                "ku1",
                "evidence-hash",
                "eligible",
                "R4",
                "now",
            ),
        )
    con.close()


def test_change_and_risk_refs_must_belong_to_their_run(tmp_path: Path) -> None:
    con = _database(tmp_path)
    _seed_snapshot(con)
    _seed_snapshot(
        con,
        snapshot_id="ss2",
        snapshot_hash="snapshot-hash-2",
        artifact_version_id="av2",
    )
    _insert_run(con, "run1", "ss1", "snapshot-hash-1")
    _insert_run(con, "run2", "ss2", "snapshot-hash-2")
    _insert_assertion(con, "a1", "run1")
    _insert_assertion(con, "a2", "run2")

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO personal_state_changes VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("c1", "run1", "updated", "a1", "a2", 0.8, "", "{}", _DIGEST, "now"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO personal_state_risks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "run1", "a2", "rule", "v1", "low", 0.8, "", "{}", _DIGEST, "now"),
        )
    con.close()


def test_committed_personal_state_rows_reject_updates_and_deletes(tmp_path: Path) -> None:
    con = _database(tmp_path)
    _seed_snapshot(con)
    _insert_run(con, "run1", "ss1", "snapshot-hash-1")
    _insert_assertion(con, "a1", "run1")
    con.execute(
        "INSERT INTO personal_state_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "e1",
            "a1",
            "ss1",
            "snapshot-hash-1",
            "fixture_av1",
            "av1",
            "knowledge_unit",
            "ku1",
            "evidence-hash",
            "eligible",
            "R4",
            "now",
        ),
    )
    con.execute(
        "INSERT INTO personal_state_changes VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("c1", "run1", "created", None, "a1", 0.8, "", "{}", _DIGEST, "now"),
    )
    con.execute(
        "INSERT INTO personal_state_risks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "run1", "a1", "rule", "v1", "low", 0.8, "", "{}", _DIGEST, "now"),
    )
    con.commit()

    mutations = (
        ("UPDATE personal_state_assertions SET confidence=0.9 WHERE assertion_id='a1'", "personal state assertions are immutable"),
        ("DELETE FROM personal_state_evidence WHERE evidence_id='e1'", "personal state evidence is immutable"),
        ("UPDATE personal_state_changes SET confidence=0.9 WHERE change_id='c1'", "personal state changes are immutable"),
        ("DELETE FROM personal_state_risks WHERE risk_id='r1'", "personal state risks are immutable"),
        ("DELETE FROM personal_state_runs WHERE run_id='run1'", "personal state runs are immutable"),
    )
    for statement, message in mutations:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            con.execute(statement)
    con.close()


def test_derived_risk_severity_persists_without_translation(tmp_path: Path) -> None:
    from personal_knowledge.intelligence.changes import TrendSample, derive_risk, derive_trend
    from personal_knowledge.intelligence.state_projection import StateKey

    con = _database(tmp_path)
    _seed_snapshot(con)
    _insert_run(con, "run1", "ss1", "snapshot-hash-1")
    _insert_assertion(con, "a1", "run1", kind="constraint")
    key = StateKey("constraint", "subject", "work", "personal", "complete_target")
    trend = derive_trend(tuple(
        TrendSample(
            assertion_id=f"a{i}", key=key, value=float(i), unit="count",
            observed_at=f"2026-0{i}-01T00:00:00Z", evidence_refs=(f"ku{i}",),
            evidence_eligible=True, confidence=0.9,
        )
        for i in range(1, 4)
    ))
    risk = derive_risk(trend, rule_id="increasing_constraint_pressure")
    payload = {"inference_id": risk.inference_id, "severity": risk.severity}
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    con.execute(
        "INSERT INTO personal_state_risks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r-derived", "run1", "a1", risk.rule_id, risk.rule_version,
         risk.severity, risk.confidence, "", canonical_json(payload), digest, "now"),
    )
    assert con.execute(
        "SELECT severity FROM personal_state_risks WHERE risk_id='r-derived'"
    ).fetchone()[0] == "medium"
    con.close()
