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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
    # the locator wins over the native id (codex records the literal
    # 'agent_message' as a native id, so ids are not address-safe)
    assert "cm|claude|S-native-1|agent-x.jsonl#L1" in msgs
    assert "cm|claude|S-native-1|agent-x.jsonl#L2" in msgs
    assert msgs["cm|claude|S-native-1|agent-x.jsonl#L2"]["content_length"] \
        == len("hi there")


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


def test_same_address_two_captures_keeps_newest_only(db):
    plan, problems = _plan(db)
    assert problems == []
    rows = [r for r in plan.messages
            if r["canonical_session_id"] == "cs|claude|S-native-1"
            and r["canonical_message_id"].endswith("agent-x.jsonl#L1")]
    assert len(rows) == 1
    # the active generation's capture wins over the stale one
    assert rows[0]["content_hash"] == "h1"
    m = {old: new for t, old, new in plan.id_map if t == "canonical_messages"}
    stale = u.v2_message_hash("ev1b")
    assert m[stale] == "cm|claude|S-native-1|agent-x.jsonl#L1"


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
        " canonical_message_id='cm|claude|S-native-1|agent-x.jsonl#L2'").fetchone()
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
    # uniform ids pass, and a DB without the map is never blocked
    build._assert_publish_compatible(work, [
        {"canonical_session_id": "cs|claude|S-native-1"},
    ])
    build._assert_publish_compatible(db, [
        {"canonical_session_id": "cs|ad0eeb10bebed2eb976dffab99ab83e5"},
    ])


def test_content_merge_replaces_shifted_address(tmp_path):
    """A re-captured message whose address shifted must replace, not duplicate.

    An origin file that is rewritten (not appended) moves the physical line the
    address is derived from, so the same message arrives under a new id while
    the row captured under the old id is still in the store. The publish must
    supersede it by content.
    """
    from personal_knowledge.application.conversation.compatibility_projection import (
        CompatibilityProjectionReport,
        ProjectionFingerprint,
        write_compatibility_projection,
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
    write_compatibility_projection(con, report)
    rows = con.execute(
        "SELECT canonical_message_id FROM canonical_messages").fetchall()
    con.close()
    assert rows == [(new_id,)], rows


def test_content_merge_keeps_genuinely_repeated_turns(tmp_path):
    """Two identical user turns stay two rows (multiset, not set, semantics)."""
    from personal_knowledge.application.conversation.compatibility_projection import (
        CompatibilityProjectionReport,
        ProjectionFingerprint,
        write_compatibility_projection,
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
    write_compatibility_projection(con, report)
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
