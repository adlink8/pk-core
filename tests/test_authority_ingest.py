# -*- coding: utf-8 -*-
"""authority_ingest 门禁与隔离机制的离线测试。

全部使用 tmp  fixtures，不触碰任何真实库。覆盖：
* 硬门逐条（消息丢失/时间倒挂/密钥残留/空会话/重复源会话/孤儿消息）→ HardGateError；
* 软门逐条（时间戳 null/0/epoch 秒/非 ISO、消息盈余）→ quarantine 表 + 去重计数；
* canonical 加载器的 quarantine 排除钩子（表存在/不存在两条路径）；
* 审计库独立落账（run + findings）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from personal_knowledge.application.conversation import authority_ingest as ai
from personal_knowledge.application.conversation.authority_ingest import (
    GateFinding,
    HardGateError,
    check_normalized,
    write_quarantine,
)

_SESSIONS_DDL = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    source_session_id TEXT NOT NULL,
    agent TEXT,
    started_at TEXT,
    ended_at TEXT,
    message_count INTEGER,
    user_message_count INTEGER,
    parent_session_id TEXT,
    relationship_type TEXT,
    source_session_ref TEXT,
    file_hash TEXT,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    secret_leak_count INTEGER NOT NULL DEFAULT 0,
    evidence_eligible INTEGER NOT NULL DEFAULT 1,
    excluded INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    evidence_scope TEXT NOT NULL DEFAULT 'user',
    ineligible_reasons_json TEXT
)
"""
_MESSAGES_DDL = """
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    content_length INTEGER
)
"""


def _make_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(_SESSIONS_DDL)
    con.execute(_MESSAGES_DDL)
    con.commit()
    con.close()


def _add_session(con: sqlite3.Connection, sid: str, src: str, agent: str = "codex",
                 started: str | None = "2026-09-01T10:00:00",
                 ended: str | None = "2026-09-01T11:00:00",
                 declared: int | None = 2, secret: int = 0,
                 eligible: int = 1, excluded: int = 0) -> None:
    con.execute(
        "INSERT INTO sessions (session_id, source_session_id, agent, started_at,"
        " ended_at, message_count, secret_leak_count, evidence_eligible, excluded)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, src, agent, started, ended, declared, secret, eligible, excluded),
    )


def _add_messages(con: sqlite3.Connection, sid: str, n: int,
                  with_content: bool = True) -> None:
    for i in range(n):
        con.execute(
            "INSERT INTO messages (message_id, session_id, ordinal, role, content,"
            " content_length) VALUES (?,?,?,?,?,?)",
            (f"{sid}:m{i}", sid, i, "user",
             f"hello {i}" if with_content else None,
             7 if with_content else 0),
        )


# ------------------------------------------------------------------ 硬门


