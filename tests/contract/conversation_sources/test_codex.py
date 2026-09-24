"""codex 模块契约：原生记录必须变成带正文或带原因的事件，不能静默丢掉。

一个生产模块一个测试文件。断言一律经 registry 族级 seam
（``registry.adapt_for``）—— 不直接调用 ``codex.adapt``，所以本文件不需要
也没有 import 任何别的测试文件的私有夹具。

夹具与常量在 ``support/codex.py``：手写最小 JSONL，合成句子，不读权威库。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import codex as fixtures


def test_encrypted_reasoning_keeps_summary_and_states_why_plaintext_is_missing(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    reasoning = artifacts.event_with(result, EventKind.REASONING)
    assert not reasoning.content
    assert reasoning.summary == fixtures.ENC_REASONING_SUMMARY
    encrypted = [
        record for record in reasoning.field_dispositions
        if record.field_name == "encrypted_content"
    ]
    assert encrypted
    assert encrypted[0].reason == fixtures.ENCRYPTED_REASON
    assert reasoning.kind is not EventKind.UNKNOWN_NATIVE


def test_compacted_replacement_history_becomes_role_messages(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    user = [
        event for event in result.events
        if event.kind is EventKind.USER_MESSAGE
        and event.content == fixtures.COMPACTED_USER_TEXT
    ]
    assistant = [
        event for event in result.events
        if event.kind is EventKind.ASSISTANT_MESSAGE
        and event.content == fixtures.COMPACTED_ASSISTANT_TEXT
    ]
    assert user and assistant
    assert all(event.kind is not EventKind.UNKNOWN_NATIVE for event in user + assistant)


def test_image_generation_call_puts_revised_prompt_in_content(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    image = next(
        event for event in result.events
        if event.content == fixtures.IMAGE_PROMPT
    )
    assert image.kind is not EventKind.UNKNOWN_NATIVE


def test_thread_goal_updated_keeps_objective_and_native_type(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    goal = next(
        event for event in result.events
        if fixtures.GOAL_OBJECTIVE in (
            (event.content or "") + (event.summary or "") + artifacts.reasons(event)
        )
    )
    assert goal.kind is not EventKind.UNKNOWN_NATIVE
    assert "thread_goal_updated" in ((goal.summary or "") + artifacts.reasons(goal))


def test_token_count_without_info_is_usage_or_explains_missing_fields(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    token = next(
        event for event in result.events
        if event.provenance.native_locator.endswith(f"#L{fixtures.TOKEN_COUNT_LINE}")
        or (
            event.kind is EventKind.USAGE
            and "input_tokens=4" not in (event.summary or "")
            and f"#L{fixtures.TOKEN_USAGE_LINE}" not in event.provenance.native_locator
        )
    )
    assert token.kind is not EventKind.UNKNOWN_NATIVE
    assert token.kind is EventKind.USAGE
    assert fixtures.TOKEN_COUNT_REASON in artifacts.reasons(token)


def test_token_usage_record_becomes_usage(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    usage = next(
        event for event in result.events
        if "input_tokens=4" in (event.summary or "")
    )
    assert usage.kind is EventKind.USAGE
    assert "output_tokens=1" in (usage.summary or "")
    assert usage.kind is not EventKind.UNKNOWN_NATIVE


def test_inter_agent_metadata_without_body_explains_itself(tmp_path):
    _, result = fixtures.adapt_coverage(tmp_path)
    inter = next(
        event for event in result.events
        if event.provenance.native_locator.endswith(f"#L{fixtures.INTER_AGENT_LINE}")
        or "inter_agent_communication_metadata" in (
            (event.summary or "") + artifacts.reasons(event)
        )
    )
    assert inter.kind is not EventKind.UNKNOWN_NATIVE
    assert not inter.content
    assert fixtures.INTER_AGENT_REASON in artifacts.reasons(inter)


def test_every_input_record_produces_an_event(tmp_path):
    records, result = fixtures.adapt_coverage(tmp_path)
    assert fixtures.covered_lines(result) == set(range(1, len(records) + 1))
    assert all(event.kind is not EventKind.UNKNOWN_NATIVE for event in result.events)


def test_oversized_bodies_are_kept_in_full(tmp_path):
    """工具入参/产出、消息、compaction、reasoning、stderr 都是正文，不设上限。"""
    _records, result = fixtures.adapt_oversize(tmp_path)
    contents = {event.content for event in result.events}

    for label, expected in (
        ("tool input", fixtures.BIG_INPUT),
        ("tool output", fixtures.BIG_OUTPUT),
        ("agent_message item", fixtures.BIG_ITEM_MESSAGE),
        ("agent_message event", fixtures.BIG_EVENT_MESSAGE),
        ("compacted message", fixtures.BIG_COMPACTED),
        ("reasoning", fixtures.BIG_REASONING),
        ("stderr", fixtures.BIG_STDERR),
    ):
        assert expected in contents, f"{label} was cut; full text must land in content"
    assert not artifacts.any_reason(result, "truncated"), (
        "content cap removed: no truncation disposition may be produced"
    )


# 探测器是全函数：畸形文件判「不是我」，不得把解析异常抛给发现层（会被记成
# probe_error）。非 UTF-8 字节 = 写了一半/别的编码落地的 rollout。
# 期望值是独立字面量 False。
# 探测器是全函数：畸形文件判「不是我」，不得把解析异常抛给发现层。
# 非 UTF-8 字节（写了一半/别的编码落地的 rollout）必须不抛。
# 期望值是独立字面量 False。
NON_UTF8_LINE = bytes([0xFF, 0xFE, 0x00]) + b'{"type":"session_meta"}'
UTF16_LINE = '{"type":"session_meta"}'.encode("utf-16")


@pytest.mark.parametrize(
    "name,raw",
    (("rollout-2026.jsonl", NON_UTF8_LINE), ("rollout-2026.jsonl", UTF16_LINE)),
)
def test_codex_detector_rejects_malformed_bytes_without_raising(tmp_path, name, raw):
    artifact, root = artifacts.probe_file(tmp_path, name, raw)
    assert registry.detect_family("codex", artifact, artifact_root=root) is False
