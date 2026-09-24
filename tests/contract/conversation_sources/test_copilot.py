"""copilot 家族适配器契约：dotted 事件流、reasoning、compaction 与控制记录。

生产代码只经 ``registry`` 的族级 seam 消费适配器（``adapt_for`` /
``capability_for`` / ``detect_family``），没有任何调用方直接摸家族模块的函数。
所以契约断言也走这条路径，而不是直接调 ``copilot.adapt(...)``。
"""

from __future__ import annotations

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind

from tests.contract.conversation_sources.support import artifacts, copilot


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