def test_hard_gate_message_loss(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1", declared=5)
    _add_messages(con, "s1", 1)
    con.commit()
    con.close()

    hard, soft = check_normalized(db)
    assert [f.code for f in hard] == ["message_loss"]
    assert soft == []


def test_hard_gate_temporal_inversion(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1",
                 started="2026-09-01T12:00:00", ended="2026-09-01T10:00:00")
    _add_messages(con, "s1", 2)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert [f.code for f in hard] == ["temporal_inversion"]


def test_hard_gate_secret_content_leak(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1", secret=2)
    _add_messages(con, "s1", 2)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert [f.code for f in hard] == ["secret_content_leak"]


def test_hard_gate_empty_eligible_session(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1")
    _add_messages(con, "s1", 3, with_content=False)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert [f.code for f in hard] == ["empty_eligible_session"]


def test_hard_gate_duplicate_source_session(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1")
    _add_session(con, "s2", "src1")
    _add_messages(con, "s1", 2)
    _add_messages(con, "s2", 2)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert "duplicate_source_session" in [f.code for f in hard]


def test_hard_gate_orphan_messages(tmp_path):
    db = tmp_path / "norm.sqlite"
    con = sqlite3.connect(str(db))
    con.execute(_SESSIONS_DDL)
    con.execute(_MESSAGES_DDL)
    _add_session(con, "s1", "src1")
    _add_messages(con, "s1", 2)
    con.execute(
        "INSERT INTO messages (message_id, session_id, ordinal, role, content)"
        " VALUES ('ghost', 'no-such-session', 0, 'user', 'x')"
    )
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert [f.code for f in hard] == ["orphan_messages"]


def test_hard_gate_missing_normalized_db(tmp_path):
    with pytest.raises(HardGateError) as ei:
        check_normalized(tmp_path / "absent.sqlite")
    assert ei.value.findings[0].code == "normalized_missing"


def test_message_loss_skips_ineligible_sessions(tmp_path):
    """secret/excluded/deleted 会话被合法 tombstone（0 消息行），其声明
    message_count 保留源值——不得误报 message_loss（dry-run 实测回归）。"""
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    # 真实 normalized 构建器对 secret/excluded 会话置 eligible=0（0 消息行）。
    _add_session(con, "s-secret", "src-secret", declared=448, secret=2,
                 eligible=0)
    _add_session(con, "s-excluded", "src-excluded", declared=1154, excluded=1)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert hard == []


def test_temporal_inversion_compares_time_values_not_strings(tmp_path):
    """ended '…37.621Z' 晚于 started '…37.62Z'：字符串比较会误判，必须按时间值。"""
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1",
                 started="2026-04-24T13:44:37.62Z",
                 ended="2026-04-24T13:44:37.621Z")
    _add_messages(con, "s1", 2)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert hard == []


def test_temporal_inversion_real_inversion_still_caught(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1",
                 started="2026-04-24T13:44:38Z",
                 ended="2026-04-24T13:44:37.621Z")
    _add_messages(con, "s1", 2)
    con.commit()
    con.close()

    hard, _ = check_normalized(db)
    assert [f.code for f in hard] == ["temporal_inversion"]


# ------------------------------------------------------------------ 软门


@pytest.mark.parametrize("bad_ts,code", [
    (None, "started_at_null"),
    ("", "started_at_empty"),
    ("0", "started_at_zero"),
    ("1780575834", "started_at_epoch_seconds"),
    ("not-a-time", "started_at_unparseable"),
])
def test_soft_gate_bad_started_at(tmp_path, bad_ts, code):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1", started=bad_ts)
    _add_messages(con, "s1", 2)
    con.commit()
    con.close()

    hard, soft = check_normalized(db)
    assert hard == []
    assert code in [f.code for f in soft]


def test_soft_gate_message_surplus_and_bad_ended(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1", declared=1, ended="0")
    _add_messages(con, "s1", 3)
    con.commit()
    con.close()

    _, soft = check_normalized(db)
    codes = [f.code for f in soft]
    assert "message_surplus" in codes
    assert "ended_at_zero" in codes


def test_soft_gate_does_not_flag_good_session(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s1", "src1")
    _add_messages(con, "s1", 2)
    con.commit()
    con.close()

    hard, soft = check_normalized(db)
    assert hard == [] and soft == []


# ------------------------------------------------------- quarantine 与排除钩子


def test_write_quarantine_dedups_sessions(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    findings = [
        GateFinding("started_at_zero", "soft", "s1", "src1", "codex", "d"),
        GateFinding("ended_at_zero", "soft", "s1", "src1", "codex", "d"),
        GateFinding("started_at_null", "soft", "s2", "src2", "grok", "d"),
    ]
    n = write_quarantine(db, findings, "run-1")
    assert n == 2

    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT session_id, gate_code FROM ingest_quarantine ORDER BY session_id"
    ).fetchall()
    con.close()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {"s1", "s2"}


def test_write_quarantine_idempotent(tmp_path):
    db = tmp_path / "norm.sqlite"
    _make_db(db)
    findings = [GateFinding("started_at_zero", "soft", "s1", "src1", "codex", "d")]
    write_quarantine(db, findings, "run-1")
    write_quarantine(db, findings, "run-2")  # 同主键替换，不翻倍

    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM ingest_quarantine").fetchone()[0]
    run_ids = [r[0] for r in con.execute("SELECT run_id FROM ingest_quarantine")]
    con.close()
    assert n == 1
    assert run_ids == ["run-2"]


def test_canonical_loader_excludes_quarantined(tmp_path):
    from personal_knowledge.application.conversation import (
        build_canonical_agent_conversations as bcc,
    )

    db = tmp_path / "norm.sqlite"
    _make_db(db)
    con = sqlite3.connect(str(db))
    _add_session(con, "s-keep", "src-keep")
    _add_session(con, "s-drop", "src-drop", started="0")
    _add_messages(con, "s-keep", 2)
    _add_messages(con, "s-drop", 2)
    con.commit()
    con.close()

    # 无隔离表：两个都加载。
    loaded = bcc._load_agentsview_sessions(db)
    assert {s["session_id"] for s in loaded} == {"s-keep", "s-drop"}

    # 写入隔离后：s-drop 被排除。
    write_quarantine(db, [GateFinding(
        "started_at_zero", "soft", "s-drop", "src-drop", "grok", "d")], "run-x")
    loaded = bcc._load_agentsview_sessions(db)
    assert [s["session_id"] for s in loaded] == ["s-keep"]


# ------------------------------------------------------------------ 发布前收缩守卫


def test_shrink_guard_blocks_when_projection_loses_sessions(monkeypatch):
    monkeypatch.setattr(ai, "_count_published", lambda: 2959)
    monkeypatch.setattr(ai, "_project_canonical_sessions", lambda: 2351)
    with pytest.raises(HardGateError) as ei:
        ai._assert_no_shrink()
    f = ei.value.findings[0]
    assert f.code == "authority_shrink_guard"
    assert "2959" in f.detail and "2351" in f.detail


def test_shrink_guard_passes_when_projection_grows(monkeypatch):
    monkeypatch.setattr(ai, "_count_published", lambda: 2959)
    monkeypatch.setattr(ai, "_project_canonical_sessions", lambda: 3100)
    assert ai._assert_no_shrink() == (2959, 3100)


def test_shrink_guard_passes_on_equal_counts(monkeypatch):
    monkeypatch.setattr(ai, "_count_published", lambda: 2959)
    monkeypatch.setattr(ai, "_project_canonical_sessions", lambda: 2959)
    assert ai._assert_no_shrink() == (2959, 2959)


# ------------------------------------------------------------------ 审计


def test_record_run_writes_independent_audit(tmp_path, monkeypatch):
    audit = tmp_path / "ingest_audit.sqlite"
    monkeypatch.setattr(ai, "AUDIT_DB", audit)

    report = ai.IngestReport(run_id="run-1", status="blocked",
                             started_at="t0", finished_at="t1", duration_s=1.5,
                             eligible_sessions=10)
    report.hard_findings = [GateFinding(
        "message_loss", "hard", "s1", "src1", "codex", "declared 5 got 1")]
    report.error = "hard gate blocked"
    ai.record_run(report)

    con = sqlite3.connect(str(audit))
    run = con.execute(
        "SELECT status, hard_count, error FROM ingest_runs WHERE run_id='run-1'"
    ).fetchone()
    findings = con.execute(
        "SELECT severity, gate_code, session_id FROM ingest_findings"
    ).fetchall()
    con.close()
    assert run == ("blocked", 1, "hard gate blocked")
    assert findings == [("hard", "message_loss", "s1")]


def test_main_rejects_write_and_dry_run_together(capsys):
    rc = ai.main(["--write", "--dry-run"])
    assert rc == 2


def test_main_dry_run_default_is_write_false(monkeypatch):
    """默认（无参数）必须是 dry-run：绝不意外发布权威库。"""
    seen = {}

    def fake_run_ingest(*, write=False, dry_run=False):
        seen["write"] = write
        seen["dry_run"] = dry_run
        return ai.IngestReport(run_id="r", status="ok")

    monkeypatch.setattr(ai, "run_ingest", fake_run_ingest)
    rc = ai.main([])
    assert rc == 0
    assert seen == {"write": False, "dry_run": True}
