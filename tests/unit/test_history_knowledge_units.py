"""Phase 22-02: growth-line history query (read-only multi-version)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.history_knowledge_units import (
    GROWTH_LINE_LIFECYCLES,
    format_table,
    list_history_for_subject,
)
from personal_knowledge.application.ku import build_parser, main as ku_main


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
    answer: str,
    created_at: str,
    lifecycle: str = "current",
    supersedes_id: str | None = None,
    confidence: float = 0.9,
    unit_type: str = "preference",
) -> None:
    con.execute(
        "INSERT INTO canonical_knowledge_units VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cid,
            subject,
            unit_type,
            f"q about {subject}",
            answer,
            confidence,
            lifecycle,
            "current",
            1,
            "run_test",
            "single",
            supersedes_id,
            created_at,
        ),
    )


def _setup_db(tmp_path: Path) -> Path:
    db = tmp_path / "history.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(_SCHEMA)
    # Multi-version growth line for subject "Shell"
    _insert(
        con,
        cid="cu_old",
        subject="Shell",
        answer="Prefer bash on Linux servers only",
        created_at="2026-01-01T00:00:00Z",
        lifecycle="superseded",
        supersedes_id=None,
        confidence=0.7,
    )
    _insert(
        con,
        cid="cu_mid",
        subject="Shell",
        answer="Prefer PowerShell for Windows automation",
        created_at="2026-03-01T00:00:00Z",
        lifecycle="superseded",
        supersedes_id="cu_old",
        confidence=0.85,
    )
    _insert(
        con,
        cid="cu_new",
        subject="Shell",
        answer="Prefer PowerShell for Windows automation daily",
        created_at="2026-06-01T00:00:00Z",
        lifecycle="current",
        supersedes_id="cu_mid",
        confidence=0.95,
    )
    _insert(
        con,
        cid="cu_other",
        subject="OtherSubject",
        answer="unrelated",
        created_at="2026-07-01T00:00:00Z",
        lifecycle="current",
    )
    # Non-growth lifecycle (e.g. draft) — excluded unless include_all
    _insert(
        con,
        cid="cu_draft",
        subject="Shell",
        answer="draft only",
        created_at="2026-05-01T00:00:00Z",
        lifecycle="draft",
    )
    con.commit()
    con.close()
    return db


def test_growth_line_returns_multi_version_ordered(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    report = list_history_for_subject(db, "Shell", limit=10)
    assert report.count == 3  # current + 2 superseded; draft excluded
    ids = [r["unit_id"] for r in report.rows]
    assert ids == ["cu_new", "cu_mid", "cu_old"]  # created_at desc
    assert report.rows[0]["lifecycle"] == "current"
    assert report.rows[0]["is_current_value"] is True
    assert sum(row["is_current_value"] for row in report.rows) == 1
    assert report.rows[0]["supersedes_id"] == "cu_mid"
    assert report.rows[1]["lifecycle"] == "superseded"
    assert "PowerShell" in report.rows[0]["answer_snippet"]
    assert set(report.lifecycles) == set(GROWTH_LINE_LIFECYCLES)


def test_limit_caps_rows(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    report = list_history_for_subject(db, "Shell", limit=1)
    assert report.count == 1
    assert report.rows[0]["unit_id"] == "cu_new"


def test_include_all_lifecycle_includes_draft(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    report = list_history_for_subject(
        db, "Shell", limit=10, include_all_lifecycle=True
    )
    ids = {r["unit_id"] for r in report.rows}
    assert "cu_draft" in ids
    assert report.count == 4


def test_format_table_marks_only_current_value(tmp_path: Path) -> None:
    report = list_history_for_subject(_setup_db(tmp_path), "Shell", limit=10)
    table = format_table(report.rows)
    assert table.count("← 当前值") == 1
    assert "cu_new" in table


def test_missing_subject_empty(tmp_path: Path) -> None:
    db = _setup_db(tmp_path)
    report = list_history_for_subject(db, "NoSuchSubject", limit=5)
    assert report.count == 0
    assert report.rows == []


def test_subject_required() -> None:
    with pytest.raises(ValueError):
        list_history_for_subject(Path("x.sqlite"), "  ")


def test_cli_history_on_fixture(tmp_path: Path, capsys) -> None:
    db = _setup_db(tmp_path)
    code = ku_main(
        ["history", "--subject", "Shell", "--limit", "5", "--db", str(db), "--json"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "cu_new" in out
    assert "superseded" in out
    assert "cu_draft" not in out


def test_cli_parser_history_exists() -> None:
    p = build_parser()
    with pytest.raises(SystemExit) as ei:
        p.parse_args(["history", "--help"])
    assert ei.value.code == 0
    args = p.parse_args(["history", "--subject", "项目", "--limit", "5"])
    assert args.command == "history"
    assert args.subject == "项目"
    assert args.limit == 5
    assert args.include_all_lifecycle is False
