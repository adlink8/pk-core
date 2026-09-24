"""claude_qoder 模块契约：claude 与 qoder 共用一份代码、一个 ADAPTER_VERSION。

一个生产模块一个测试文件 —— 两个家族必须同住这里，不能按客户端拆开。
断言一律经 registry 族级 seam（``registry.adapt_for`` /
``registry.detect_family``），不直接调用 ``claude_qoder.adapt(family, …)``。

这里承载的是**家族特有**的记录形状（claude 侧链子代理；qoder 的
config / lifecycle 类记录与 last-prompt）。两家**共享**的 message 记录形状
属跨模块 wire 不变量，由 wire 格式文件用 parametrize 统一表达，避免重复用例。

夹具与常量在 ``support/claude_qoder.py``：合成句子，不读权威库。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import claude_qoder as fixtures


# --------------------------------------------------------------- claude 侧链


def test_subagent_only_file_keeps_text_and_drops_unexplained_boundary_session(tmp_path):
    """每行都有 agentId 时，正文留在事件里，不再造无原因的边界空会话。"""
    _, _, result = fixtures.adapt_family(
        "claude", tmp_path, "agent-fixture.jsonl", fixtures.subagent_records(),
    )

    assert artifacts.has_event(
        result, EventKind.USER_MESSAGE, fixtures.SUBAGENT_USER_TEXT
    )
    assert artifacts.has_event(
        result, EventKind.ASSISTANT_MESSAGE, fixtures.SUBAGENT_ASSISTANT_TEXT
    )
    grouped = fixtures.events_by_session(result)
    for session in result.sessions:
        events = grouped.get(session.session_id, [])
        only_boundary = bool(events) and all(
            event.kind is EventKind.SUBAGENT_BOUNDARY for event in events
        )
        if not events or only_boundary:
            assert events, "subagent file must not add an event-less session"
            for event in events:
                note = fixtures.explained(event)
                assert "占位" in note or "placeholder" in note.lower()
                assert fixtures.SUBAGENT_AGENT_ID in note


# ------------------------------------------------------ qoder 检测与记录覆盖


def test_qoder_detects_message_jsonl_without_compact_summary(tmp_path):
    """没有 isCompactSummary、但有 message 的 jsonl 必须能被 detect 接受。"""
    artifact, root, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-no-compact.jsonl", fixtures.no_compact_records(),
    )
    assert registry.detect_family("qoder", artifact, artifact_root=root) is True
    assert artifacts.has_event(
        result, EventKind.USER_MESSAGE, fixtures.NO_COMPACT_USER_TEXT
    )
    assert artifacts.has_event(
        result, EventKind.ASSISTANT_MESSAGE, fixtures.NO_COMPACT_ASSISTANT_TEXT
    )


def test_unmapped_native_types_keep_text_or_name_the_type(tmp_path):
    """未映射类型不能是没有原因的 unknown_native；有文本进 content。"""
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "unmapped.jsonl", fixtures.unmapped_records(),
    )

    for event in result.events:
        if event.kind is not EventKind.UNKNOWN_NATIVE:
            continue
        named = artifacts.reasons(event)
        assert any(
            type_name in named for type_name in fixtures.UNMAPPED_TYPE_NAMES
        ), named or "<empty>"
    for type_name in ("active-leaf", "runtime-config", "worktree-state"):
        assert any(
            type_name in artifacts.reasons(event) for event in result.events
        ), type_name
    assert any("image" in artifacts.reasons(event) for event in result.events)
    assert any(
        fixtures.UNMAPPED_DIRECTORY in (event.content or "") for event in result.events
    )
    assert any(
        fixtures.UNMAPPED_CAPTION in (event.content or "") for event in result.events
    )
    assert artifacts.has_event(
        result, EventKind.USER_MESSAGE, fixtures.UNMAPPED_USER_TEXT
    )


def test_last_prompt_text_is_kept_without_unexplained_lifecycle_session(tmp_path):
    """last-prompt 没有 message 时，lastPrompt 进事件，不能只剩一条没解释的 lifecycle。"""
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "agent-last-prompt.jsonl", fixtures.last_prompt_records(),
    )

    assert any(
        fixtures.LAST_PROMPT_TEXT in (event.content or "") for event in result.events
    )
    assert artifacts.has_event(
        result, EventKind.ASSISTANT_MESSAGE, fixtures.LAST_PROMPT_AGENT_TEXT
    )
    grouped = fixtures.events_by_session(result)
    for session in result.sessions:
        events = grouped.get(session.session_id, [])
        only_lifecycle = bool(events) and all(
            event.kind is EventKind.SESSION_LIFECYCLE for event in events
        )
        if not events or only_lifecycle:
            assert events, "last-prompt file must not add an event-less session"
            for event in events:
                assert fixtures.explained(event).strip(), event.kind


# --------------------------------------------- qoder 时间戳归一化（Fix 2）


def test_mixed_epoch_ms_and_iso_all_normalized(tmp_path):
    """epoch 毫秒整数与 ISO 串混排时，所有时间值都归一化成 UTC ISO Z 串。"""
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-session.jsonl", fixtures.mixed_timestamp_records(),
    )
    session = result.sessions[0]
    # 所有流向时间字段的值都是规范化 UTC ISO Z 串，epoch 毫秒不再裸奔。
    stamps = [event.occurred_at for event in result.events if event.occurred_at]
    stamps += [session.started_at, session.ended_at]
    assert stamps
    for stamp in stamps:
        assert isinstance(stamp, str)
        assert fixtures.ISO_Z.match(stamp), stamp
    assert fixtures.iso_utc(fixtures.QODER_EPOCH_MS // 1000) in stamps
    assert fixtures.QODER_ISO_1 in stamps and fixtures.QODER_ISO_2 in stamps


def test_session_bounds_keep_native_dag_order(tmp_path):
    # started_at = 首条记录时间（epoch 毫秒），ended_at = 末条记录时间（ISO），
    # 取的是原生 DAG 顺序而非日历排序。
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-session.jsonl", fixtures.mixed_timestamp_records(),
    )
    session = result.sessions[0]
    assert session.started_at == fixtures.iso_utc(fixtures.QODER_EPOCH_MS // 1000)
    assert session.ended_at == fixtures.QODER_ISO_3


def test_no_raw_epoch_integer_leaks_into_events(tmp_path):
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-session.jsonl", fixtures.mixed_timestamp_records(),
    )
    for event in result.events:
        assert not isinstance(event.occurred_at, int)


# --------------------------------------------- qoder queue-operation 正文

def test_queue_operation_content_is_kept_in_full(tmp_path):
    """queue-operation 的正文只在顶层 content 上，必须全文进事件 content。

    对这类记录原生没有 message 信封，也没有 text/summary 键；一旦只靠
    有界摘要（256 字符）承载，超出部分就没有任何地方落脚。
    """
    assert len(fixtures.QUEUE_OPERATION_TEXT) > 256, (
        "fixture must exceed the bounded-summary cap for this test to mean anything"
    )
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-queue-operation.jsonl",
        fixtures.queue_operation_records(),
    )

    assert artifacts.has_event(
        result, EventKind.SYSTEM_MESSAGE, fixtures.QUEUE_OPERATION_TEXT
    ), "queue-operation 正文被砍或丢失；全文必须落到 content"
    event = next(
        item for item in result.events
        if item.content == fixtures.QUEUE_OPERATION_TEXT
    )
    # summary 仍是有界导航标签，既有 512 上限不变。
    assert event.summary
    assert len(event.summary) <= 512


def test_short_queue_operation_content_also_lands_in_content(tmp_path):
    """短正文同样走 content，不是只在超长时才被救回来。"""
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-queue-operation-short.jsonl",
        fixtures.queue_operation_records(),
    )

    assert artifacts.has_event(
        result, EventKind.SYSTEM_MESSAGE, fixtures.QUEUE_OPERATION_SHORT_TEXT
    )
    assert artifacts.has_event(
        result, EventKind.ASSISTANT_MESSAGE, fixtures.QUEUE_OPERATION_ASSISTANT_TEXT
    )


# ------------------------------------------------------------ 超限正文

def test_oversized_bodies_are_kept_in_full(tmp_path):
    """附件、工具入参/产出与推理都是正文，不设内容上限。"""
    _, _, result = fixtures.adapt_family(
        "qoder", tmp_path, "qoder-oversize.jsonl", fixtures.oversize_records(),
    )
    contents = [event.content or "" for event in result.events]

    for label, expected in (
        ("attachment", fixtures.BIG_ATTACHMENT),
        ("tool input", fixtures.BIG_TOOL_INPUT),
        ("reasoning", fixtures.BIG_REASONING),
    ):
        assert any(expected in content for content in contents), (
            f"{label} was cut; full text must land in content"
        )
    assert fixtures.BIG_TOOL_OUTPUT in contents, (
        "tool output was cut; full text must land in content"
    )
    assert not artifacts.any_reason(result, "truncated"), (
        "content cap removed: no truncation disposition may be produced"
    )


# ------------------------------------------------- 探测器全函数（两家都要）
# 探测器是全函数：畸形文件判「不是我」，不得把解析异常抛给发现层（会被记成
# probe_error）。非 UTF-8 / UTF-16 字节 = 写了一半或别的编码落地的轨迹。
# 期望值是独立字面量 False。claude 与 qoder 共用同一份 detect，两家都要过。
NON_UTF8_LINE = bytes([0xFF, 0xFE, 0x00]) + b'{"uuid":"u1","parentUuid":null}'
UTF16_LINE = '{"uuid":"u1","parentUuid":null}'.encode("utf-16")


@pytest.mark.parametrize("family", ["claude", "qoder"])
@pytest.mark.parametrize(
    "name,raw",
    [("session.jsonl", NON_UTF8_LINE), ("session.jsonl", UTF16_LINE)],
)
def test_claude_qoder_detector_rejects_malformed_bytes_without_raising(
    tmp_path, family, name, raw
):
    artifact, root = artifacts.probe_file(tmp_path, name, raw)
    assert registry.detect_family(family, artifact, artifact_root=root) is False
