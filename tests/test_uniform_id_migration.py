"""Tests for the uniform origin-derived id migration.

Builds a small synthetic authority DB (exact live DDL) covering: adapter-only
sessions, snapshot-only sessions, merged sessions with partial coverage, tool
call_index collisions, missing native session ids, relation re-pointing, and
the verification gates.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PKG_ROOT / "src"))

from personal_knowledge.application.conversation import uniform_id_migration as u  # noqa: E402

DDL = [
    """CREATE TABLE canonical_sessions (canonical_session_id TEXT PRIMARY KEY,
    primary_source TEXT NOT NULL CHECK(primary_source IN ('agentsview','legacy')),
    agent TEXT, started_at TEXT, ended_at TEXT, message_count INTEGER,
    user_message_count INTEGER, file_hash TEXT, parent_canonical_id TEXT,
    relationship_type TEXT, cwd TEXT, git_branch TEXT, model TEXT,
    evidence_eligible INTEGER NOT NULL DEFAULT 1,
    evidence_scope TEXT NOT NULL DEFAULT 'user', merged INTEGER NOT NULL DEFAULT 0,
    lifecycle TEXT NOT NULL DEFAULT 'active', superseded_by_canonical_id TEXT)""",
    """CREATE TABLE canonical_messages (canonical_message_id TEXT PRIMARY KEY,
    canonical_session_id TEXT NOT NULL REFERENCES canonical_sessions(canonical_session_id),
    source TEXT NOT NULL CHECK(source IN ('agentsview','legacy')),
    source_message_ref TEXT, ordinal INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','developer','system','tool')),
    content TEXT, content_length INTEGER, timestamp TEXT, model TEXT,
    is_system INTEGER NOT NULL DEFAULT 0, is_sidechain INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT, evidence_scope TEXT NOT NULL DEFAULT 'user')""",
    """CREATE TABLE canonical_tool_events (canonical_tool_id TEXT PRIMARY KEY,
    canonical_session_id TEXT NOT NULL REFERENCES canonical_sessions(canonical_session_id),
    source TEXT NOT NULL CHECK(source IN ('agentsview','legacy')),
    source_kind TEXT NOT NULL, tool_name TEXT, category TEXT, status TEXT,
    call_index INTEGER, subagent_session_id TEXT, content_length INTEGER,
    timestamp TEXT)""",
    """CREATE TABLE session_source_links (link_id TEXT PRIMARY KEY,
    canonical_session_id TEXT NOT NULL REFERENCES canonical_sessions(canonical_session_id),
    source TEXT NOT NULL CHECK(source IN ('agentsview','legacy')),
    source_session_id TEXT NOT NULL, source_raw_file TEXT,
    match_method TEXT NOT NULL CHECK(match_method IN
    ('file_hash','source_mapping','review_required','single_source')),
    match_confidence TEXT NOT NULL DEFAULT 'strong')""",
    """CREATE TABLE session_relations (relation_id TEXT PRIMARY KEY,
    parent_canonical_id TEXT NOT NULL, child_canonical_id TEXT NOT NULL,
    relationship_type TEXT)""",
    """CREATE TABLE ce_sessions (session_id TEXT PRIMARY KEY, family TEXT,
    native_session_id TEXT, generation_id TEXT, started_at TEXT, ended_at TEXT,
    cwd TEXT, git_branch TEXT, model TEXT)""",
    """CREATE TABLE ce_events (event_id TEXT PRIMARY KEY, session_id TEXT, kind TEXT,
    native_event_id TEXT, native_locator TEXT, ordinal INTEGER, occurred_at TEXT)""",
    """CREATE TABLE ce_generation_authority (generation_id TEXT PRIMARY KEY,
    active INTEGER, updated_at TEXT)""",
]

SESSION_COLS = ",".join(u.SESSION_COLUMNS)
MESSAGE_COLS = ",".join(u.MESSAGE_COLUMNS)
TOOL_COLS = ",".join(u.TOOL_COLUMNS)


def _insert_session(con, csid, source, agent, started, ended="2026-01-01T02:00:00Z",
                    eligible=1):
    con.execute(
        f"INSERT INTO canonical_sessions ({SESSION_COLS}) VALUES (?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?)",
        (csid, source, agent, started, ended, 0, 0, None, None, None, None, None,
         None, eligible, "user", 0, "active", None))


def _insert_message(con, mid, csid, source, ref, ordinal, role, content, ts,
                    chash):
    con.execute(
        f"INSERT INTO canonical_messages ({MESSAGE_COLS}) VALUES ("
        + ",".join("?" * len(u.MESSAGE_COLUMNS)) + ")",
        (mid, csid, source, ref, ordinal, role, content, len(content or ""), ts,
         None, 0, 0, chash, "user"))


def _insert_tool(con, tid, csid, source, kind, name, call_index, ts, length=3):
    con.execute(
        f"INSERT INTO canonical_tool_events ({TOOL_COLS}) VALUES ("
        + ",".join("?" * len(u.TOOL_COLUMNS)) + ")",
        (tid, csid, source, kind, name, None, "ok", call_index, None, length, ts))


def _insert_ce(con, ce_sid, family, native, gen):
    con.execute(
        "INSERT INTO ce_sessions (session_id, family, native_session_id,"
        " generation_id, started_at, ended_at, cwd, git_branch, model)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (ce_sid, family, native, gen, "2026-01-01T01:00:00Z",
         "2026-01-01T02:00:00Z", None, None, None))


def _insert_event(con, eid, ce_sid, kind, native_id, locator, ts):
    con.execute(
        "INSERT INTO ce_events (event_id, session_id, kind, native_event_id,"
        " native_locator, ordinal, occurred_at) VALUES (?,?,?,?,?,?,?)",
        (eid, ce_sid, kind, native_id, locator, 0, ts))


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "authority.sqlite"
    con = sqlite3.connect(path)
    for stmt in DDL:
        con.execute(stmt)
    con.execute("INSERT INTO ce_generation_authority VALUES ('gen-old',0,"
                "'2026-01-01T00:00:00Z')")
    con.execute("INSERT INTO ce_generation_authority VALUES ('gen-new',1,"
                "'2026-01-02T00:00:00Z')")

    # --- adapter-only session (native message ids present) -----------------
    _insert_ce(con, "ceA", "claude", "S-native-1", "gen-new")
    _insert_event(con, "ev1", "ceA", "user_message", "uuid-u1",
                  "agent-x.jsonl#L1", "2026-01-01T01:00:00Z")
    _insert_event(con, "ev2", "ceA", "assistant_message", "uuid-a1",
                  "agent-x.jsonl#L2", "2026-01-01T01:01:00Z")
    cs_a = u.v2_session_hash("ceA")
    _insert_session(con, cs_a, "legacy", "claude", "2026-01-01T01:00:00Z")
    _insert_message(con, u.v2_message_hash("ev1"), cs_a, "legacy",
                    "agent-x.jsonl#L1", 1, "user", "hello", "2026-01-01T01:00:00Z",
                    "h1")
    _insert_message(con, u.v2_message_hash("ev2"), cs_a, "legacy",
                    "agent-x.jsonl#L2", 2, "assistant", "hi there",
                    "2026-01-01T01:01:00Z", "h2")

    # --- same address captured twice (older generation, other content) -----
    _insert_ce(con, "ceA-old", "claude", "S-native-1", "gen-old")
    _insert_event(con, "ev1b", "ceA-old", "user_message", "uuid-u1",
                  "agent-x.jsonl#L1", "2026-01-01T01:00:00Z")
    cs_a_old = u.v2_session_hash("ceA-old")
    _insert_session(con, cs_a_old, "legacy", "claude", "2026-01-01T01:00:00Z")
    _insert_message(con, u.v2_message_hash("ev1b"), cs_a_old, "legacy",
                    "agent-x.jsonl#L1", 1, "user", "hello (stale capture)",
                    "2026-01-01T01:00:00Z", "h1-stale")

    # --- snapshot-only session (chatgpt: no native root at all) -------------
    cs_b = "cs|bbbb2222"
    _insert_session(con, cs_b, "agentsview", "chatgpt", "2026-01-01T01:00:00Z")
    _insert_message(con, "cm|old-b0", cs_b, "agentsview", "7001", 0, "user",
                    "q1", "2026-01-01T01:00:00Z", "hq1")
    _insert_message(con, "cm|old-b1", cs_b, "agentsview", "7002", 1, "assistant",
                    "a1", "2026-01-01T01:02:00Z", "ha1")
    con.execute(
        "INSERT INTO session_source_links (link_id, canonical_session_id, source,"
        " source_session_id, source_raw_file, match_method) VALUES (?,?,?,?,?,?)",
        ("link|b", cs_b, "agentsview", "chatgpt:CG-1", None, "source_mapping"))

    # --- merged session: adapter has 2 of 3 snapshot messages ---------------
    _insert_ce(con, "ceC", "codex", "C-native-1", "gen-new")
    _insert_event(con, "ev5", "ceC", "user_message", None,
                  "rollout-c.jsonl#L3", "2026-01-01T01:00:00Z")
    _insert_event(con, "ev6", "ceC", "assistant_message", "native-x9",
                  "rollout-c.jsonl#L4", "2026-01-01T01:01:00Z")
    cs_c_v2 = u.v2_session_hash("ceC")
    _insert_session(con, cs_c_v2, "legacy", "codex", "2026-01-01T01:00:00Z")
    _insert_message(con, u.v2_message_hash("ev5"), cs_c_v2, "legacy",
                    "rollout-c.jsonl#L3", 1, "user", "shared user turn",
                    "2026-01-01T01:00:00Z", "hSharedU")
    _insert_message(con, u.v2_message_hash("ev6"), cs_c_v2, "legacy",
                    "rollout-c.jsonl#L4", 2, "assistant", "shared assistant turn",
                    "2026-01-01T01:01:00Z", "hSharedA")
    cs_c_v1 = "cs|cccc3333"
    _insert_session(con, cs_c_v1, "agentsview", "codex", "2026-01-01T01:00:00Z")
    _insert_message(con, "cm|old-c0", cs_c_v1, "agentsview", "9101", 0, "user",
                    "shared user turn", "2026-01-01T01:00:00Z", "hSharedU")
    _insert_message(con, "cm|old-c1", cs_c_v1, "agentsview", "9102", 1,
                    "assistant", "shared assistant turn", "2026-01-01T01:01:00Z",
                    "hSharedA")
    _insert_message(con, "cm|old-c2", cs_c_v1, "agentsview", "9103", 2, "user",
                    "snapshot-only turn", "2026-01-01T01:05:00Z", "hOnly")
    con.execute(
        "INSERT INTO session_source_links (link_id, canonical_session_id, source,"
        " source_session_id, source_raw_file, match_method) VALUES (?,?,?,?,?,?)",
        ("link|c", cs_c_v1, "agentsview", "codex:C-native-1", None,
         "source_mapping"))

    # --- snapshot-only zcode session with duplicate tool call_index ---------
    cs_d = "cs|dddd4444"
    _insert_session(con, cs_d, "agentsview", "zcode", "2026-01-01T01:00:00Z")
    _insert_tool(con, "cte|old-d0", cs_d, "agentsview", "call", "Bash", 3,
                 "2026-01-01T01:00:00Z")
    _insert_tool(con, "cte|old-d1", cs_d, "agentsview", "result", None, 3,
                 "2026-01-01T01:00:00Z")
    con.execute(
        "INSERT INTO session_source_links (link_id, canonical_session_id, source,"
        " source_session_id, source_raw_file, match_method) VALUES (?,?,?,?,?,?)",
        ("link|d", cs_d, "agentsview", "zcode:Z-native-1", None, "source_mapping"))

    # --- adapter session whose native session id is missing (grok) ----------
    _insert_ce(con, "ceE", "grok", None, "gen-new")
    _insert_event(con, "ev7", "ceE", "user_message", "row-7",
                  "chat_history.jsonl#7", "2026-01-01T01:00:00Z")
    cs_e = u.v2_session_hash("ceE")
    _insert_session(con, cs_e, "legacy", "grok", "2026-01-01T01:00:00Z")
    _insert_message(con, u.v2_message_hash("ev7"), cs_e, "legacy",
                    "chat_history.jsonl#7", 1, "user", "grok hi",
                    "2026-01-01T01:00:00Z", "hg")

    # --- relation between two snapshot sessions ------------------------------
    con.execute(
        "INSERT INTO session_relations VALUES (?,?,?,?)",
        ("rel|1", cs_b, cs_d, "subagent"))

    con.commit()
    con.close()
    return path


def _plan(db_path):
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    plan = u.plan_migration(con)
    u._assign_ordinals(plan)
    problems = u.verify_plan(con, plan)
    con.close()
    return plan, problems


def test_id_formats():
    assert u.make_session_id("claude", "S1") == "cs|claude|S1"
    assert u.make_message_id("claude", "S1", "u1") == "cm|claude|S1|u1"
    assert u.make_tool_id("codex", "S1", "L3") == "ct|codex|S1|L3"
    # a part containing the separator must not forge extra id fields
    assert u.make_message_id("a|b", "c", "d") == "cm|a/b|c|d"


def test_v2_hash_reproduction():
    assert u.v2_session_hash("ceA") == u._norm_hash("v2|cs", "ceA")
    assert u.v2_message_hash("ev1") == u._norm_hash("v2|cm", "ev1")


def test_plan_and_verify(db):
    plan, problems = _plan(db)
    assert problems == []
    ids = {r["canonical_session_id"] for r in plan.sessions}
    # adapter-only, snapshot-only, merged (one row), zcode snapshot-only, grok
    assert ids == {
        "cs|claude|S-native-1",
        "cs|chatgpt|CG-1",
        "cs|codex|C-native-1",
        "cs|zcode|Z-native-1",
        "cs|grok|ce:ceE",
    }


def test_adapter_rows_keep_native_addresses(db):
    plan, _ = _plan(db)
    msgs = {r["canonical_message_id"]: r for r in plan.messages}
    # native-id-first family (claude): the reliable client uuid is the
    # address, so a re-capture through a different mirror path keeps the id
    assert "cm|claude|S-native-1|uuid-u1" in msgs
    assert "cm|claude|S-native-1|uuid-a1" in msgs
    assert msgs["cm|claude|S-native-1|uuid-a1"]["content_length"] \
        == len("hi there")
    # locator-first family (codex): the native id is not address-safe (the
    # adapter records the literal 'agent_message'), so the locator wins
    assert "cm|codex|C-native-1|rollout-c.jsonl#L4" in msgs


def test_address_precedence():
    from collections import Counter
    stats = Counter()
    assert u._adapter_address("x", {}, {"x": ("native-9", "f.jsonl#L12")},
                              stats) == ("f.jsonl#L12", "native_locator")
    assert u._adapter_address("y", {}, {"y": ("native-9", None)},
                              stats) == ("native-9", "native_event_id")
    assert u._adapter_address("z", {"source_message_ref": "ref-1"}, {},
                              stats) == ("ref-1", "source_message_ref")
    assert u._adapter_address("w", {}, {}, stats) == ("w", "canonical_id_hash")
    # native-id-first families address messages by their native id
    assert u._adapter_address("x", {}, {"x": ("uuid-9", "f.jsonl#L12")},
                              stats, "claude") == ("uuid-9", "native_event_id")
    # ...but still fall back to the locator when the id is missing
    assert u._adapter_address("x", {}, {"x": (None, "f.jsonl#L12")},
                              stats, "claude") == ("f.jsonl#L12", "native_locator")
    # codex keeps locator-first even when a native id is present
    assert u._adapter_address("x", {}, {"x": ("agent_message", "r.jsonl#L7")},
                              stats, "codex") == ("r.jsonl#L7", "native_locator")


def test_adapter_address_is_family_aware():
    nid, loc = "uuid-u1", "mirror-a/agent-x.jsonl#L1"
    for family in ("claude", "qoder", "zcode", "mimo", "opencode", "pi",
                   "antigravity", "gemini", "copilot", "workbuddy", "kimi",
                   "kimi-work"):
        assert u.adapter_address(nid, loc, family) == nid, family
        assert u.adapter_address(None, loc, family) == loc, family
        assert u.adapter_address(nid, None, family) == nid, family
    # locator-first families and the legacy default signature
    for family in ("codex", "grok", "chatgpt", "cursor", "", None):
        assert u.adapter_address(nid, loc, family) == loc, family
        assert u.adapter_address(nid, None, family) == nid, family
    # both missing -> empty (the caller falls back to the event id)
    assert u.adapter_address(None, None, "claude") == ""


def test_same_address_two_captures_keeps_newest_only(db):
    plan, problems = _plan(db)
    assert problems == []
    rows = [r for r in plan.messages
            if r["canonical_session_id"] == "cs|claude|S-native-1"
            and r["canonical_message_id"].endswith("uuid-u1")]
    assert len(rows) == 1
    # the active generation's capture wins over the stale one
    assert rows[0]["content_hash"] == "h1"
    m = {old: new for t, old, new in plan.id_map if t == "canonical_messages"}
    stale = u.v2_message_hash("ev1b")
    assert m[stale] == "cm|claude|S-native-1|uuid-u1"


def test_merged_session_keeps_snapshot_only_content(db):
    plan, _ = _plan(db)
    msgs = [r for r in plan.messages
            if r["canonical_session_id"] == "cs|codex|C-native-1"]
    ids = {r["canonical_message_id"] for r in msgs}
    assert "cm|codex|C-native-1|rollout-c.jsonl#L3" in ids   # native locator
    assert "cm|codex|C-native-1|rollout-c.jsonl#L4" in ids   # locator beats id
    assert "cm|codex|C-native-1|av2" in ids                  # snapshot-only turn
    # the two covered snapshot rows map onto the surviving adapter rows
    m = {old: new for t, old, new in plan.id_map if t == "canonical_messages"}
    assert m["cm|old-c0"] == "cm|codex|C-native-1|rollout-c.jsonl#L3"
    assert m["cm|old-c1"] == "cm|codex|C-native-1|rollout-c.jsonl#L4"
    assert m["cm|old-c2"] == "cm|codex|C-native-1|av2"


def test_snapshot_only_session_uses_ordinals(db):
    plan, _ = _plan(db)
    ids = {r["canonical_message_id"] for r in plan.messages
           if r["canonical_session_id"] == "cs|chatgpt|CG-1"}
    assert ids == {"cm|chatgpt|CG-1|av0", "cm|chatgpt|CG-1|av1"}


def test_tool_call_index_collision_is_disambiguated(db):
    plan, _ = _plan(db)
    ids = {r["canonical_tool_id"] for r in plan.tools
           if r["canonical_session_id"] == "cs|zcode|Z-native-1"}
    assert ids == {"ct|zcode|Z-native-1|av3", "ct|zcode|Z-native-1|av3#2"}


def test_missing_native_session_id_falls_back(db):
    plan, _ = _plan(db)
    assert any(r["canonical_session_id"] == "cs|grok|ce:ceE"
               for r in plan.sessions)


def test_relations_are_repointed(db):
    plan, _ = _plan(db)
    assert plan.relations == [{
        "relation_id": u._norm_hash("rel", "cs|chatgpt|CG-1", "cs|zcode|Z-native-1"),
        "parent_canonical_id": "cs|chatgpt|CG-1",
        "child_canonical_id": "cs|zcode|Z-native-1",
        "relationship_type": "subagent",
    }]


def test_ordinals_are_dense_per_session(db):
    plan, _ = _plan(db)
    by_sid = {}
    for row in plan.messages:
        by_sid.setdefault(row["canonical_session_id"], []).append(row["ordinal"])
    for sid, ords in by_sid.items():
        assert sorted(ords) == list(range(1, len(ords) + 1)), sid


def test_verify_catches_content_loss(db):
    plan, problems = _plan(db)
    assert problems == []
    plan.messages = [r for r in plan.messages
                     if r["canonical_message_id"] != "cm|codex|C-native-1|av2"]
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    problems = u.verify_plan(con, plan)
    con.close()
    assert any("cs|codex|C-native-1" in p and "content lost" in p
               for p in problems)


def test_apply_roundtrip(tmp_path, db):
    work = tmp_path / "work.sqlite"
    work.write_bytes(db.read_bytes())
    plan, problems = _plan(db)
    assert problems == []
    con = sqlite3.connect(work)
    u.apply_plan(con, plan)
    con.close()

    con = sqlite3.connect(f"file:{work.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    assert con.execute("SELECT count(*) FROM canonical_sessions").fetchone()[0] \
        == len(plan.sessions)
    assert con.execute("SELECT count(*) FROM canonical_messages").fetchone()[0] \
        == len(plan.messages)
    assert con.execute("SELECT count(*) FROM canonical_tool_events").fetchone()[0] \
        == len(plan.tools)
    row = con.execute(
        "SELECT content, role, ordinal FROM canonical_messages WHERE"
        " canonical_message_id='cm|claude|S-native-1|uuid-a1'").fetchone()
    assert (row["content"], row["role"], row["ordinal"]) == ("hi there",
                                                             "assistant", 2)
    assert con.execute("SELECT count(*) FROM id_migration_map").fetchone()[0] \
        == len(plan.id_map)
    assert con.execute("SELECT count(*) FROM canonical_session_origins"
                       ).fetchone()[0] == len(plan.origins)
    assert con.execute("SELECT count(*) FROM session_relations").fetchone()[0] == 1
    con.close()


def test_idempotent_planning(db):
    plan1, _ = _plan(db)
    plan2, _ = _plan(db)
    assert [r["canonical_session_id"] for r in plan1.sessions] == \
        [r["canonical_session_id"] for r in plan2.sessions]
    assert plan1.id_map == plan2.id_map


def test_publish_guard_blocks_pre_migration_ids(tmp_path, db):
    """A migrated DB must refuse a publish that derives old-style ids."""
    from personal_knowledge.application.conversation import (
        build_canonical_agent_conversations as build,
    )
    work = tmp_path / "work.sqlite"
    work.write_bytes(db.read_bytes())
    plan, problems = _plan(db)
    assert problems == []
    con = sqlite3.connect(work)
    u.apply_plan(con, plan)
    con.close()

    # pre-migration style ids (cs|<hash>) must be refused
    with pytest.raises(RuntimeError, match="id_migration_map"):
        build._assert_publish_compatible(work, [
            {"canonical_session_id": "cs|ad0eeb10bebed2eb976dffab99ab83e5"},
        ])
    # 统一 id 不再触发 id 竖线数门；迁移库仍带白名单外的表（id_migration_map），
    # 故 2026-09 起的 schema 门照样拒绝（用例见下节）。
    with pytest.raises(RuntimeError, match="outside the published schema"):
        build._assert_publish_compatible(work, [
            {"canonical_session_id": "cs|claude|S-native-1"},
        ])
    # 白名单内的库（无迁移产物）从不因守卫受阻，与 id 形态无关
    clean = tmp_path / "clean.sqlite"
    clean.write_bytes(db.read_bytes())
    con = sqlite3.connect(clean)
    for table in ("ce_sessions", "ce_events", "ce_generation_authority"):
        con.execute(f"DROP TABLE {table}")
    con.commit()
    con.close()
    build._assert_publish_compatible(clean, [
        {"canonical_session_id": "cs|ad0eeb10bebed2eb976dffab99ab83e5"},
    ])


# ------------------------------------------------------- 发布守卫（fail-closed）

# 发布产物实测（2026-09-24，tmp 内跑完整发布路径）：staging 对象 = 下列 6 张表
# + 7 个显式索引 + 6 个 sqlite_autoindex_*；无 sqlite_sequence、无虚拟表影子表。
# 真实权威库另有 20 张白名单外的表（ce_* 17 张含 112 万行 ce_events、id_migration_map / id_address_policy / canonical_session_origins），整库替换会静默清空它们。
PUBLISHED_TABLES = [
    "canonical_messages", "canonical_sessions", "canonical_tool_events",
    "crosswalk_review", "session_relations", "session_source_links",
]

_AV_DDL = (
    "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
    " source_session_id TEXT NOT NULL, agent TEXT, started_at TEXT,"
    " ended_at TEXT, message_count INTEGER, user_message_count INTEGER,"
    " file_hash TEXT, parent_session_id TEXT, relationship_type TEXT, cwd TEXT,"
    " git_branch TEXT, evidence_eligible INTEGER NOT NULL DEFAULT 1, evidence_scope TEXT NOT NULL DEFAULT 'user')",
    "CREATE TABLE messages (message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
    " source_message_id TEXT, ordinal INTEGER NOT NULL, role TEXT NOT NULL,"
    " content TEXT, content_length INTEGER, timestamp TEXT, model TEXT,"
    " is_system INTEGER NOT NULL DEFAULT 0, is_sidechain INTEGER NOT NULL DEFAULT 0,"
    " content_hash TEXT, evidence_scope TEXT NOT NULL DEFAULT 'user')",
)


def _make_av_source(path: Path) -> Path:
    """最小可发布 normalized 源（1 会话 1 消息）。"""
    con = sqlite3.connect(str(path))
    for stmt in _AV_DDL:
        con.execute(stmt)
    con.execute(
        "INSERT INTO sessions (session_id, source_session_id, agent, started_at,"
        " ended_at, message_count, user_message_count, evidence_eligible,"
        " evidence_scope) VALUES (?,?,?,?,?,?,?,?,?)",
        ("s-1", "chatgpt:guard-probe", "chatgpt", "2026-05-01T10:00:00",
         "2026-05-01T10:05:00", 1, 1, 1, "user"))
    con.execute(
        "INSERT INTO messages (message_id, session_id, source_message_id,"
        " ordinal, role, content, content_length, evidence_scope)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s-1:m0", "s-1", "m0", 0, "user", "guard probe message", 18, "user"))
    con.commit()
    con.close()
    return path


def _tables(path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        return sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        con.close()


def _table_rows(path: Path, table: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        return sorted(con.execute(f"SELECT * FROM {table}").fetchall())
    finally:
        con.close()


def test_publish_guard_refuses_dest_with_foreign_tables(tmp_path, db):
    """权威库含发布 schema 之外的表 → 拒绝发布，且库一字未动。"""
    from personal_knowledge.application.conversation import (
        build_canonical_agent_conversations as build,
    )
    dest = tmp_path / "authority.sqlite"
    dest.write_bytes(db.read_bytes())
    av = _make_av_source(tmp_path / "av.sqlite")
    absent = tmp_path / "absent_legacy.sqlite"
    events_before = _table_rows(dest, "ce_events")
    sessions_before = _table_rows(dest, "canonical_sessions")
    tables_before = _tables(dest)

    with pytest.raises(RuntimeError, match="ce_events"):
        build.run(False, True, av_db=av, legacy_db=absent, dest_db=dest)
    # 同一入口的 CLI 形态（缺口家族通道 / run_pipeline 走这条）也必须中止
    with pytest.raises(RuntimeError, match="ce_events"):
        build.main(["--families", "chatgpt", "--write", "--av-db", str(av),
                    "--legacy-db", str(absent), "--dest-db", str(dest)])

    # 拒绝 = 权威库一字未动（白名单外的表还在，canonical 行也没被改写）
    assert _tables(dest) == tables_before
    assert _table_rows(dest, "ce_events") == events_before
    assert _table_rows(dest, "canonical_sessions") == sessions_before
    assert not (tmp_path / "authority.staging.sqlite").exists()
    assert not (tmp_path / "authority.backup.sqlite").exists()


def test_publish_guard_allows_fresh_and_published_dest(tmp_path):
    """首次发布（目标库不存在）与白名单内核（前次发布产物）照常放行。"""
    from personal_knowledge.application.conversation import (
        build_canonical_agent_conversations as build,
    )
    av = _make_av_source(tmp_path / "av.sqlite")
    absent = tmp_path / "absent_legacy.sqlite"
    dest = tmp_path / "authority.sqlite"

    assert not dest.exists()
    assert build.run(False, True, av_db=av, legacy_db=absent, dest_db=dest) == 0
    assert _tables(dest) == PUBLISHED_TABLES  # 发布产物 = 上述 6 张（实测）
    assert len(_table_rows(dest, "canonical_sessions")) == 1

    # 二次发布走 union 路径：目标库只有白名单内的表，守卫不得误拒
    assert build.run(False, True, av_db=av, legacy_db=absent, dest_db=dest) == 0
    assert _tables(dest) == PUBLISHED_TABLES
    assert len(_table_rows(dest, "canonical_sessions")) == 1


def test_shifted_address_is_retained_next_to_the_old_row(tmp_path):
    """A re-captured message whose address shifted is kept, never replaced.

    An origin file that is rewritten (not appended) moves the physical line the
    address is derived from, so the same message arrives under a new id while
    the row captured under the old id is still in the store. The store is a
    collection: the old row stays exactly as collected (same rowid, never
    deleted) and the new row is inserted next to it. Superseding it by content
    would DELETE the old row and recycle its rowid behind the monotonic rowid
    cursor in ``retrieval/conversation_fts.py``.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        CompatibilityProjectionReport,
        ProjectionFingerprint,
        upsert_compatibility_projection,
    )

    db = tmp_path / "auth.sqlite"
    con = sqlite3.connect(db)
    for stmt in DDL:
        con.execute(stmt)

    old_id = "cm|codex|S1|rollout.jsonl#L103"     # address at capture time
    new_id = "cm|codex|S1|rollout.jsonl#L113"     # same message, file rewritten
    con.execute(
        "INSERT INTO canonical_sessions (canonical_session_id, primary_source,"
        " agent, message_count, user_message_count, evidence_eligible,"
        " evidence_scope, merged, lifecycle) VALUES ('cs|codex|S1','legacy',"
        "'codex',1,1,1,'user',0,'active')")
    con.execute(
        "INSERT INTO canonical_messages (canonical_message_id,"
        " canonical_session_id, source, source_message_ref, ordinal, role,"
        " content, content_length, timestamp, is_system, is_sidechain,"
        " evidence_scope) VALUES (?, 'cs|codex|S1', 'legacy',"
        " 'rollout.jsonl#L103', 1, 'user', 'same turn', 9,"
        " '2026-08-03T02:21:48Z', 0, 0, 'user')",
        (old_id,))
    con.commit()

    report = CompatibilityProjectionReport(
        generation_id="gen-1",
        sessions=(),
        messages=({
            "canonical_message_id": new_id,
            "canonical_session_id": "cs|codex|S1",
            "source": "legacy",
            "source_message_ref": "rollout.jsonl#L113",
            "ordinal": 1,
            "role": "user",
            "content": "same turn",
            "content_length": 9,
            "timestamp": "2026-08-03T02:21:48Z",
            "model": None,
            "is_system": 0,
            "is_sidechain": 0,
            "content_hash": None,
            "evidence_scope": "user",
        },),
        tools=(),
        excluded=(),
        fingerprint=ProjectionFingerprint("gen-1", 0, 1, 0, "digest"),
    )
    upsert_compatibility_projection(con, report)
    rows = con.execute(
        "SELECT canonical_message_id, rowid FROM canonical_messages"
        " ORDER BY rowid").fetchall()
    con.close()
    # the pre-existing row is retained with its rowid; the shifted one lands
    # after it — nothing was deleted.
    assert rows == [(old_id, 1), (new_id, 2)], rows


