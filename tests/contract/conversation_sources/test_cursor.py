"""cursor 家族（生产模块 ``cursor``，ADAPTER_VERSION 1.0.0）适配契约。

公开 seam 是 registry（``adapt_for`` / ``detect_family``）——生产代码只经它
调用家族模块，所以断言也走同一入口，不直接摸 ``cursor.adapt``。夹具来自
``support/cursor.py``，正文全是合成句子。
"""

from __future__ import annotations

import sqlite3

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind, make_event_id
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import cursor as cursor_fixtures


class TestCursor:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("cursor")
        db = tmp / "cursor.db"
        cursor_fixtures.make_cursor_db(db)
        artifact, root = artifacts.captured_sqlite(
            db, tmp,
            allowed_tables=cursor_fixtures.SQLITE_TABLES,
            allowed_columns=cursor_fixtures.SQLITE_COLUMNS,
            byte_limit=1_000_000, count_limit=4,
        )
        return registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)

    def test_detect_supported_store(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("cursor-detect")
        db = tmp / "cursor.db"
        cursor_fixtures.make_cursor_db(db)
        artifact, root = artifacts.captured_sqlite(
            db, tmp,
            allowed_tables=cursor_fixtures.SQLITE_TABLES,
            allowed_columns=cursor_fixtures.SQLITE_COLUMNS,
            byte_limit=1_000_000, count_limit=4,
        )
        assert registry.detect_family("cursor", artifact, artifact_root=root) is True

    def test_kinds(self, adapted):
        result = adapted
        kinds = {e.kind for e in result.events}
        assert EventKind.SESSION_LIFECYCLE in kinds
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        message = next(
            event for event in adapted.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "cursor prompt"
        assert message.summary is None

    def test_attribution_only_store_fails_closed(self, tmp_path):
        db = tmp_path / "attribution.db"
        cursor_fixtures.make_cursor_db(db, attribution_only=True)
        artifact, root = artifacts.captured_sqlite(
            db, tmp_path,
            allowed_tables=cursor_fixtures.ATTRIBUTION_TABLES,
            allowed_columns=cursor_fixtures.ATTRIBUTION_COLUMNS,
            byte_limit=1_000_000, count_limit=2,
        )
        assert registry.detect_family("cursor", artifact, artifact_root=root) is False
        result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)
        assert result.events == ()
        assert any("not supported" in w for w in result.warnings)


def test_cursor_jsonl_extracts_text_blocks_tools_and_turn_error(tmp_path):
    rows = [
        {
            "role": "user",
            "message": {
                "content": [{"type": "text", "text": "fixture-cursor-user"}],
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "fixture-cursor-assistant"},
                    {
                        "type": "tool_use",
                        "name": "read_file",
                        "input": {"path": "fixture-cursor-tool-input"},
                    },
                ],
            },
        },
        {"type": "turn_ended", "error": "fixture-cursor-turn-error"},
    ]
    src = cursor_fixtures.write_cursor_transcript(tmp_path / "source", rows)
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture",
        relative_path=cursor_fixtures.JSONL_RELATIVE_PATH,
        byte_limit=1_000_000, count_limit=1,
    )
    result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)

    user = next(event for event in result.events if event.kind is EventKind.USER_MESSAGE)
    assistant = next(
        event for event in result.events if event.kind is EventKind.ASSISTANT_MESSAGE
    )
    assert user.content == "fixture-cursor-user"
    assert assistant.content == "fixture-cursor-assistant"
    assert not str(user.content).startswith("[")
    assert "tool_use" not in (assistant.content or "")

    tool_events = [
        event for event in result.events
        if event.kind in (EventKind.TOOL_CALL, EventKind.TOOL_RESULT)
    ]
    assert tool_events, "tool_use block was not emitted as a tool event"
    assert any(
        "fixture-cursor-tool-input" in (event.content or "") for event in tool_events
    )

    turns = [event for event in result.events if event.kind is EventKind.TURN_BOUNDARY]
    assert any(
        "fixture-cursor-turn-error" in artifacts.record_text(event) for event in turns
    ), "turn_ended error was not stored on the turn event"


