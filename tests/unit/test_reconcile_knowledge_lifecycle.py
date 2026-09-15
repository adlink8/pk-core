"""Phase 22-01: lifecycle reconcile (zero DELETE, dry-run default)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.reconcile_knowledge_lifecycle import (
    ACTION_KEEP_CURRENT,
    ACTION_MARK_CONFLICT,
    ACTION_MARK_SUPERSEDED,
    ACTION_NOOP,
    answer_jaccard,
    apply_actions,
    propose_actions_for_group,
    reconcile_knowledge_lifecycle,
)
from personal_knowledge.application.ku import build_parser, main as ku_main


# Minimal schema: no unit_type CHECK so fixtures stay simple.
_SCHEMA = """
CREATE TABLE canonical_knowledge_units (
    canonical_unit_id   TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,
    unit_type       TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    confidence      REAL NOT NULL,
    lifecycle       TEXT NOT NULL DEFAULT 'current',
    status          TEXT NOT NULL DEFAULT 'current',
    version         INTEGER NOT NULL DEFAULT 1,
    run_id          TEXT NOT NULL,
    merge_reason    TEXT,
    supersedes_id   TEXT,
    created_at      TEXT NOT NULL
);
"""


def _insert(
    con: sqlite3.Connection,
    *,
    cid: str,
    subject: str,
    unit_type: str = "preference",
    answer: str,
    created_at: str,
    lifecycle: str = "current",
    question: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO canonical_knowledge_units VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cid,
            subject,
            unit_type,
            question or f"q about {subject}",
            answer,
            0.9,
            lifecycle,
            "current",
            1,
            "run_test",
            "single",
            None,
            created_at,
        ),
    )


def _setup_db(tmp_path: Path) -> Path:
    db = tmp_path / "reconcile.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return db


def test_jaccard_identical() -> None:
    assert answer_jaccard("hello world", "hello world") == 1.0


def test_jaccard_disjoint() -> None:
    assert answer_jaccard("alpha beta", "gamma delta") == 0.0


def test_propose_similar_supersedes_older() -> None:
    """High similarity: newest keep_current; older mark_superseded."""
    units = [
        {
            "canonical_unit_id": "cu_old",
            "subject": "Shell",
            "unit_type": "preference",
            "answer": "I prefer PowerShell for Windows automation scripts",
            "lifecycle": "current",
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "canonical_unit_id": "cu_new",
            "subject": "Shell",
            "unit_type": "preference",
            "answer": "I prefer PowerShell for Windows automation scripts daily",
            "lifecycle": "current",
            "created_at": "2026-06-01T00:00:00Z",
        },
    ]
    # Ensure high similarity
    sim = answer_jaccard(units[0]["answer"], units[1]["answer"])
    assert sim >= 0.85

    actions = propose_actions_for_group(units)
    by_id = {a.canonical_unit_id: a for a in actions}
    assert by_id["cu_new"].action == ACTION_KEEP_CURRENT
    assert by_id["cu_old"].action == ACTION_MARK_SUPERSEDED
    assert by_id["cu_old"].supersedes_id == "cu_new"
    assert by_id["cu_old"].lifecycle_after == "superseded"


def test_propose_low_similarity_conflict() -> None:
    """Low similarity both current → mark_conflict on both."""
    units = [
        {
            "canonical_unit_id": "cu_a",
            "subject": "Editor",
            "unit_type": "preference",
            "answer": "exclusively use vim keybindings forever",
            "lifecycle": "current",
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "canonical_unit_id": "cu_b",
            "subject": "Editor",
            "unit_type": "preference",
            "answer": "only jetbrains products for java work",
            "lifecycle": "current",
            "created_at": "2026-02-01T00:00:00Z",
        },
    ]
    sim = answer_jaccard(units[0]["answer"], units[1]["answer"])
    assert sim < 0.4

    actions = propose_actions_for_group(units)
    assert all(a.action == ACTION_MARK_CONFLICT for a in actions)
    assert {a.lifecycle_after for a in actions} == {"conflict"}


def test_propose_singleton_noop() -> None:
    units = [
        {
            "canonical_unit_id": "cu1",
            "subject": "Solo",
            "unit_type": "habit",
            "answer": "one answer only",
            "lifecycle": "current",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    actions = propose_actions_for_group(units)
    assert len(actions) == 1
    assert actions[0].action == ACTION_NOOP


def test_dry_run_makes_no_changes(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    con = sqlite3.connect(str(db))
    _insert(
        con,
        cid="cu_old",
        subject="Shell",
        answer="I prefer PowerShell for Windows automation scripts",
        created_at="2026-01-01T00:00:00Z",
    )
    _insert(
        con,
        cid="cu_new",
        subject="Shell",
        answer="I prefer PowerShell for Windows automation scripts daily",
        created_at="2026-06-01T00:00:00Z",
    )
    con.commit()
    before = list(
        con.execute(
            "SELECT canonical_unit_id, lifecycle, supersedes_id "
            "FROM canonical_knowledge_units ORDER BY canonical_unit_id"
        )
    )
    count_before = con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
    con.close()

    report = reconcile_knowledge_lifecycle(db, write=False, dry_run=True)
    assert report.dry_run is True
    assert report.write is False
    assert report.counts.get(ACTION_MARK_SUPERSEDED, 0) >= 1

    con = sqlite3.connect(str(db))
    after = list(
        con.execute(
            "SELECT canonical_unit_id, lifecycle, supersedes_id "
            "FROM canonical_knowledge_units ORDER BY canonical_unit_id"
        )
    )
    count_after = con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
    con.close()
    assert before == after
    assert count_before == count_after


def test_write_mode_does_not_reduce_count(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    con = sqlite3.connect(str(db))
    _insert(
        con,
        cid="cu_old",
        subject="Shell",
        answer="I prefer PowerShell for Windows automation scripts",
        created_at="2026-01-01T00:00:00Z",
    )
    _insert(
        con,
        cid="cu_new",
        subject="Shell",
        answer="I prefer PowerShell for Windows automation scripts daily",
        created_at="2026-06-01T00:00:00Z",
    )
    _insert(
        con,
        cid="cu_x",
        subject="Editor",
        answer="exclusively use vim keybindings forever",
        created_at="2026-01-01T00:00:00Z",
    )
    _insert(
        con,
        cid="cu_y",
        subject="Editor",
        answer="only jetbrains products for java work",
        created_at="2026-02-01T00:00:00Z",
    )
    con.commit()
    count_before = con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
    con.close()

    report = reconcile_knowledge_lifecycle(db, write=True, dry_run=False)
    assert report.write is True
    assert report.row_count_after == report.row_count_before == count_before

    con = sqlite3.connect(str(db))
    count_after = con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
    assert count_after == count_before  # NEVER DELETE

    old = con.execute(
        "SELECT lifecycle, supersedes_id FROM canonical_knowledge_units "
        "WHERE canonical_unit_id='cu_old'"
    ).fetchone()
    new = con.execute(
        "SELECT lifecycle, supersedes_id FROM canonical_knowledge_units "
        "WHERE canonical_unit_id='cu_new'"
    ).fetchone()
    assert old[0] == "superseded"
    assert old[1] == "cu_new"
    assert new[0] == "current"

    conflicts = con.execute(
        "SELECT COUNT(*) FROM canonical_knowledge_units WHERE lifecycle='conflict'"
    ).fetchone()[0]
    assert conflicts == 2
    con.close()


def test_artifact_written(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    con = sqlite3.connect(str(db))
    _insert(
        con,
        cid="cu1",
        subject="Solo",
        answer="only one",
        created_at="2026-01-01T00:00:00Z",
    )
    con.commit()
    con.close()
    art = tmp_path / "out" / "report.json"
    report = reconcile_knowledge_lifecycle(db, write=False, artifact=art)
    assert art.exists()
    data = json.loads(art.read_text(encoding="utf-8"))
    assert data["units_scanned"] == 1
    assert report.artifact == str(art)


def test_cli_write_requires_i_know(capsys) -> None:
    code = ku_main(["reconcile", "--write"])
    assert code == 2
    err = capsys.readouterr().err
    assert "i-know" in err.lower() or "--i-know" in err


def test_cli_parser_reconcile_exists() -> None:
    p = build_parser()
    with pytest.raises(SystemExit) as ei:
        p.parse_args(["reconcile", "--help"])
    assert ei.value.code == 0


def test_cli_dry_run_json(tmp_path: Path, capsys) -> None:
    db = _setup_db(tmp_path)
    con = sqlite3.connect(str(db))
    _insert(
        con,
        cid="cu1",
        subject="Solo",
        answer="only one",
        created_at="2026-01-01T00:00:00Z",
    )
    con.commit()
    con.close()
    code = ku_main(["reconcile", "--dry-run", "--db", str(db), "--max-subjects", "5"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["dry_run"] is True
    assert data["write"] is False
    assert "counts" in data
    assert "sample_actions" in data


def test_apply_actions_no_delete(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    _insert(
        con,
        cid="cu_old",
        subject="Shell",
        answer="I prefer PowerShell for Windows automation scripts",
        created_at="2026-01-01T00:00:00Z",
    )
    _insert(
        con,
        cid="cu_new",
        subject="Shell",
        answer="I prefer PowerShell for Windows automation scripts daily",
        created_at="2026-06-01T00:00:00Z",
    )
    con.commit()
    units = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM canonical_knowledge_units WHERE lifecycle='current'"
        )
    ]
    actions = propose_actions_for_group(units)
    n = apply_actions(con, actions, write=False)
    assert n == 0
    assert con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0] == 2
    apply_actions(con, actions, write=True)
    assert con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0] == 2
    con.close()