def test_content_merge_keeps_genuinely_repeated_turns(tmp_path):
    """Two identical user turns stay two rows (the writer never dedups by content)."""
    from personal_knowledge.application.conversation.compatibility_projection import (
        CompatibilityProjectionReport,
        ProjectionFingerprint,
        upsert_compatibility_projection,
    )

    db = tmp_path / "auth.sqlite"
    con = sqlite3.connect(db)
    for stmt in DDL:
        con.execute(stmt)
    for i, mid in enumerate(("cm|codex|S1|L1", "cm|codex|S1|L2"), start=1):
        con.execute(
            "INSERT INTO canonical_messages (canonical_message_id,"
            " canonical_session_id, source, ordinal, role, content,"
            " content_length, timestamp, is_system, is_sidechain,"
            " evidence_scope) VALUES (?, 'cs|codex|S1', 'legacy', ?, 'user',"
            " 'ok', 2, '2026-08-03T02:21:48Z', 0, 0, 'user')",
            (mid, i))
    con.commit()

    report = CompatibilityProjectionReport(
        generation_id="gen-1",
        sessions=(),
        messages=tuple({
            "canonical_message_id": f"cm|codex|S1|L{i}",
            "canonical_session_id": "cs|codex|S1",
            "source": "legacy",
            "source_message_ref": f"L{i}",
            "ordinal": i,
            "role": "user",
            "content": "ok",
            "content_length": 2,
            "timestamp": "2026-08-03T02:21:48Z",
            "model": None,
            "is_system": 0,
            "is_sidechain": 0,
            "content_hash": None,
            "evidence_scope": "user",
        } for i in (1, 2)),
        tools=(),
        excluded=(),
        fingerprint=ProjectionFingerprint("gen-1", 0, 2, 0, "digest"),
    )
    upsert_compatibility_projection(con, report)
    rows = con.execute(
        "SELECT canonical_message_id FROM canonical_messages"
        " ORDER BY canonical_message_id").fetchall()
    con.close()
    assert rows == [("cm|codex|S1|L1",), ("cm|codex|S1|L2",)], rows