def test_cursor_empty_transcript_no_ghost_session(tmp_path):
    # 无 message 行（仅 unknown / turn 边界等）的 transcript 不应产生会话。
    rows = [
        {"type": "turn_ended", "timestamp": "2026-07-01T10:00:00Z"},
        {"role": "system", "content": "note"},
    ]
    src = cursor_fixtures.write_cursor_transcript(
        tmp_path / "source", rows, relative_path="thread.jsonl"
    )
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture", relative_path="thread.jsonl",
        byte_limit=1_000_000, count_limit=1,
    )
    result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)
    assert result.sessions == ()


def _make_multi_thread_cursor_db(path) -> None:
    """两个 thread 的 v1 schema 库：messages 无 thread 列，按时间窗归属。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, created_at TEXT);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO threads VALUES ('t1', 'thread one', '2026-07-01T10:00:00Z')"
        )
        con.execute(
            "INSERT INTO threads VALUES ('t2', 'thread two', '2026-07-01T11:00:00Z')"
        )
        con.execute(
            "INSERT INTO messages VALUES ('m1', 'user', 'multi-t1-a', '2026-07-01T10:00:01Z')"
        )
        con.execute(
            "INSERT INTO messages VALUES ('m2', 'assistant', 'multi-t1-b', '2026-07-01T10:00:02Z')"
        )
        con.execute(
            "INSERT INTO messages VALUES ('m3', 'user', 'multi-t2-a', '2026-07-01T11:00:01Z')"
        )
        con.execute(
            "INSERT INTO messages VALUES ('m4', 'assistant', 'multi-t2-b', '2026-07-01T11:00:02Z')"
        )
        con.commit()
    finally:
        con.close()


def test_cursor_multi_thread_sqlite_one_session_per_thread(tmp_path):
    # P0-2 回归：多 thread 库必须每个 thread 一个独立会话，消息各归各，
    # 否则 AdaptationResult.__post_init__ 撞 duplicate session id。
    db = tmp_path / "cursor-multi.db"
    _make_multi_thread_cursor_db(db)
    artifact, root = artifacts.captured_sqlite(
        db, tmp_path,
        allowed_tables=cursor_fixtures.SQLITE_TABLES,
        allowed_columns=cursor_fixtures.SQLITE_COLUMNS,
        byte_limit=1_000_000, count_limit=4,
    )
    result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)

    assert len(result.sessions) == 2
    assert len({s.session_id for s in result.sessions}) == 2

    by_native = {s.native_session_id: s for s in result.sessions}
    assert set(by_native) == {"t1", "t2"}

    per_session: dict[str, list] = {}
    for event in result.events:
        if event.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE):
            per_session.setdefault(event.session_id, []).append(event.content)
    assert sorted(per_session[by_native["t1"].session_id]) == ["multi-t1-a", "multi-t1-b"]
    assert sorted(per_session[by_native["t2"].session_id]) == ["multi-t2-a", "multi-t2-b"]
    # 没有任何消息被算到两个会话头上。
    all_message_events = [e for e in result.events if e.kind in (
        EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)]
    assert len(all_message_events) == 4

    # started_at / ended_at 按本 thread 自己的消息计算，不共享全局末条时间戳。
    assert by_native["t1"].started_at == "2026-07-01T10:00:00Z"
    assert by_native["t1"].ended_at == "2026-07-01T10:00:02Z"
    assert by_native["t2"].started_at == "2026-07-01T11:00:00Z"
    assert by_native["t2"].ended_at == "2026-07-01T11:00:02Z"


def test_cursor_single_thread_sqlite_session_id_shape_stable(tmp_path):
    # 兼容性契约：单 thread 库的 session id 仍是 probe 版本派生的常量，
    # 不因 per-thread id 规则而无谓漂移。
    db = tmp_path / "cursor-single.db"
    cursor_fixtures.make_cursor_db(db)
    artifact, root = artifacts.captured_sqlite(
        db, tmp_path,
        allowed_tables=cursor_fixtures.SQLITE_TABLES,
        allowed_columns=cursor_fixtures.SQLITE_COLUMNS,
        byte_limit=1_000_000, count_limit=4,
    )
    result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)

    assert len(result.sessions) == 1
    expected = make_event_id(
        "cursor", artifact.artifact_id, "2", None,
        kind=EventKind.SESSION_LIFECYCLE, native_locator="probe:v1",
    )
    assert result.sessions[0].session_id == expected
    assert all(e.session_id == expected for e in result.events)


# --------------------------------------------------------------- P1-7 回归

_UUID_STEM = "123e4567-e89b-12d3-a456-426614174000"


def _adapt_cursor_rows(tmp_path, rows, *, relative_path):
    src = cursor_fixtures.write_cursor_transcript(tmp_path / "source", rows, relative_path=relative_path)
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture", relative_path=relative_path,
        byte_limit=1_000_000, count_limit=1,
    )
    return registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)


def test_cursor_jsonl_uuid_stem_keeps_native_session_key(tmp_path):
    # 导出文件名是 uuid 时，词干仍是原生会话键。
    relative = f"projects/fixture/agent-transcripts/{_UUID_STEM}/{_UUID_STEM}.jsonl"
    result = _adapt_cursor_rows(
        tmp_path,
        [{"role": "user", "message": {"content": "fixture-cursor-uuid-stem"}}],
        relative_path=relative,
    )
    assert len(result.sessions) == 1
    assert result.sessions[0].native_session_id == _UUID_STEM


def test_cursor_jsonl_non_uuid_stem_uses_deterministic_path_key(tmp_path):
    # P1-7 回归：``transcript copy`` 这类非 uuid 词干不得成为伪会话键；
    # 改用确定性全路径键，不同文件不再共享同一个伪会话。
    relative = "projects/fixture/agent-transcripts/abc/transcript copy.jsonl"
    result = _adapt_cursor_rows(
        tmp_path,
        [{"role": "user", "message": {"content": "fixture-cursor-renamed"}}],
        relative_path=relative,
    )
    assert len(result.sessions) == 1
    assert result.sessions[0].native_session_id == "cursor-path:" + relative
    assert result.sessions[0].native_session_id != "transcript copy"


def test_cursor_jsonl_timestamps_normalized_to_utc_z(tmp_path):
    # P2 回归：jsonl timestamp（ISO 带时区偏移 / epoch 毫秒）统一归一化。
    result = _adapt_cursor_rows(
        tmp_path,
        [
            {"role": "user", "message": {"content": "fixture-cursor-ts-a"},
             "timestamp": "2026-07-01T18:00:00+08:00"},
            {"role": "assistant", "message": {"content": "fixture-cursor-ts-b"},
             "timestamp": 1782900000123},
        ],
        relative_path=cursor_fixtures.JSONL_RELATIVE_PATH,
    )
    session = result.sessions[0]
    assert session.started_at == "2026-07-01T10:00:00Z"
    assert session.ended_at == "2026-07-01T10:00:00.123Z"


def test_cursor_jsonl_tool_result_blocks_surface_as_tool_result_events(tmp_path):
    # P2 回归：assistant content 里的 tool_result 块必须留下 TOOL_RESULT
    # 事件，结果正文全量进 content（不静默丢弃、不设上限）。
    long_payload = "fixture-cursor-tool-result-" + "x" * 3_000
    result = _adapt_cursor_rows(
        tmp_path,
        [
            {"role": "user", "message": {"content": "fixture-cursor-tool-result-prompt"}},
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": [
                            {"type": "text", "text": long_payload},
                        ]},
                    ],
                },
            },
        ],
        relative_path=cursor_fixtures.JSONL_RELATIVE_PATH,
    )
    tool_results = [e for e in result.events if e.kind is EventKind.TOOL_RESULT]
    assert tool_results, "tool_result block was silently dropped"
    assert tool_results[0].content == long_payload


def test_cursor_jsonl_title_skips_placeholder_first_message(tmp_path):
    # P2 回归：占位首条用户消息（New Chat 等）不得成为会话标题。
    result = _adapt_cursor_rows(
        tmp_path,
        [
            {"role": "user", "message": {"content": "New Chat"}},
            {"role": "user", "message": {"content": "fixture-cursor-real-question"}},
        ],
        relative_path=cursor_fixtures.JSONL_RELATIVE_PATH,
    )
    assert result.sessions[0].title == "fixture-cursor-real-question"


def test_cursor_jsonl_project_cwd_restores_escaped_windows_path(tmp_path):
    # P2 回归：projects/<escaped-project>/agent-transcripts/... 里被转义成
    # 连接号的项目目录要还原成 Windows 路径，而不是拿会话 id 目录充数。
    result = _adapt_cursor_rows(
        tmp_path,
        [{"role": "user", "message": {"content": "fixture-cursor-cwd"}}],
        relative_path="projects/c-Users-li-Desktop-fixtureproj/agent-transcripts/abc/abc.jsonl",
    )
    assert result.sessions[0].cwd == "C:\\Users\\li\\Desktop\\fixtureproj"


def test_cursor_sqlite_timestamps_normalized_to_utc_z(tmp_path):
    # P2 回归：sqlite created_at（epoch 毫秒）全走 normalize_timestamp。
    db = tmp_path / "cursor-millis.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(
            """
            CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, created_at TEXT);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at TEXT
            );
            """
        )
        con.execute("INSERT INTO threads VALUES ('t1', 'millis', 1782900000000)")
        con.execute("INSERT INTO messages VALUES ('m1', 'user', 'millis-a', 1782900000123)")
        con.commit()
    finally:
        con.close()
    artifact, root = artifacts.captured_sqlite(
        db, tmp_path,
        allowed_tables=cursor_fixtures.SQLITE_TABLES,
        allowed_columns=cursor_fixtures.SQLITE_COLUMNS,
        byte_limit=1_000_000, count_limit=4,
    )
    result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)
    session = result.sessions[0]
    assert session.started_at == "2026-07-01T10:00:00Z"
    assert session.ended_at == "2026-07-01T10:00:00.123Z"
    lifecycle = next(e for e in result.events if e.kind is EventKind.SESSION_LIFECYCLE)
    assert lifecycle.occurred_at == "2026-07-01T10:00:00Z"
    message = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
    assert message.occurred_at == "2026-07-01T10:00:00.123Z"


def test_cursor_sqlite_no_id_message_locator_is_content_deterministic(tmp_path):
    # P2 回归：无 id 列的消息 locator 不用 len(events)（随事件发射漂移），
    # 改为内容确定性哈希——重复行不撞 event id，重复适配身份稳定。
    db = tmp_path / "cursor-no-id.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(
            """
            CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, created_at TEXT);
            CREATE TABLE messages (role TEXT, content TEXT, created_at TEXT);
            """
        )
        con.execute("INSERT INTO threads VALUES ('t1', 'no-id', '2026-07-01T10:00:00Z')")
        con.execute("INSERT INTO messages VALUES ('user', 'no-id-dup', '2026-07-01T10:00:01Z')")
        con.execute("INSERT INTO messages VALUES ('user', 'no-id-dup', '2026-07-01T10:00:01Z')")
        con.commit()
    finally:
        con.close()
    artifact, root = artifacts.captured_sqlite(
        db, tmp_path,
        allowed_tables=cursor_fixtures.SQLITE_TABLES,
        # 这张夹具库的 messages 表故意没有 id 列：声明与实际 schema 一致。
        allowed_columns={
            "threads": ("id", "title", "created_at"),
            "messages": ("role", "content", "created_at"),
        },
        byte_limit=1_000_000, count_limit=4,
    )
    result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)
    messages = [e for e in result.events if e.kind is EventKind.USER_MESSAGE]
    assert len(messages) == 2
    assert len({e.event_id for e in messages}) == 2, "duplicate rows must not collide"
    assert all("#message:sha256:" in e.provenance.native_locator for e in messages)
    re_result = registry.adapt_for("cursor", artifacts.single(artifact), artifact_root=root)
    re_messages = [e for e in re_result.events if e.kind is EventKind.USER_MESSAGE]
    assert [e.event_id for e in re_messages] == [e.event_id for e in messages]
