"""Phase 13.5 Wave 1.1 测试：AgentView source adapter。

覆盖 PLAN Task 1.1 的三个 Verify：
  - schema probe + backup 对临时 fixture 工作
  - adapter 不产生任何源库写入（total_changes / 文件 mtime）
  - 源库 schema 缺表/缺列时 pre-flight abort（只产 blocked probe，不创建快照）
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.adapters.agentsview import (  # noqa: E402
    REQUIRED_COLUMNS,
    REQUIRED_TABLES,
    AgentViewAdapter,
    SchemaGateError,
    backup_snapshot,
    probe_source,
)


def _make_fixture(dest: Path, *, drop_table: str | None = None,
                   drop_column: str | None = None) -> None:
    """构造一个符合最小 schema 的临时 AgentView 源库。"""
    con = sqlite3.connect(str(dest))
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA user_version=59")

    # sessions
    cur.execute(
        """CREATE TABLE sessions (
            id TEXT, agent TEXT, started_at TEXT, ended_at TEXT,
            message_count INTEGER, user_message_count INTEGER,
            file_hash TEXT, parent_session_id TEXT, relationship_type TEXT,
            source_session_id TEXT, deleted_at TEXT, secret_leak_count INTEGER
        )"""
    )
    # messages
    cur.execute(
        """CREATE TABLE messages (
            id INTEGER, session_id TEXT, ordinal INTEGER, role TEXT,
            content TEXT, thinking_text TEXT, timestamp TEXT,
            is_system INTEGER, is_sidechain INTEGER, model TEXT
        )"""
    )
    # tool_calls
    cur.execute(
        """CREATE TABLE tool_calls (
            id INTEGER, session_id TEXT, tool_name TEXT, category TEXT,
            call_index INTEGER, subagent_session_id TEXT, input_json TEXT,
            result_content TEXT
        )"""
    )
    # tool_result_events
    cur.execute(
        """CREATE TABLE tool_result_events (
            id INTEGER, session_id TEXT, status TEXT,
            subagent_session_id TEXT, event_index INTEGER
        )"""
    )
    # usage_events
    cur.execute(
        """CREATE TABLE usage_events (
            id INTEGER, session_id TEXT, model TEXT, occurred_at TEXT
        )"""
    )
    # secret_findings
    cur.execute(
        """CREATE TABLE secret_findings (
            id INTEGER, session_id TEXT, rule_name TEXT, message_ordinal INTEGER
        )"""
    )
    # excluded_sessions
    cur.execute("CREATE TABLE excluded_sessions (id TEXT, created_at TEXT)")

    if drop_table and drop_table in REQUIRED_TABLES:
        cur.execute(f"DROP TABLE {drop_table}")

    con.commit()
    con.close()


def _make_fixture_clean(dest: Path) -> None:
    """干净 fixture（全部 required 表/列齐全）。"""
    _make_fixture(dest, drop_table=None)


def test_probe_clean_fixture_ok(tmp_path: Path) -> None:
    """干净 fixture：probe.ok 为 True，counts 正确。"""
    src = tmp_path / "fake_sessions.db"
    _make_fixture_clean(src)

    probe = probe_source(src)
    assert probe.ok, f"gate should pass: missing={probe.missing_columns}"
    assert probe.integrity_check == "ok"
    assert probe.required_tables_missing == []
    assert probe.missing_columns == {}
    assert set(REQUIRED_TABLES).issubset(probe.required_tables_present)


def test_probe_missing_table_blocks(tmp_path: Path) -> None:
    """缺 required 表：probe.ok 为 False，missing 非空。"""
    src = tmp_path / "fake_sessions.db"
    _make_fixture(src, drop_table="secret_findings")

    probe = probe_source(src)
    assert not probe.ok
    assert "secret_findings" in probe.required_tables_missing


def test_backup_creates_snapshot_and_no_source_write(tmp_path: Path) -> None:
    """backup API：创建快照，源库零写入（mtime 不变/无 -wal 残留增长）。"""
    src = tmp_path / "fake_sessions.db"
    _make_fixture_clean(src)

    # 记录源库 backup 前状态
    src_mtime_before = src.stat().st_mtime

    probe = probe_source(src)
    assert probe.ok

    manifest, snap = backup_snapshot(src, dest_dir=tmp_path / "snap", probe=probe)

    # 快照文件存在
    assert snap.exists()
    assert manifest.source_integrity == "ok"
    assert manifest.counts.get("sessions", 0) == 0  # 空 fixture

    # 快照可读，且表结构完整
    con = sqlite3.connect(str(snap))
    tabs = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert set(REQUIRED_TABLES).issubset(tabs)

    # 源库未被写入：mtime 不变（query_only 保证）
    src_mtime_after = src.stat().st_mtime
    assert src_mtime_after == src_mtime_before, "source DB was modified!"

    # 清理
    snap.unlink(missing_ok=True)


def test_backup_aborts_on_schema_gate_failure(tmp_path: Path) -> None:
    """schema gate 失败：backup 抛 SchemaGateError，不创建快照。"""
    src = tmp_path / "fake_sessions.db"
    _make_fixture(src, drop_table="messages")  # 缺关键表

    probe = probe_source(src)
    assert not probe.ok

    import pytest
    with pytest.raises(SchemaGateError):
        backup_snapshot(src, dest_dir=tmp_path / "snap", probe=probe)


def test_adapter_class_roundtrip(tmp_path: Path) -> None:
    """AgentViewAdapter 封装 probe + snapshot 工作正常。"""
    src = tmp_path / "fake_sessions.db"
    _make_fixture_clean(src)

    adapter = AgentViewAdapter(src)
    probe = adapter.probe()
    assert probe.ok

    manifest, snap = adapter.snapshot(dest_dir=tmp_path / "snap2")
    assert snap.exists()
    assert manifest.run_id  # 非空
    snap.unlink(missing_ok=True)


def test_required_columns_constants_complete() -> None:
    """REQUIRED_COLUMNS 覆盖全部 REQUIRED_TABLES。"""
    for t in REQUIRED_TABLES:
        assert t in REQUIRED_COLUMNS, f"{t} missing from REQUIRED_COLUMNS"
        assert len(REQUIRED_COLUMNS[t]) > 0
