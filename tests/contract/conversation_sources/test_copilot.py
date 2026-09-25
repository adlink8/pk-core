"""copilot 家族适配器契约：dotted 事件流、reasoning、compaction 与控制记录。

生产代码只经 ``registry`` 的族级 seam 消费适配器（``adapt_for`` /
``capability_for`` / ``detect_family``），没有任何调用方直接摸家族模块的函数。
所以契约断言也走这条路径，而不是直接调 ``copilot.adapt(...)``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind

from tests.contract.conversation_sources.support import artifacts, copilot


def _adapt_custom(tmp_path: Path, records: list[dict], *, relative_path: str):
    """把一份自定义 JSONL trace 写到源目录，经 capture seam 抓取并适配。"""
    src = tmp_path / "source" / "trace.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(artifacts.jsonl(records), encoding="utf-8")
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture", relative_path=relative_path,
        byte_limit=1_000_000, count_limit=1,
    )
    return registry.adapt_for(
        "copilot", artifacts.single(artifact), artifact_root=root
    )


def test_copilot_events_keep_message_reasoning_compaction_and_control_text(tmp_path):
    artifact, root = copilot.captured_trace(tmp_path)
    result = registry.adapt_for(
        "copilot", artifacts.single(artifact), artifact_root=root
    )

    user = artifacts.event_with(result, EventKind.USER_MESSAGE)
    assistant = artifacts.event_with(result, EventKind.ASSISTANT_MESSAGE)
    assert user.content == copilot.USER_TEXT
    assert copilot.ASSISTANT_TEXT in (assistant.content or "")
    reasoning_kept = any(
        copilot.REASONING_TEXT in (event.content or "")
        for event in artifacts.events_of(result, EventKind.REASONING)
    ) or copilot.REASONING_TEXT in (assistant.content or "")
    assert reasoning_kept, "assistant.message reasoningText was dropped"

    compactions = artifacts.events_of(result, EventKind.COMPACTION_SUMMARY)
    assert any(
        copilot.COMPACTION_TEXT in ((event.content or "") + (event.summary or ""))
        for event in compactions
    ), "session.compaction summaryContent did not reach a compaction event"

    for body in (
        copilot.ABORT_REASON,
        copilot.NOTIFICATION_TEXT,
        copilot.SYSTEM_TEXT,
        copilot.ERROR_TEXT,
    ):
        assert body in artifacts.event_text(result), body
    assert artifacts.any_reason(result, "session.truncation"), (
        "session.truncation without a body has no reason naming the record type"
    )
    assert not artifacts.bare_unknowns(result)


def test_copilot_tool_payloads_keep_full_text(tmp_path):
    """工具入参/产出是正文，必须逐字保留，不再被内容上限裁断。"""
    artifact, root = copilot.captured_trace_with_big_tools(tmp_path)
    result = registry.adapt_for(
        "copilot", artifacts.single(artifact), artifact_root=root
    )

    calls = artifacts.events_of(result, EventKind.TOOL_CALL)
    assert any(event.content == copilot.BIG_TOOL_INPUT for event in calls), (
        "tool input was cut; full arguments must land in content"
    )
    results = artifacts.events_of(result, EventKind.TOOL_RESULT)
    assert any(event.content == copilot.BIG_TOOL_OUTPUT for event in results), (
        "tool output was cut; full result must land in content"
    )
    assert not artifacts.any_reason(result, "truncated"), (
        "content cap removed: no truncation disposition may be produced"
    )


# 探测器是全函数：畸形文件必须判「不是我」，不得把解析异常抛给发现层。
# 非 UTF-8 字节：写了一半/别的编码落地的轨迹（UnicodeDecodeError ⊂ ValueError）。
# 非法 JSON：~/.copilot 下别的家族的合法配置（如 config.json）不是 JSON。
# 期望值是独立字面量 False，不由被测代码同款算法重算。
MALFORMED_COPILOT_BYTES = (
    ("session.jsonl", b'\xff\xfe\x00{"type":"user"}'),
    ("session.json", b'\xff\xfe\x00{"requests": []}'),
    ("config.json", "{ this is not json"),
    ("config.json", b"\x00\x01\x02\x03"),
)


@pytest.mark.parametrize("name,raw", MALFORMED_COPILOT_BYTES)
def test_copilot_detector_rejects_malformed_bytes_without_raising(
    tmp_path, name, raw
):
    artifact, root = artifacts.probe_file(tmp_path, name, raw)
    assert registry.detect_family("copilot", artifact, artifact_root=root) is False


# --------------------------------------------------------- 批次 B 回归补测

# 独立字面量期望值：1745308980000 ms -> 2025-04-22T08:03:00Z；
# 1745308981000 ms（数字串）-> 2025-04-22T08:03:01Z；
# 1745308979000 ms -> 2025-04-22T08:02:59Z。
def test_copilot_timestamps_normalize_to_utc_z(tmp_path):
    result = _adapt_custom(tmp_path, [
        {"type": "session.start", "id": "ss", "timestamp": 1745308979000,
         "data": {"sessionId": "sess-ts"}},
        {"type": "user.message", "id": "u1", "timestamp": 1745308980000,
         "data": {"sessionId": "sess-ts", "content": copilot.USER_TEXT}},
        {"type": "assistant.message", "id": "a1", "timestamp": "1745308981000",
         "data": {"sessionId": "sess-ts", "content": copilot.ASSISTANT_TEXT,
                  "reasoningText": copilot.REASONING_TEXT}},
    ], relative_path=copilot.RELATIVE_PATH)

    user = artifacts.event_with(result, EventKind.USER_MESSAGE)
    assistant = artifacts.event_with(result, EventKind.ASSISTANT_MESSAGE)
    reasoning = artifacts.event_with(result, EventKind.REASONING)
    assert user.occurred_at == "2025-04-22T08:03:00Z"
    assert assistant.occurred_at == "2025-04-22T08:03:01Z"
    assert reasoning.occurred_at == "2025-04-22T08:03:01Z"
    session = result.sessions[0]
    assert session.started_at == "2025-04-22T08:02:59Z"


def test_copilot_ended_at_takes_last_shutdown(tmp_path):
    """docstring 承诺 last：多段会话取最后一个 shutdown，不许命中首个即返回。"""
    result = _adapt_custom(tmp_path, [
        {"type": "session.start", "id": "ss", "timestamp": "2026-09-01T00:00:00Z",
         "data": {"sessionId": "sess-fixture"}},
        {"type": "session.shutdown", "id": "sd1", "timestamp": "2026-09-01T00:05:00Z"},
        {"type": "user.message", "id": "u9", "timestamp": "2026-09-01T00:06:00Z",
         "data": {"sessionId": "sess-fixture", "content": "after restart"}},
        {"type": "session.shutdown", "id": "sd2", "timestamp": "2026-09-01T00:10:00Z"},
    ], relative_path=copilot.RELATIVE_PATH)

    assert result.sessions[0].ended_at == "2026-09-01T00:10:00Z"


def test_copilot_non_uuid_stem_falls_back_to_deterministic_path_key(tmp_path):
    """词干不是 uuid（如 events.jsonl）时不得用词干当会话键；uuid 词干保留。"""
    records = [
        {"type": "user.message", "id": "u1",
         "data": {"content": copilot.USER_TEXT}},
    ]
    relative = "logs/sessions/events.jsonl"
    result = _adapt_custom(tmp_path / "a", records, relative_path=relative)
    rerun = _adapt_custom(tmp_path / "b", records, relative_path=relative)

    session = result.sessions[0]
    assert session.native_session_id != "events"
    assert session.native_session_id.startswith("path:")
    # 同一 relative_path 在不同捕获目录重抓，键不漂移。
    assert rerun.sessions[0].native_session_id == session.native_session_id

    uuid_relative = "logs/sessions/123e4567-e89b-42d3-a456-426614174000.jsonl"
    uuid_result = _adapt_custom(
        tmp_path / "c", records, relative_path=uuid_relative,
    )
    assert (uuid_result.sessions[0].native_session_id
            == "123e4567-e89b-42d3-a456-426614174000")


def test_copilot_duplicate_tool_ids_keep_first_pair_and_warn(tmp_path):
    start_first = {
        "type": "tool.execution_start", "id": "t1",
        "timestamp": "2026-09-01T00:00:00Z",
        "data": {"sessionId": "sess-fixture", "toolId": "tool-1",
                 "toolName": "fixture-tool", "arguments": "first"},
    }
    start_dup = dict(start_first, id="t1-again",
                     timestamp="2026-09-01T00:00:01Z")
    start_dup["data"] = dict(start_first["data"], arguments="second")
    complete = {
        "type": "tool.execution_complete", "id": "t1-done",
        "timestamp": "2026-09-01T00:00:02Z",
        "data": {"sessionId": "sess-fixture", "toolId": "tool-1",
                 "result": "ok"},
    }
    result = _adapt_custom(
        tmp_path, [start_first, start_dup, complete],
        relative_path=copilot.RELATIVE_PATH,
    )

    # 两条 start 都是独立原生记录、事件都在；配对必须用首对而不是被覆盖后的。
    assert len(result.relations) == 1
    events_by_id = {event.event_id: event for event in result.events}
    assert events_by_id[result.relations[0].source_event_id].content == "first"
    assert any("duplicate tool id" in warning for warning in result.warnings)


def test_copilot_json_export_keeps_string_parts_and_counts_dropped(tmp_path):
    """.json 导出：字符串 value part 保留；非文本块显式计数告警，不许静默丢。"""
    doc = {
        "sessionId": "sess-json",
        "creationDate": 1745308979000,
        "requests": [
            {
                "requestId": "r1",
                "timestamp": "2026-09-01T00:00:00Z",
                "message": {"text": copilot.USER_TEXT},
                "responseId": "resp-1",
                "response": [
                    {"value": copilot.ASSISTANT_TEXT, "kind": "markdown"},
                    {"kind": "progress"},
                    42,
                ],
            },
        ],
    }
    src = tmp_path / "source" / "export.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture", relative_path="export/session.json",
        byte_limit=1_000_000, count_limit=1,
    )
    result = registry.adapt_for(
        "copilot", artifacts.single(artifact), artifact_root=root
    )

    assistant = artifacts.event_with(result, EventKind.ASSISTANT_MESSAGE)
    assert assistant.content == copilot.ASSISTANT_TEXT
    assert any(
        "2 non-text response part(s)" in warning for warning in result.warnings
    ), result.warnings
    # creationDate 毫秒纪元同样归一。
    assert result.sessions[0].started_at == "2025-04-22T08:02:59Z"


def test_copilot_jsonl_parse_is_single_pass(tmp_path, monkeypatch):
    """JSONL 解析只读一遍文件：消除第二次全文件 read_text。"""
    records = copilot.trace_records()
    src = tmp_path / "source" / "events.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        artifacts.jsonl(records) + "{ this line is corrupt\n", encoding="utf-8",
    )
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture", relative_path=copilot.RELATIVE_PATH,
        byte_limit=1_000_000, count_limit=1,
    )

    def _no_read_text(self, *args, **kwargs):
        raise AssertionError("JSONL 适配路径不得二次 read_text 全文件")

    monkeypatch.setattr(Path, "read_text", _no_read_text)
    result = registry.adapt_for(
        "copilot", artifacts.single(artifact), artifact_root=root
    )
    assert any(
        warning.startswith("1 malformed/native-corrupt record(s) skipped")
        for warning in result.warnings
    ), result.warnings
