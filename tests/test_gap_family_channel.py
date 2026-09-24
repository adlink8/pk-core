# -*- coding: utf-8 -*-
"""缺口家族 feeder 通道与 union 发布的离线测试。

全部使用 tmp fixtures，不触碰任何真实库。覆盖：
* 家族过滤：normalized 多家族 → ``--families`` 只产指定家族，legacy 仅作
  合并配对进入产物（legacy-only 不漏入）；
* union 发布：预置 v2 轨会话的权威库 → 过滤发布后 v2 会话原样保留、目标
  家族被新增/更新、非目标家族不被触碰；
* 幂等：同输入跑两次，第二次零变化；
* 发布前备份生成且只保留一份；
* ``run_gap_family_channel`` 编排（子步骤打桩）与审计落账。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.conversation import (
    authority_ingest as ai,
)
from personal_knowledge.application.conversation import (
    build_canonical_agent_conversations as bcc,
)

GAP = bcc.GAP_CHANNEL_FAMILIES

# ---------------------------------------------------------------- fixture DDL

_NORM_SESSIONS_DDL = """
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
    file_hash TEXT,
    cwd TEXT,
    git_branch TEXT,
    evidence_eligible INTEGER NOT NULL DEFAULT 1,
    evidence_scope TEXT NOT NULL DEFAULT 'user'
)
"""
_NORM_MESSAGES_DDL = """
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_message_id TEXT,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    content_length INTEGER,
    timestamp TEXT,
    model TEXT,
    is_system INTEGER NOT NULL DEFAULT 0,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    evidence_scope TEXT NOT NULL DEFAULT 'user'
)
"""
_NORM_TOOLS_DDL = """
CREATE TABLE tool_events (
    tool_event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    tool_name TEXT,
    category TEXT,
    status TEXT,
    call_index INTEGER,
    subagent_session_id TEXT,
    content_length INTEGER,
    timestamp TEXT
)
"""
_LEGACY_META_DDL = """
CREATE TABLE agent_sessions_meta (
    session_id TEXT,
    source TEXT,
    family TEXT,
    raw_file TEXT,
    timestamp TEXT,
    cwd TEXT,
    model TEXT
)
"""
_LEGACY_SOURCE_FILES_DDL = """
CREATE TABLE source_files (
    sha256 TEXT,
    relative_path TEXT,
    copied_path TEXT
)
"""
_LEGACY_MESSAGES_DDL = """
CREATE TABLE agent_messages (
    session_id TEXT,
    event_index INTEGER,
    timestamp TEXT,
    role TEXT,
    text TEXT
)
"""

# 缺口家族源会话（chatgpt：2 条，其中 chat-1 带 file_hash 可与 legacy 配对）
CHAT1_SRC = "chatgpt:11111111-1111-1111-1111-111111111111"
CHAT2_SRC = "chatgpt:22222222-2222-2222-2222-222222222222"
GEM1_SRC = "gemini:33333333-3333-3333-3333-333333333333"
# 非缺口家族源会话
CODEX1_SRC = "codex:44444444-4444-4444-4444-444444444444"
GROK1_SRC = "grok:55555555-5555-5555-5555-555555555555"
# legacy：L1 与 chat-1 同文件（file_hash 配对），L2 为 legacy-only
LEGACY1_ID = "rollout-11111111-1111-1111-1111-111111111111.jsonl"
LEGACY2_ID = "rollout-99999999-9999-9999-9999-999999999999.jsonl"
HASH_A = "a" * 64
HASH_B = "b" * 64


def _make_normalized(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(_NORM_SESSIONS_DDL)
    con.execute(_NORM_MESSAGES_DDL)
    con.execute(_NORM_TOOLS_DDL)

    def add_session(sid, src, agent, started, count, file_hash=None):
        con.execute(
            "INSERT INTO sessions (session_id, source_session_id, agent,"
            " started_at, ended_at, message_count, user_message_count, file_hash,"
            " evidence_eligible, evidence_scope) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, src, agent, started, started, count, count, file_hash, 1, "user"))

    def add_messages(sid, n):
        for i in range(n):
            con.execute(
                "INSERT INTO messages (message_id, session_id, source_message_id,"
                " ordinal, role, content, content_length, timestamp, is_system,"
                " is_sidechain, content_hash, evidence_scope)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{sid}:m{i}", sid, f"{sid}:m{i}", i, "user", f"hi {i}", 4,
                 "2026-05-01T10:00:00", 0, 0, None, "user"))

    add_session("s-chat-1", CHAT1_SRC, "chatgpt", "2026-05-01T10:00:00", 2, HASH_A)
    add_messages("s-chat-1", 2)
    con.execute(
        "INSERT INTO tool_events (tool_event_id, session_id, source_kind,"
        " tool_name, category, status, call_index, content_length, timestamp)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("s-chat-1:t0", "s-chat-1", "tool_use", "Bash", "shell", "ok", 0, 12,
         "2026-05-01T10:05:00"))
    add_session("s-chat-2", CHAT2_SRC, "chatgpt", "2026-05-02T10:00:00", 1)
    add_messages("s-chat-2", 1)
    add_session("s-gem-1", GEM1_SRC, "gemini", "2026-05-03T10:00:00", 1)
    add_messages("s-gem-1", 1)
    add_session("s-codex-1", CODEX1_SRC, "codex", "2026-05-04T10:00:00", 1)
    add_messages("s-codex-1", 1)
    add_session("s-grok-1", GROK1_SRC, "grok", "2026-05-05T10:00:00", 1)
    add_messages("s-grok-1", 1)
    con.commit()
    con.close()


def _make_legacy(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(_LEGACY_META_DDL)
    con.execute(_LEGACY_SOURCE_FILES_DDL)
    con.execute(_LEGACY_MESSAGES_DDL)
    con.execute(
        "INSERT INTO agent_sessions_meta (session_id, source, family, raw_file,"
        " timestamp) VALUES (?,?,?,?,?)",
        (LEGACY1_ID, "ChatGPT", "ChatGPT", "Agent/raw/ChatGPT/x.jsonl",
         "2026-05-01T10:00:00"))
    con.execute(
        "INSERT INTO agent_sessions_meta (session_id, source, family, raw_file,"
        " timestamp) VALUES (?,?,?,?,?)",
        (LEGACY2_ID, "Codex", "Codex", "Agent/raw/Codex/y.jsonl",
         "2026-04-01T00:00:00"))
    con.execute(
        "INSERT INTO source_files (sha256, relative_path, copied_path)"
        " VALUES (?,?,?)", (HASH_A, None, "raw/ChatGPT/x.jsonl"))
    con.execute(
        "INSERT INTO source_files (sha256, relative_path, copied_path)"
        " VALUES (?,?,?)", (HASH_B, None, "raw/Codex/y.jsonl"))
    for sid in (LEGACY1_ID, LEGACY2_ID):
        con.execute(
            "INSERT INTO agent_messages (session_id, event_index, timestamp,"
            " role, text) VALUES (?,?,?,?,?)",
            (sid, 0, "2026-05-01T10:00:00", "user", "legacy hello"))
    con.commit()
    con.close()


def _create_canonical_schema(con: sqlite3.Connection) -> None:
    for table, cols in bcc.CANONICAL_SCHEMA.items():
        col_def = ", ".join(f"{c} {t}" for c, t in cols)
        con.execute(f"CREATE TABLE {table} ({col_def})")
    for idx_name, table, cols in bcc.CANONICAL_INDEXES:
        con.execute(f"CREATE INDEX {idx_name} ON {table} ({cols})")


def _insert_canonical_session(con, csid, agent, started, message_count):
    con.execute(
        "INSERT INTO canonical_sessions (canonical_session_id, primary_source,"
        " agent, started_at, ended_at, message_count, evidence_eligible,"
        " evidence_scope, merged, lifecycle) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (csid, "agentsview", agent, started, started, message_count, 1, "user",
         0, "active"))


def _insert_canonical_message(con, csid, ordinal, content):
    con.execute(
        "INSERT INTO canonical_messages (canonical_message_id,"
        " canonical_session_id, source, source_message_ref, ordinal, role,"
        " content, content_length, evidence_scope)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{csid}:cm{ordinal}", csid, "agentsview", f"ref{ordinal}", ordinal,
         "user", content, len(content), "user"))


def _insert_canonical_tool(con, csid, idx):
    con.execute(
        "INSERT INTO canonical_tool_events (canonical_tool_id,"
        " canonical_session_id, source, source_kind, tool_name)"
        " VALUES (?,?,?,?,?)",
        (f"{csid}:ct{idx}", csid, "agentsview", "tool_use", "Bash"))


def _make_dest_with_v2(path: Path) -> dict[str, str]:
    """预置权威库：3 条 v2 轨会话 + 1 条 v1 轨残留 + 1 条将被更新的 cs 会话。"""
    con = sqlite3.connect(str(path))
    _create_canonical_schema(con)
    ids = {}
    for name, agent in (("v2a", "zcode"), ("v2b", "codex"), ("v2c", "grok")):
        csid = f"v2|cs|{name}_deadbeefdeadbeefdeadbeefdeadbeef"
        ids[name] = csid
        _insert_canonical_session(con, csid, agent, "2026-09-10T10:00:00", 2)
        for i in range(2):
            _insert_canonical_message(con, csid, i, f"v2 {name} msg {i}")
        _insert_canonical_tool(con, csid, 0)
    # v2 轨的一条 source link 与一条 relation（都应原样保留）
    con.execute(
        "INSERT INTO session_source_links (link_id, canonical_session_id, source,"
        " source_session_id, match_method, match_confidence)"
        " VALUES (?,?,?,?,?,?)",
        ("v2a-link", ids["v2a"], "agentsview", "zcode:v2a", "single_source",
         "strong"))
    con.execute(
        "INSERT INTO session_relations (relation_id, parent_canonical_id,"
        " child_canonical_id, relationship_type) VALUES (?,?,?,?)",
        ("v2-rel", ids["v2a"], ids["v2b"], "parent"))
    # v1 轨残留：投影里没有的 cs id（旧 codex 会话已不在 normalized），必须保留
    ids["stale"] = bcc._norm_id("cs", "agentsview", "codex:stale-dead-beef")
    _insert_canonical_session(con, ids["stale"], "codex", "2026-06-01T10:00:00", 1)
    _insert_canonical_message(con, ids["stale"], 0, "stale session")
    # 将被投影更新的 cs 会话（= chat-1 的 canonical id），旧内容必须被覆写
    ids["update"] = bcc._norm_id("cs", "agentsview", CHAT1_SRC)
    _insert_canonical_session(con, ids["update"], "chatgpt", "2026-05-01T10:00:00", 99)
    _insert_canonical_message(con, ids["update"], 0, "OLD CONTENT")
    _insert_canonical_tool(con, ids["update"], 0)
    con.execute(
        "INSERT INTO session_source_links (link_id, canonical_session_id, source,"
        " source_session_id, match_method, match_confidence)"
        " VALUES (?,?,?,?,?,?)",
        ("old-chat1-link", ids["update"], "agentsview", CHAT1_SRC,
         "single_source", "strong"))
    con.commit()
    con.close()
    return ids


_CANONICAL_TABLES = tuple(bcc.CANONICAL_SCHEMA)


def _dump(db: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {
            table: sorted(con.execute(f"SELECT * FROM {table}").fetchall())
            for table in _CANONICAL_TABLES
        }
    finally:
        con.close()


def _count_by_agent(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute(
            "SELECT agent, COUNT(*) FROM canonical_sessions GROUP BY agent"
        ).fetchall())
    finally:
        con.close()


def _rows_for(db: Path, table: str, csid: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return sorted(con.execute(
            f"SELECT * FROM {table} WHERE canonical_session_id=?", (csid,)
        ).fetchall())
    finally:
        con.close()


def _before_rows(rows: list[tuple], table: str, csid: str) -> list[tuple]:
    # canonical_sessions 的 canonical_session_id 是第 0 列，其余表是第 1 列
    idx = 0 if table == "canonical_sessions" else 1
    return [r for r in rows if r[idx] == csid]


@pytest.fixture()
def norm_db(tmp_path) -> Path:
    path = tmp_path / "agentsview_normalized.sqlite"
    _make_normalized(path)
    return path


@pytest.fixture()
def legacy_db(tmp_path) -> Path:
    path = tmp_path / "agent_data.sqlite"
    _make_legacy(path)
    return path


# ------------------------------------------------------------------ 家族过滤


def test_load_agentsview_sessions_family_filter(norm_db):
    all_sessions = bcc._load_agentsview_sessions(norm_db)
    assert len(all_sessions) == 5

    filtered = bcc._load_agentsview_sessions(norm_db, {"chatgpt", "gemini"})
    assert {s["agent"] for s in filtered} == {"chatgpt", "gemini"}
    assert len(filtered) == 3

    # 大小写不敏感；空集合 = 不过滤（与默认行为一致）
    assert len(bcc._load_agentsview_sessions(norm_db, {"CHATGPT"})) == 2
    assert len(bcc._load_agentsview_sessions(norm_db, set())) == 5
    assert len(bcc._load_agentsview_sessions(norm_db, None)) == 5


def test_family_filter_publishes_only_target_families(tmp_path, norm_db, legacy_db):
    dest = tmp_path / "agent_conversations.sqlite"
    rc = bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest,
                 families={"chatgpt", "gemini"})
    assert rc == 0
    assert _count_by_agent(dest) == {"chatgpt": 2, "gemini": 1}

    # chat-1 与 legacy L1 按 file_hash 合并：legacy 以合并配对身份进入产物
    con = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    row = con.execute(
        "SELECT primary_source, merged FROM canonical_sessions"
        " WHERE canonical_session_id=?",
        (bcc._norm_id("cs", "agentsview", CHAT1_SRC),),
    ).fetchone()
    links = con.execute(
        "SELECT source, match_method FROM session_source_links"
        " WHERE canonical_session_id=?",
        (bcc._norm_id("cs", "agentsview", CHAT1_SRC),),
    ).fetchall()
    con.close()
    assert row == ("agentsview", 1)
    assert sorted(links) == [("agentsview", "file_hash"), ("legacy", "file_hash")]


def test_family_filter_drops_legacy_only_sessions(tmp_path, norm_db, legacy_db):
    """legacy-only 会话不是任何指定家族的合并配对，过滤激活时不得漏入。"""
    dest = tmp_path / "agent_conversations.sqlite"
    bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest,
            families={"chatgpt"})
    con = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    legacy_only = con.execute(
        "SELECT COUNT(*) FROM canonical_sessions WHERE primary_source='legacy'"
    ).fetchone()[0]
    con.close()
    assert legacy_only == 0


def test_default_run_without_filter_keeps_all_families_and_legacy_only(
        tmp_path, norm_db, legacy_db):
    """不传 families：行为与改造前一致——全部家族 + legacy-only 会话都在。"""
    dest = tmp_path / "agent_conversations.sqlite"
    rc = bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest)
    assert rc == 0
    # legacy-only 会话的 agent 标签取 legacy source 原值 'Codex'（大小写不归一）
    assert _count_by_agent(dest) == {
        "chatgpt": 2, "gemini": 1, "codex": 1, "Codex": 1, "grok": 1}
    con = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    legacy_only = con.execute(
        "SELECT COUNT(*) FROM canonical_sessions WHERE primary_source='legacy'"
    ).fetchone()[0]
    con.close()
    assert legacy_only == 1  # LEGACY2_ID（codex，无 AgentsView 配对）


def test_canonical_cli_families_flag_dry_run(tmp_path, norm_db, legacy_db, capsys):
    dest = tmp_path / "agent_conversations.sqlite"
    rc = bcc.main(["--dry-run", "--families", "chatgpt", "--av-db", str(norm_db),
                   "--legacy-db", str(legacy_db), "--dest-db", str(dest)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Families:" in out and "chatgpt" in out
    assert not dest.exists()  # dry-run 不写任何文件


# ------------------------------------------------------------------ union 发布


def test_union_publish_preserves_v2_track_and_updates_target(
        tmp_path, norm_db, legacy_db):
    dest = tmp_path / "agent_conversations.sqlite"
    ids = _make_dest_with_v2(dest)
    before = _dump(dest)

    rc = bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest,
                 families={"chatgpt", "gemini"})
    assert rc == 0
    after = _dump(dest)

    # v2 轨 3 条会话的 sessions/messages/tools/links 一行不变
    for name in ("v2a", "v2b", "v2c"):
        csid = ids[name]
        for table in ("canonical_sessions", "canonical_messages",
                      "canonical_tool_events", "session_source_links"):
            assert _rows_for(dest, table, csid) == _before_rows(
                before[table], table, csid), (name, table)
    assert ("v2-rel", ids["v2a"], ids["v2b"], "parent") in after["session_relations"]
    assert ("v2a-link", ids["v2a"], "agentsview", "zcode:v2a", None,
            "single_source", "strong") in after["session_source_links"]

    # v1 轨残留（投影没有的 cs id）原样保留
    stale_rows = _rows_for(dest, "canonical_sessions", ids["stale"])
    assert len(stale_rows) == 1
    assert _rows_for(dest, "canonical_messages", ids["stale"]) == _before_rows(
        before["canonical_messages"], "canonical_messages", ids["stale"])

    # 目标家族被新增/更新：chatgpt 2 + gemini 1，且 chat-1 旧内容被覆写
    # （codex 2 = v2 轨 v2b + v1 轨残留 stale，均非本次投影新增）
    assert _count_by_agent(dest) == {
        "chatgpt": 2, "gemini": 1, "zcode": 1, "codex": 2, "grok": 1}
    updated = _rows_for(dest, "canonical_sessions", ids["update"])
    assert len(updated) == 1 and updated[0][5] == 2  # message_count 99 → 2
    update_msgs = _rows_for(dest, "canonical_messages", ids["update"])
    assert len(update_msgs) == 2
    assert all("OLD CONTENT" != r[5] for r in update_msgs)
    # 旧 tool 事件（不同 id）随投影重建，不残留
    assert len(_rows_for(dest, "canonical_tool_events", ids["update"])) == 1

    # 非目标家族（codex/grok 的 AgentsView 会话）没有被新增
    # （预置的 codex/grok 会话：v1 轨残留 stale + v2 轨 v2b/v2c，均须排除）
    con = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    new_codex = con.execute(
        "SELECT COUNT(*) FROM canonical_sessions WHERE agent IN ('codex','grok')"
        " AND canonical_session_id NOT IN (?,?,?)",
        (ids["stale"], ids["v2b"], ids["v2c"]),
    ).fetchone()[0]
    con.close()
    assert new_codex == 0


def test_union_publish_is_idempotent(tmp_path, norm_db, legacy_db, capsys):
    dest = tmp_path / "agent_conversations.sqlite"
    _make_dest_with_v2(dest)
    families = {"chatgpt", "gemini"}

    assert bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db,
                   dest_db=dest, families=families) == 0
    first = _dump(dest)

    capsys.readouterr()
    assert bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db,
                   dest_db=dest, families=families) == 0
    second = _dump(dest)

    assert first == second  # 全表零变化
    out = capsys.readouterr().out
    assert re.search(r"Added sessions:\s+0", out)
    assert re.search(r"Preserved sessions:\s+4", out)  # 3 v2 + 1 stale
    assert re.search(r"Updated sessions:\s+3", out)  # chat-1/chat-2/gem-1


def test_publish_creates_single_rolling_backup(tmp_path, norm_db, legacy_db):
    dest = tmp_path / "agent_conversations.sqlite"
    _make_dest_with_v2(dest)
    before = _dump(dest)

    bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest,
            families={"chatgpt"})
    backup = tmp_path / "agent_conversations.backup.sqlite"
    assert backup.exists()
    backups = list(tmp_path.glob("*.backup.sqlite"))
    assert backups == [backup]  # 单份滚动覆盖
    assert _dump(backup) == before  # 备份的是发布前的旧库
    assert not (tmp_path / "agent_conversations.staging.sqlite").exists()

    # 再跑一次：备份滚动为上一次的发布结果，仍然只有一份
    after_first = _dump(dest)
    bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest,
            families={"chatgpt"})
    assert list(tmp_path.glob("*.backup.sqlite")) == [backup]
    assert _dump(backup) == after_first


def test_union_publish_to_fresh_dest_needs_no_backup(tmp_path, norm_db, legacy_db):
    dest = tmp_path / "agent_conversations.sqlite"
    bcc.run(False, True, av_db=norm_db, legacy_db=legacy_db, dest_db=dest,
            families={"chatgpt"})
    assert dest.exists()
    assert list(tmp_path.glob("*.backup.sqlite")) == []


# ------------------------------------------------------- id 消歧（既有数据缺陷）


def test_union_disambiguates_colliding_ids_without_losing_rows(
        tmp_path, norm_db, legacy_db):
    """整数型 source id 跨家族相撞：既有行与投影行不得互相顶掉。

    fixture：chatgpt 会话消息的 source_message_id 与既有 grok 会话某条消息的
    source id 相同（冻结轨实测形态，grok/zcode/qoder/gemini 均如此）。
    """
    # 专用 normalized：chatgpt 会话，唯一消息的 source id 是裸整数 86412
    norm = tmp_path / "collide_norm.sqlite"
    con = sqlite3.connect(str(norm))
    con.execute(_NORM_SESSIONS_DDL)
    con.execute(_NORM_MESSAGES_DDL)
    con.execute(
        "INSERT INTO sessions (session_id, source_session_id, agent, started_at,"
        " ended_at, message_count, evidence_eligible, evidence_scope)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s-collide", "chatgpt:86412abcd", "chatgpt", "2026-05-09T10:00:00",
         "2026-05-09T11:00:00", 1, 1, "user"))
    con.execute(
        "INSERT INTO messages (message_id, session_id, source_message_id,"
        " ordinal, role, content, content_length, evidence_scope)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s-collide:m0", "s-collide", "86412", 0, "user", "new chatgpt message",
         19, "user"))
    con.commit()
    con.close()

    dest = tmp_path / "agent_conversations.sqlite"
    con = sqlite3.connect(str(dest))
    _create_canonical_schema(con)
    frozen = "cs|frozen_grok_deadbeefdeadbeefdeadbeef"
    _insert_canonical_session(con, frozen, "grok", "2026-06-01T10:00:00", 1)
    # 既有行：id 与 chatgpt 投影消息的自然 id 相同（source id 均为 "86412"）
    colliding_id = bcc._norm_id("cm", "av", "86412")
    con.execute(
        "INSERT INTO canonical_messages (canonical_message_id,"
        " canonical_session_id, source, source_message_ref, ordinal, role,"
        " content, content_length, evidence_scope) VALUES (?,?,?,?,?,?,?,?,?)",
        (colliding_id, frozen, "agentsview", "86412", 0, "user",
         "frozen grok message", 19, "user"))
    con.commit()
    con.close()

    rc = bcc.run(False, True, av_db=norm, legacy_db=legacy_db, dest_db=dest,
                 families={"chatgpt"})
    assert rc == 0

    # 既有行原样保留（id 与内容都没被投影顶掉）
    con = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    frozen_rows = con.execute(
        "SELECT canonical_message_id, canonical_session_id, content"
        " FROM canonical_messages WHERE canonical_session_id=?",
        (frozen,)).fetchall()
    # chatgpt 投影消息以命名空间变体 id 落地，一条不丢
    chat_csid = bcc._norm_id("cs", "agentsview", "chatgpt:86412abcd")
    chat_rows = con.execute(
        "SELECT canonical_message_id, content FROM canonical_messages"
        " WHERE canonical_session_id=? ORDER BY ordinal", (chat_csid,)).fetchall()
    con.close()

    assert frozen_rows == [(colliding_id, frozen, "frozen grok message")]
    assert len(chat_rows) == 1
    assert chat_rows[0][0] == bcc._norm_id(
        "cm", "av+session", "chatgpt:86412abcd", "86412")
    assert chat_rows[0][1] == "new chatgpt message"


# ------------------------------------------------------- gap channel 编排


def _stub_steps(monkeypatch, fail_label: str | None = None):
    calls: list[tuple[str, str, list[str]]] = []

    def fake_run_step(label, module, extra):
        calls.append((label, module, extra))
        ok = label != fail_label
        return {"label": label, "module": module, "exit": 0 if ok else 1,
                "elapsed_s": 0.1, "status": "ok" if ok else "failed"}

    monkeypatch.setattr(ai, "_run_step", fake_run_step)
    monkeypatch.setattr(ai, "_count_eligible_by_agents", lambda db, ag: 7)
    monkeypatch.setattr(ai, "_count_published_by_agents", lambda ag: 9)
    return calls


@pytest.fixture()
def audit_paths(tmp_path, monkeypatch):
    audit = tmp_path / "ingest_audit.sqlite"
    human = tmp_path / "authority-ingest.log"
    monkeypatch.setattr(ai, "AUDIT_DB", audit)
    monkeypatch.setattr(ai, "HUMAN_LOG", human)
    return audit, human


def test_gap_channel_publishes_with_family_filter(monkeypatch, audit_paths):
    calls = _stub_steps(monkeypatch)
    report = ai.run_gap_family_channel(write=True)

    assert report.status == "gap_channel_ok"
    assert [c[0] for c in calls] == [
        "agentsview inventory", "normalized build",
        "canonical publish (gap families)"]
    label, module, extra = calls[2]
    assert module.endswith("build_canonical_agent_conversations")
    assert extra == ["--families", ",".join(GAP), "--write"]
    assert calls[1][2] == ["--write"]  # normalized 正常构建
    assert report.eligible_sessions == 7
    assert report.published_sessions == 9

    audit, _ = audit_paths
    con = sqlite3.connect(str(audit))
    row = con.execute(
        "SELECT status, eligible_sessions, published_sessions FROM ingest_runs"
        " WHERE run_id=?", (report.run_id,)).fetchone()
    con.close()
    assert row == ("gap_channel_ok", 7, 9)


def test_gap_channel_dry_run_does_not_publish(monkeypatch, audit_paths):
    calls = _stub_steps(monkeypatch)
    report = ai.run_gap_family_channel(write=False, dry_run=True)

    assert report.status == "gap_channel_dry_run"
    assert calls[1][2] == ["--dry-run"]
    assert calls[2][2] == ["--families", ",".join(GAP)]  # 无 --write
    assert report.published_sessions == 0


def test_gap_channel_custom_families(monkeypatch, audit_paths):
    calls = _stub_steps(monkeypatch)
    report = ai.run_gap_family_channel(
        write=True, families=("chatgpt", "gemini"))
    assert report.status == "gap_channel_ok"
    assert calls[2][2] == ["--families", "chatgpt,gemini", "--write"]


def test_gap_channel_failure_is_audited(monkeypatch, audit_paths):
    _stub_steps(monkeypatch, fail_label="normalized build")
    report = ai.run_gap_family_channel(write=True)
    assert report.status == "failed"
    assert "normalized build failed" in report.error

    audit, _ = audit_paths
    con = sqlite3.connect(str(audit))
    status = con.execute(
        "SELECT status FROM ingest_runs WHERE run_id=?",
        (report.run_id,)).fetchone()[0]
    con.close()
    assert status == "failed"


def test_main_channel_gap_families_defaults_to_dry_run(monkeypatch):
    seen: dict = {}

    def fake(*, write=False, dry_run=False, families=GAP):
        seen.update(write=write, dry_run=dry_run, families=families)
        return ai.IngestReport(run_id="r", status="gap_channel_dry_run")

    monkeypatch.setattr(ai, "run_gap_family_channel", fake)
    rc = ai.main(["--channel-gap-families"])
    assert rc == 0
    assert seen == {"write": False, "dry_run": True, "families": GAP}


def test_main_channel_gap_families_custom_list_and_write(monkeypatch):
    seen: dict = {}

    def fake(*, write=False, dry_run=False, families=GAP):
        seen.update(write=write, dry_run=dry_run, families=families)
        return ai.IngestReport(run_id="r", status="gap_channel_ok")

    monkeypatch.setattr(ai, "run_gap_family_channel", fake)
    rc = ai.main(["--channel-gap-families", "chatgpt, gemini", "--write"])
    assert rc == 0
    assert seen["write"] is True and seen["dry_run"] is False
    assert seen["families"] == ("chatgpt", "gemini")


def test_main_without_channel_flag_still_runs_default_ingest(monkeypatch):
    """默认入口行为不变：不带 --channel-gap-families 时仍走 run_ingest。"""
    seen: dict = {}

    def fake(*, write=False, dry_run=False):
        seen.update(write=write, dry_run=dry_run)
        return ai.IngestReport(run_id="r", status="ok")

    monkeypatch.setattr(ai, "run_ingest", fake)
    rc = ai.main([])
    assert rc == 0
    assert seen == {"write": False, "dry_run": True}
