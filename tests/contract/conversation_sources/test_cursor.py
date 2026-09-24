"""cursor 家族（生产模块 ``cursor``，ADAPTER_VERSION 1.0.0）适配契约。

公开 seam 是 registry（``adapt_for`` / ``detect_family``）——生产代码只经它
调用家族模块，所以断言也走同一入口，不直接摸 ``cursor.adapt``。夹具来自
``support/cursor.py``，正文全是合成句子。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind
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