def test_projection_dedups_shared_address_keeping_richer_copy():
    """One native session discovered as two ce sessions must not lose rows.

    Measured on the 2026-09-23 re-import: 30 native sessions had two ce
    sessions inside one generation, so 1,204 message rows and 11,154 tool rows
    shared a canonical id and a blind INSERT OR REPLACE silently dropped
    whichever arrived last — sometimes the longer copy. The projection now
    collapses duplicates per id and keeps the richer body.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "codex", "native_session_id": "S1"},
        {"session_id": "ce-b", "family": "codex", "native_session_id": "S1"},
    ]
    event_rows = [
        # same address, shorter body first
        {"event_id": "e1", "session_id": "ce-a", "kind": "assistant_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L10",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "short"},
        # same address, longer body second (must win)
        {"event_id": "e2", "session_id": "ce-b", "kind": "assistant_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L10",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "the longer captured body"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    assert len(report.messages) == 1
    assert report.messages[0]["content"] == "the longer captured body"
    assert report.collapsed_duplicate_ids == 1
    # and the id is the origin-derived one
    assert report.messages[0]["canonical_message_id"] == \
        "cm|codex|S1|rollout.jsonl#L10"
def test_projection_merges_session_copies_of_one_native_session():
    """Two ce sessions of one native session merge into ONE canonical row.

    ``_project_sessions`` emits one row per ce session and origin-derived ids
    make every copy of the same native session share a canonical_session_id;
    the writer keys rows by id, so the last copy used to win wholesale —
    started_at/ended_at/counts depended on slot-hash order and flipped on
    every re-capture. The merge keeps started=min, ended=max, counts=max.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "codex", "native_session_id": "S1",
         "started_at": "2026-08-03T02:00:00Z",
         "ended_at": "2026-08-03T03:00:00Z",
         "cwd": None, "git_branch": "main", "model": None},
        {"session_id": "ce-b", "family": "codex", "native_session_id": "S1",
         "started_at": "2026-08-03T01:30:00Z",
         "ended_at": "2026-08-03T03:30:00Z",
         "cwd": "/repo", "git_branch": "main", "model": "gpt-5"},
    ]
    event_rows = [
        # ce-a saw one user turn; ce-b (a later, fuller capture) saw two.
        {"event_id": "e1", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L10",
         "occurred_at": "2026-08-03T02:10:00Z", "ordinal": 1,
         "content": "turn one"},
        {"event_id": "e2", "session_id": "ce-b", "kind": "user_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L10",
         "occurred_at": "2026-08-03T02:10:00Z", "ordinal": 1,
         "content": "turn one"},
        {"event_id": "e3", "session_id": "ce-b", "kind": "user_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L20",
         "occurred_at": "2026-08-03T02:20:00Z", "ordinal": 2,
         "content": "turn two"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)

    sessions = report.sessions
    assert len(sessions) == 1, sessions
    row = sessions[0]
    assert row["canonical_session_id"] == "cs|codex|S1"
    assert row["started_at"] == "2026-08-03T01:30:00Z"   # min across copies
    assert row["ended_at"] == "2026-08-03T03:30:00Z"     # max across copies
    assert row["message_count"] == 2                     # max across copies
    assert row["user_message_count"] == 2                # max across copies
    assert row["cwd"] == "/repo"                         # non-empty wins
    assert row["model"] == "gpt-5"                       # non-empty wins
    # and the rows stay reproducible on re-computation
    again = compute_projection("gen-1", session_rows, event_rows)
    assert again.sessions == sessions


def test_projection_keeps_explicit_empty_content_over_summary_copy():
    """A legitimate ``content=""`` copy must not be overwritten by summary.

    ``""`` is a legitimate tool-only/empty native message (documented in
    ``_project_messages``). The collapse between same-address copies used to
    compare bare ``len(content or summary)``, which let the summary fallback
    prose win over an explicitly empty body. Now an explicit ``content`` —
    even empty — beats a summary-only copy.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "codex", "native_session_id": "S1"},
        {"session_id": "ce-b", "family": "codex", "native_session_id": "S1"},
    ]
    event_rows = [
        # same address: an older adapter stored only the bounded summary
        {"event_id": "e1", "session_id": "ce-a", "kind": "assistant_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L10",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": None, "summary": "bounded summary prose fallback"},
        # a newer capture maps the exact body: an explicit empty string
        {"event_id": "e2", "session_id": "ce-b", "kind": "assistant_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#L10",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "", "summary": None},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    assert len(report.messages) == 1
    assert report.messages[0]["content"] == ""
    assert report.messages[0]["content_length"] == 0


def test_projection_mirror_recapture_keeps_one_id_for_native_id_families():
    """One native message collected through two mirror paths = ONE cm id.

    The locator embeds the mirror-relative path of the artifact a capture was
    staged from, so locator-first addressing gave the same native message a
    different cm id per mirror path (measured 2026-09-24: ~770 duplicate rows
    out of 4,007 in one canonical session). Native-id-first families address
    the message by its mirror-path-free client uuid instead, so the second
    capture collapses onto the first row (the ``seen`` mechanism keeps the
    richer copy when the bytes differ).
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "claude", "native_session_id": "S1"},
        {"session_id": "ce-b", "family": "claude", "native_session_id": "S1"},
    ]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": "uuid-u1",
         "native_locator": "mirror-a/projects/p/agent-x.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "hello"},
        {"event_id": "e2", "session_id": "ce-b", "kind": "user_message",
         "native_event_id": "uuid-u1",
         "native_locator": "mirror-b/projects/p/agent-x.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "hello"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    assert len(report.messages) == 1
    assert report.messages[0]["canonical_message_id"] == "cm|claude|S1|uuid-u1"
    assert report.collapsed_duplicate_ids == 1


def test_projection_mirror_recapture_keeps_richer_bytes_per_native_id():
    """Same native id, different captured bytes: the richer copy wins."""
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "claude", "native_session_id": "S1"},
        {"session_id": "ce-b", "family": "claude", "native_session_id": "S1"},
    ]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-a", "kind": "assistant_message",
         "native_event_id": "uuid-a1",
         "native_locator": "mirror-a/agent-x.jsonl#L2",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "truncated"},
        {"event_id": "e2", "session_id": "ce-b", "kind": "assistant_message",
         "native_event_id": "uuid-a1",
         "native_locator": "mirror-b/agent-x.jsonl#L2",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "the full assistant answer"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    assert len(report.messages) == 1
    assert report.messages[0]["content"] == "the full assistant answer"


def test_projection_codex_keeps_locator_first_for_mirror_copies():
    """codex native ids are placeholders: mirror copies must NOT collapse.

    The codex adapter records the literal 'agent_message' as the native id of
    446 message events in one session (measured 2026-09-23), so its address
    rule stays locator-first — two mirror paths yield two distinct ids.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "codex", "native_session_id": "S1"},
        {"session_id": "ce-b", "family": "codex", "native_session_id": "S1"},
    ]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-a", "kind": "assistant_message",
         "native_event_id": "agent_message",
         "native_locator": "rollout-a.jsonl#L10",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "hello"},
        {"event_id": "e2", "session_id": "ce-b", "kind": "assistant_message",
         "native_event_id": "agent_message",
         "native_locator": "rollout-b.jsonl#L10",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "hello"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    ids = {m["canonical_message_id"] for m in report.messages}
    assert ids == {"cm|codex|S1|rollout-a.jsonl#L10",
                   "cm|codex|S1|rollout-b.jsonl#L10"}


def test_projection_placeholder_native_id_falls_back_to_locator():
    """A native id repeated with conflicting content is a placeholder.

    If one canonical session's native ids do not identify messages (the same
    id carrying different bodies), id-first would collapse genuinely different
    messages onto one row. That session keeps the locator-first rule, so every
    distinct message survives under its own address.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "claude", "native_session_id": "S1"},
    ]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": "user", "native_locator": "agent-x.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "first question"},
        {"event_id": "e2", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": "user", "native_locator": "agent-x.jsonl#L5",
         "occurred_at": "2026-08-03T02:10:00Z", "ordinal": 2,
         "content": "second question"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    ids = {m["canonical_message_id"] for m in report.messages}
    assert ids == {"cm|claude|S1|agent-x.jsonl#L1",
                   "cm|claude|S1|agent-x.jsonl#L5"}


def test_projection_placeholder_detection_is_per_session():
    """A placeholder id in one session must not change another session's ids.

    The fallback is decided per canonical session: session T2's healthy uuids
    keep native-id-first addressing even though session T1's ids collapsed.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-1", "family": "claude", "native_session_id": "T1"},
        {"session_id": "ce-2", "family": "claude", "native_session_id": "T2"},
    ]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-1", "kind": "user_message",
         "native_event_id": "user", "native_locator": "a.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "one"},
        {"event_id": "e2", "session_id": "ce-1", "kind": "user_message",
         "native_event_id": "user", "native_locator": "a.jsonl#L2",
         "occurred_at": "2026-08-03T02:10:00Z", "ordinal": 2,
         "content": "two"},
        {"event_id": "e3", "session_id": "ce-2", "kind": "user_message",
         "native_event_id": "uuid-ok", "native_locator": "b.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1,
         "content": "healthy"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    ids = {m["canonical_message_id"] for m in report.messages}
    assert ids == {"cm|claude|T1|a.jsonl#L1",
                   "cm|claude|T1|a.jsonl#L2",
                   "cm|claude|T2|uuid-ok"}


# --------------------------------------------------------------------------
# P1-3: the writer reconciles incoming rows against the rows already stored
# (read-merge-write), so a live-sync round that applies only one slot cannot
# overwrite a richer body an earlier round stored from another slot.
# --------------------------------------------------------------------------

def _upsert_con(tmp_path, name="reconcile"):
    con = sqlite3.connect(tmp_path / f"{name}.sqlite")
    for stmt in DDL:
        con.execute(stmt)
    return con


def _projection_message(mid, content, **overrides):
    row = {
        "canonical_message_id": mid,
        "canonical_session_id": "cs|codex|S1",
        "source": "legacy",
        "source_message_ref": "rollout.jsonl#L10",
        "ordinal": 1,
        "role": "assistant",
        "content": content,
        "content_length": len(content or ""),
        "timestamp": "2026-08-03T02:00:00Z",
        "model": None,
        "is_system": 0,
        "is_sidechain": 0,
        "content_hash": None,
        "evidence_scope": "user",
    }
    row.update(overrides)
    return row


def _projection_tool(tid, name, length, **overrides):
    row = {
        "canonical_tool_id": tid,
        "canonical_session_id": "cs|codex|S1",
        "source": "legacy",
        "source_kind": "call",
        "tool_name": name,
        "category": None,
        "status": "ok",
        "call_index": 1,
        "subagent_session_id": None,
        "content_length": length,
        "timestamp": "2026-08-03T02:00:00Z",
    }
    row.update(overrides)
    return row


def _projection_session(csid, **overrides):
    row = {
        "canonical_session_id": csid,
        "primary_source": "legacy",
        "agent": "codex",
        "started_at": "2026-08-03T02:00:00Z",
        "ended_at": "2026-08-03T03:00:00Z",
        "message_count": 1,
        "user_message_count": 1,
        "file_hash": None,
        "parent_canonical_id": None,
        "relationship_type": None,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "evidence_eligible": 1,
        "evidence_scope": "user",
        "merged": 0,
        "lifecycle": "active",
        "superseded_by_canonical_id": None,
    }
    row.update(overrides)
    return row


def _reconcile_report(sessions=(), messages=(), tools=()):
    from personal_knowledge.application.conversation.compatibility_projection import (
        CompatibilityProjectionReport,
        ProjectionFingerprint,
    )
    return CompatibilityProjectionReport(
        generation_id="gen-1",
        sessions=tuple(sessions),
        messages=tuple(messages),
        tools=tuple(tools),
        excluded=(),
        fingerprint=ProjectionFingerprint("gen-1", len(sessions),
                                          len(messages), len(tools), "digest"),
    )


def test_upsert_reconcile_keeps_richer_stored_message_across_applies(tmp_path):
    """Slot A stored a rich body; slot B's poorer re-capture must not win.

    This is the P1-3 cross-apply shape: live sync projects only the slots a
    round touched, so the poor copy of slot B arrives in a *separate*
    ``upsert_compatibility_projection`` call — with no in-call ``seen`` dict
    to protect it. The writer must reconcile against the stored row and
    write nothing; the reverse order must still fill in the rich body.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    mid = "cm|claude|S1|uuid-a1"
    rich = _projection_message(mid, "the full assistant answer")
    poor = _projection_message(mid, "truncated")

    # rich first (slot A), then a poor re-capture (slot B)
    con = _upsert_con(tmp_path)
    first = upsert_compatibility_projection(
        con, _reconcile_report(messages=[rich]))
    assert first["canonical_messages"] == {"inserted": 1, "updated": 0}, first
    counts = upsert_compatibility_projection(
        con, _reconcile_report(messages=[poor]))
    body = con.execute(
        "SELECT content, content_length FROM canonical_messages"
        " WHERE canonical_message_id=?", (mid,)).fetchone()
    con.close()
    assert body == ("the full assistant answer", 25), body
    assert counts["canonical_messages"] == {"inserted": 0, "updated": 0}, counts

    # reverse order: poor copy stored first, rich copy must upgrade it
    con = _upsert_con(tmp_path, name="reconcile-reversed")
    upsert_compatibility_projection(con, _reconcile_report(messages=[poor]))
    upsert_compatibility_projection(con, _reconcile_report(messages=[rich]))
    body = con.execute(
        "SELECT content, content_length FROM canonical_messages"
        " WHERE canonical_message_id=?", (mid,)).fetchone()
    con.close()
    assert body == ("the full assistant answer", 25), body


def test_upsert_reconcile_keeps_explicit_empty_body_over_summary_row(tmp_path):
    """The stored-row reconciliation uses the same key as ``_richer``.

    An explicit ``content=""`` row must not be overwritten by a summary-only
    copy (``content=None``), even though the latter's prose is longer.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    mid = "cm|codex|S1|rollout.jsonl#L10"
    empty = _projection_message(mid, "")
    summary_only = _projection_message(mid, None, content_length=None,
                                       source_message_ref="other.jsonl#L10")
    con = _upsert_con(tmp_path)
    upsert_compatibility_projection(con, _reconcile_report(messages=[empty]))
    counts = upsert_compatibility_projection(
        con, _reconcile_report(messages=[summary_only]))
    body = con.execute(
        "SELECT content FROM canonical_messages"
        " WHERE canonical_message_id=?", (mid,)).fetchone()
    con.close()
    assert body == ("",), body
    assert counts["canonical_messages"] == {"inserted": 0, "updated": 0}, counts


def test_upsert_reconcile_keeps_richer_stored_tool_across_applies(tmp_path):
    """Same reconciliation for canonical_tool_events (summary length)."""
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    tid = "ct|codex|S1|L10"
    rich = _projection_tool(tid, "Read file src/app/main.py line 1-400", 36)
    poor = _projection_tool(tid, "Read file", 9)

    con = _upsert_con(tmp_path)
    upsert_compatibility_projection(con, _reconcile_report(tools=[rich]))
    counts = upsert_compatibility_projection(
        con, _reconcile_report(tools=[poor]))
    row = con.execute(
        "SELECT tool_name, content_length FROM canonical_tool_events"
        " WHERE canonical_tool_id=?", (tid,)).fetchone()
    con.close()
    assert row == ("Read file src/app/main.py line 1-400", 36), row
    assert counts["canonical_tool_events"] == {"inserted": 0, "updated": 0}, counts

    # reverse order: the richer candidate upgrades the poorer stored row
    con = _upsert_con(tmp_path, name="reconcile-reversed")
    upsert_compatibility_projection(con, _reconcile_report(tools=[poor]))
    upsert_compatibility_projection(con, _reconcile_report(tools=[rich]))
    row = con.execute(
        "SELECT tool_name, content_length FROM canonical_tool_events"
        " WHERE canonical_tool_id=?", (tid,)).fetchone()
    con.close()
    assert row == ("Read file src/app/main.py line 1-400", 36), row


def test_upsert_reconcile_merges_session_fields_with_stored_row(tmp_path):
    """A later, partial apply merges session fields instead of overwriting.

    started=min / ended=max / cwd / model follow ``_merge_session_copies``;
    fields the projection does not merge (agent, lifecycle, ...) keep the
    stored row's value. The counts deliberately follow the candidate even
    when smaller: they are derived from the round's stale-filtered event
    set, so a truncated source must be able to decrease them (the
    live-sync truncation contract).
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    csid = "cs|codex|S1"
    stored = _projection_session(
        csid, started_at="2026-08-03T02:00:00Z",
        ended_at="2026-08-03T03:00:00Z",
        message_count=1, user_message_count=1,
        cwd=None, model=None, lifecycle="active")
    candidate = _projection_session(
        csid, started_at="2026-08-03T01:30:00Z",
        ended_at="2026-08-03T02:30:00Z",  # earlier than stored: loses
        message_count=3, user_message_count=2,
        cwd="/repo", model="gpt-5", lifecycle="archived")

    con = _upsert_con(tmp_path)
    upsert_compatibility_projection(con, _reconcile_report(sessions=[stored]))
    counts = upsert_compatibility_projection(
        con, _reconcile_report(sessions=[candidate]))
    row = con.execute(
        "SELECT started_at, ended_at, message_count, user_message_count,"
        " cwd, model, lifecycle FROM canonical_sessions"
        " WHERE canonical_session_id=?", (csid,)).fetchone()
    con.close()
    assert row == ("2026-08-03T01:30:00Z", "2026-08-03T03:00:00Z", 3, 2,
                   "/repo", "gpt-5", "active"), row
    assert counts["canonical_sessions"] == {"inserted": 0, "updated": 1}, counts

    # a round that saw fewer live events (e.g. after truncation) drops the
    # derived counts, while the monotonic window keeps the wider span
    con = _upsert_con(tmp_path, name="reconcile-shrink")
    fuller = _projection_session(
        csid, started_at="2026-08-03T01:30:00Z",
        ended_at="2026-08-03T03:30:00Z",
        message_count=6, user_message_count=3)
    truncated = _projection_session(
        csid, started_at="2026-08-03T01:30:00Z",
        ended_at="2026-08-03T02:30:00Z",
        message_count=4, user_message_count=2)
    upsert_compatibility_projection(con, _reconcile_report(sessions=[fuller]))
    upsert_compatibility_projection(
        con, _reconcile_report(sessions=[truncated]))
    row = con.execute(
        "SELECT started_at, ended_at, message_count, user_message_count"
        " FROM canonical_sessions WHERE canonical_session_id=?",
        (csid,)).fetchone()
    con.close()
    assert row == ("2026-08-03T01:30:00Z", "2026-08-03T03:30:00Z", 4, 2), row


def test_upsert_reconcile_stays_idempotent_on_replay(tmp_path):
    """Re-applying identical rows still writes nothing (unchanged semantics)."""
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    report = _reconcile_report(
        sessions=[_projection_session("cs|codex|S1")],
        messages=[_projection_message("cm|codex|S1|L10", "hello")],
        tools=[_projection_tool("ct|codex|S1|L10", "Bash", 4)],
    )
    con = _upsert_con(tmp_path)
    upsert_compatibility_projection(con, report)
    counts = upsert_compatibility_projection(con, report)
    con.close()
    assert counts == {
        "canonical_sessions": {"inserted": 0, "updated": 0},
        "canonical_messages": {"inserted": 0, "updated": 0},
        "canonical_tool_events": {"inserted": 0, "updated": 0},
    }, counts


def test_upsert_reconcile_can_be_disabled(tmp_path, monkeypatch):
    """The escape hatch restores the pre-P1-3 candidate-wins behaviour."""
    import personal_knowledge.application.conversation.compatibility_projection as cp

    monkeypatch.setattr(cp, "MERGE_WITH_STORED", False)
    mid = "cm|claude|S1|uuid-a1"
    rich = _projection_message(mid, "the full assistant answer")
    poor = _projection_message(mid, "truncated")

    con = _upsert_con(tmp_path)
    cp.upsert_compatibility_projection(con, _reconcile_report(messages=[rich]))
    cp.upsert_compatibility_projection(con, _reconcile_report(messages=[poor]))
    body = con.execute(
        "SELECT content FROM canonical_messages"
        " WHERE canonical_message_id=?", (mid,)).fetchone()
    con.close()
    assert body == ("truncated",), body


# --------------------------------------------------------------------------
# P1-14a: 内容被 UPDATE 实际改写的 canonical_messages 行记入 ce_fts_invalidate。
# 同 id in-place UPDATE 不动 rowid，而 retrieval/conversation_fts 的增量游标
# 只读 rowid > watermark 的新行——没有这张失效台账，改写后的正文永远进不了
# FTS 索引。消费端（FTS 只读消费）的用例见 tests/test_conversation_fts.py。
# --------------------------------------------------------------------------

def test_upsert_content_change_records_fts_invalidation(tmp_path):
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    mid = "cm|codex|S1|rollout.jsonl#L10"
    con = _upsert_con(tmp_path)
    upsert_compatibility_projection(
        con, _reconcile_report(messages=[_projection_message(mid, "first")]))
    counts = upsert_compatibility_projection(
        con, _reconcile_report(
            messages=[_projection_message(mid, "the rewritten longer body")]))
    rows = con.execute(
        "SELECT canonical_message_id FROM ce_fts_invalidate").fetchall()
    con.close()
    assert counts["canonical_messages"] == {"inserted": 0, "updated": 1}, counts
    assert rows == [(mid,)], rows


def test_upsert_without_content_change_writes_no_invalidation(tmp_path):
    """三种"内容没变"的形态都不得进失效台账：幂等重放、只变时间戳、更穷重捕获。"""
    from personal_knowledge.application.conversation.compatibility_projection import (
        upsert_compatibility_projection,
    )

    mid = "cm|codex|S1|rollout.jsonl#L10"
    con = _upsert_con(tmp_path)
    upsert_compatibility_projection(
        con, _reconcile_report(
            messages=[_projection_message(mid, "stable body", timestamp="t1")]))

    # 幂等重放：merged == prior，连 UPDATE 都不发
    counts = upsert_compatibility_projection(
        con, _reconcile_report(
            messages=[_projection_message(mid, "stable body", timestamp="t1")]))
    assert counts["canonical_messages"] == {"inserted": 0, "updated": 0}, counts
    # 只变时间戳：UPDATE 发生，但 content 列未变，FTS 无需重索引
    counts = upsert_compatibility_projection(
        con, _reconcile_report(
            messages=[_projection_message(mid, "stable body", timestamp="t2")]))
    assert counts["canonical_messages"] == {"inserted": 0, "updated": 1}, counts
    # 更穷的重捕获：P1-3 富者胜，stored 原样保留，无写动作
    counts = upsert_compatibility_projection(
        con, _reconcile_report(messages=[_projection_message(mid, "poor")]))
    assert counts["canonical_messages"] == {"inserted": 0, "updated": 0}, counts

    rows = con.execute(
        "SELECT canonical_message_id FROM ce_fts_invalidate").fetchall()
    con.close()
    assert rows == [], rows


# --------------------------------------------------------------------------
# P1-19: 无 native_session_id 会话的回退键改为该会话自身事件内容的确定性
# 摘要（ced:<sha256 前 16 位>）。旧回退键 ce:{session_id} 内嵌槽位身份，
# 同内容多副本永不塌缩。
# --------------------------------------------------------------------------


def _no_native_ce_session(sid):
    return {"session_id": sid, "family": "grok", "native_session_id": None}


def _proj_event(eid, sid, kind, content, ordinal=1):
    return {"event_id": eid, "session_id": sid, "kind": kind,
            "native_event_id": None,
            "native_locator": f"chat-{sid}.jsonl#L{ordinal}",
            "occurred_at": "2026-08-03T02:00:00Z", "ordinal": ordinal,
            "content": content}


def test_no_native_session_copies_with_same_content_collapse():
    """同内容的无原生会话副本必须塌缩成一个 canonical 会话。

    两个 ce 会话的 event_id / session_id / locator 全不相同（各自经不同槽位
    捕获），但事件内容集合一致——回退键只取 (kind, body) 集合摘要，与槽位
    身份无关，因此两副本共享一个 canonical_session_id 并合并。
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [_no_native_ce_session("ce-slot-a"),
                    _no_native_ce_session("ce-slot-b")]
    event_rows = [
        _proj_event("e1", "ce-slot-a", "user_message", "hello"),
        _proj_event("e2", "ce-slot-a", "assistant_message", "hi there",
                    ordinal=2),
        _proj_event("e9", "ce-slot-b", "user_message", "hello"),
        _proj_event("e8", "ce-slot-b", "assistant_message", "hi there",
                    ordinal=2),
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    assert len(report.sessions) == 1, report.sessions
    csid = report.sessions[0]["canonical_session_id"]
    assert csid.startswith("cs|grok|ced:"), csid
    # 合并规则照旧：started/ended 取窗口，计数取各副本最大值
    assert report.sessions[0]["message_count"] == 2
    # 两副本的消息都落在同一个 canonical 会话下（grok locator-first，消息
    # 行仍按各自 locator 分行——塌缩的是会话身份，不是消息地址）
    assert {m["canonical_session_id"] for m in report.messages} == {csid}
    assert len(report.messages) == 4
    # 可复算：同样的输入必须得到同一个键
    again = compute_projection("gen-1", session_rows, event_rows)
    assert again.sessions == report.sessions


def test_no_native_sessions_with_different_content_stay_separate():
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [_no_native_ce_session("ce-slot-a"),
                    _no_native_ce_session("ce-slot-b")]
    event_rows = [
        _proj_event("e1", "ce-slot-a", "user_message", "hello"),
        _proj_event("e2", "ce-slot-b", "user_message", "a different talk"),
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    ids = {s["canonical_session_id"] for s in report.sessions}
    assert len(ids) == 2, ids
    assert all(i.startswith("cs|grok|ced:") for i in ids)


def test_no_native_empty_session_keeps_slot_key():
    """空事件会话没有内容身份可塌缩，保留旧槽位键（合并空壳是任意的）。"""
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    report = compute_projection("gen-1", [_no_native_ce_session("ce-empty")], [])
    assert report.sessions[0]["canonical_session_id"] == "cs|grok|ce:ce-empty"


# --------------------------------------------------------------------------
# P1-20: _sanitize 的 '|'→'/' 替换可把不同原始键折叠到同一 canonical id。
# id 公式固化在 61 万+ 存量 id 里不能改，碰撞只能观测：投影报告暴露
# sanitized_id_collisions，迁移规划写 stats['id_collisions']。
# --------------------------------------------------------------------------


def test_projection_counts_sanitized_id_collision():
    """a|b 与 a/b 两个原始地址 → 同一 id：公式不变，碰撞计数=1。"""
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [{"session_id": "ce-a", "family": "codex",
                     "native_session_id": "S1"}]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#a|b",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1, "content": "one"},
        {"event_id": "e2", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": None, "native_locator": "rollout.jsonl#a/b",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 2, "content": "two"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    # 公式不变：两个不同原始地址得到同一个 id
    ids = {m["canonical_message_id"] for m in report.messages}
    assert ids == {"cm|codex|S1|rollout.jsonl#a/b"}, ids
    # 碰撞可观测：第二个"新"原始键落 在已被占用的 id 上，计 1；同址塌缩
    # 机制照常计数 collapsed_duplicate_ids 并保留富者/字典序胜者
    assert report.sanitized_id_collisions == 1
    assert report.collapsed_duplicate_ids == 1
    assert report.messages[0]["content"] == "one"
    assert report.to_dict()["sanitized_id_collisions"] == 1


def test_projection_same_raw_key_recurrence_is_not_a_collision():
    """同址重捕获（原始键完全相同）是合法塌缩，不得计入碰撞。"""
    from personal_knowledge.application.conversation.compatibility_projection import (
        compute_projection,
    )

    session_rows = [
        {"session_id": "ce-a", "family": "claude", "native_session_id": "S1"},
        {"session_id": "ce-b", "family": "claude", "native_session_id": "S1"},
    ]
    event_rows = [
        {"event_id": "e1", "session_id": "ce-a", "kind": "user_message",
         "native_event_id": "uuid-u1", "native_locator": "mirror-a/x.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1, "content": "hi"},
        {"event_id": "e2", "session_id": "ce-b", "kind": "user_message",
         "native_event_id": "uuid-u1", "native_locator": "mirror-b/x.jsonl#L1",
         "occurred_at": "2026-08-03T02:00:00Z", "ordinal": 1, "content": "hi"},
    ]
    report = compute_projection("gen-1", session_rows, event_rows)
    assert len(report.messages) == 1
    assert report.collapsed_duplicate_ids == 1
    assert report.sanitized_id_collisions == 0


def test_plan_reports_sanitized_id_collisions(tmp_path):
    """迁移规划路径同样计数；重复 id 本就被 verify fail-closed，报告说明成因。"""
    path = tmp_path / "collide.sqlite"
    con = sqlite3.connect(path)
    for stmt in DDL:
        con.execute(stmt)
    con.execute("INSERT INTO ce_generation_authority VALUES ('g',1,"
                "'2026-01-01T00:00:00Z')")
    _insert_ce(con, "ceK", "codex", "K1", "g")
    _insert_event(con, "ek1", "ceK", "user_message", None,
                  "rollout.jsonl#a|b", "2026-01-01T01:00:00Z")
    _insert_event(con, "ek2", "ceK", "user_message", None,
                  "rollout.jsonl#a/b", "2026-01-01T01:01:00Z")
    cs_k = u.v2_session_hash("ceK")
    _insert_session(con, cs_k, "legacy", "codex", "2026-01-01T01:00:00Z")
    _insert_message(con, u.v2_message_hash("ek1"), cs_k, "legacy",
                    "rollout.jsonl#a|b", 1, "user", "one",
                    "2026-01-01T01:00:00Z", "h1")
    _insert_message(con, u.v2_message_hash("ek2"), cs_k, "legacy",
                    "rollout.jsonl#a/b", 2, "user", "two",
                    "2026-01-01T01:01:00Z", "h2")
    con.commit()
    con.close()

    plan, problems = _plan(path)
    assert plan.stats["id_collisions"] == 1
    # 公式不变：两个原始地址映射到同一个新 id
    new_ids = {n for t, _o, n in plan.id_map if t == "canonical_messages"}
    assert new_ids == {"cm|codex|K1|rollout.jsonl#a/b"}
    # 规划结果含重复 id 时 verify 必须拦下（fail-closed 不因计数而放宽）
    assert any("duplicate new messages ids" in p for p in problems), problems