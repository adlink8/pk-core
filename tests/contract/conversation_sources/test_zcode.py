"""zcode 家族适配器契约（模块 ``zcode``，ADAPTER_VERSION 1.7.0）。

registry 是唯一的族级 seam：生产代码只经 ``adapt_for`` / ``detect_family``
消费适配器，所以本文件也只经 registry 调用，不直接摸 ``zcode.adapt`` /
``zcode.detect``。

覆盖两种原生库形态（夹具见 ``support/zcode.py``，全是合成句子）：

* 在线 ``session`` / ``message`` / ``part`` 库 —— 能读到的正文进
  ``content``；读不出正文的记录（如 ``timeline`` 分隔）也必须带 field
  disposition reason 与原生类型名，不许变成静默的裸 unknown。
* allowlist 捕获的 ``conversation_traces`` / ``conversation_parts`` 库 ——
  族名、事件种类、turn 关系、精确正文与凭据不外泄。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import (
    EventKind,
    FieldDisposition,
    RelationKind,
)

# 注意：这里的 ``zcode`` 是夹具构建器（support/zcode.py），不是生产模块。
# 生产 ``zcode`` 适配器只经 registry 调用，所以本文件结构上不可能绕过 seam。
from tests.contract.conversation_sources.support import artifacts, zcode

FAMILY = "zcode"


def _event_by_native(result, native_id: str):
    """按原生 id 找到唯一一条事件。"""
    matched = [
        event for event in result.events
        if event.provenance.native_event_id == native_id
    ]
    assert len(matched) == 1, f"{native_id}: {len(matched)} events"
    return matched[0]


def _visible(event) -> str:
    """一条事件上人眼可见的正文 + 摘要。"""
    return f"{event.content or ''}\n{event.summary or ''}"


# ------------------------------------------------------------- 在线库：时间线

def test_zcode_text_and_timeline_are_not_silent(tmp_path: Path) -> None:
    artifact, root = zcode.live_artifact(tmp_path)
    result = registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)

    user = _event_by_native(result, "part-user")
    assistant = _event_by_native(result, "part-assistant")
    assert user.kind is EventKind.USER_MESSAGE
    assert user.content == "fixture user text"
    assert assistant.kind is EventKind.ASSISTANT_MESSAGE
    assert assistant.content == "fixture assistant text"

    model = _event_by_native(result, "part-model")
    compact = _event_by_native(result, "part-compact")
    goal = _event_by_native(result, "part-goal")
    fork = _event_by_native(result, "part-fork")
    bare = artifacts.bare_unknowns(result)
    for event in (model, compact, goal, fork):
        assert event not in bare

    assert "fixture-model" in _visible(model)
    assert "fixture compact note" in _visible(compact)
    assert "fixture goal note" in _visible(goal)

    assert not (fork.content or "").strip()
    assert not (fork.summary or "").strip()
    fork_reason = artifacts.reasons(fork)
    assert "timeline" in fork_reason
    assert "session_fork" in fork_reason


# ------------------------------------------------- 捕获库：族名 / 关系 / 隐私

class TestZCodeCapturedStore:
    """经 allowlist capture seam 抓下来的 zcode 库。"""

    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("zcode-store")
        artifact, root = zcode.store_artifact(tmp)
        return (
            registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root),
            artifact,
            root,
        )

    def test_detect(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("zcode-store-detect")
        artifact, root = zcode.store_artifact(tmp)
        assert registry.detect_family(FAMILY, artifact, artifact_root=root) is True

    def test_family_and_kinds(self, adapted):
        result, _a, _r = adapted
        assert result.family == FAMILY
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.REASONING in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.COMPACTION_SUMMARY in kinds

    def test_trace_and_turn_preserved(self, adapted):
        result, _a, _r = adapted
        assert len(result.sessions) == 1
        assert result.sessions[0].native_session_id == "tr_1"
        turn_rels = [r for r in result.relations if r.relation_kind is RelationKind.TURN_MEMBERSHIP]
        assert len(turn_rels) >= 1

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        result, _a, _r = adapted
        message = next(
            event for event in result.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "zcode prompt"
        assert message.summary is None

    def test_canary_never_in_events(self, adapted):
        result, _a, _r = adapted
        assert artifacts.CANARY not in artifacts.event_text(result)

    def test_privacy_dispositions_record_exclusions(self, adapted):
        _result, artifact, _r = adapted
        exclusions = [d for d in artifact.privacy_dispositions if d.startswith("excluded_table:")]
        assert any("auth_tokens" in d for d in exclusions)
        assert any("accounts" in d for d in exclusions)


# --------------------------------------------------- 超限正文：内容不设上限

def test_zcode_oversized_bodies_are_kept_in_full(tmp_path: Path) -> None:
    """工具入参/产出、reasoning 与 compaction 正文都是正文，不设内容上限。"""
    artifact, root = zcode.live_oversize_artifact(tmp_path)
    result = registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)

    calls = artifacts.events_of(result, EventKind.TOOL_CALL)
    assert any(event.content == zcode.BIG_TOOL_INPUT for event in calls), (
        "tool input was cut; full arguments must land in content"
    )
    results = artifacts.events_of(result, EventKind.TOOL_RESULT)
    assert any(event.content == zcode.BIG_TOOL_OUTPUT for event in results), (
        "tool output was cut; full result must land in content"
    )
    reasoning = artifacts.events_of(result, EventKind.REASONING)
    assert any(event.content == zcode.BIG_REASONING for event in reasoning), (
        "reasoning was cut; full text must land in content"
    )
    compaction = artifacts.events_of(result, EventKind.COMPACTION_SUMMARY)
    assert any(
        zcode.BIG_COMPACTION in f"{event.content or ''}\n{event.summary or ''}"
        for event in compaction
    ), "compaction body only existed in a bounded summary and was lost"
    assert not artifacts.any_reason(result, "truncated"), (
        "content cap removed: no truncation disposition may be produced"
    )


# --------------------------------------- file 正文：回落原生嵌套槽位（1.7.0）

def test_zcode_file_body_falls_back_to_nested_native_slots(tmp_path: Path) -> None:
    """``file`` part 顶层没有正文，正文只在 ``source.text.value``（其次 preview）。"""
    artifact, root = zcode.live_file_artifact(tmp_path)
    result = registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)

    source_only = _event_by_native(result, "part-file-source")
    assert source_only.kind is EventKind.FILE_CONTEXT
    assert source_only.content == zcode.FILE_SOURCE_TEXT

    both = _event_by_native(result, "part-file-both")
    assert both.kind is EventKind.FILE_CONTEXT
    assert both.content == zcode.FILE_SOURCE_TEXT, (
        "source.text.value must win over metadata.preview.text"
    )

    preview_only = _event_by_native(result, "part-file-preview")
    assert preview_only.kind is EventKind.FILE_CONTEXT
    assert preview_only.content == zcode.FILE_PREVIEW_TEXT


# -------------------------- 诚实为空：原生确实没正文，但必须留下解释（1.7.0）

def test_zcode_honest_empty_native_records_explain_themselves(tmp_path: Path) -> None:
    """读不出正文的三条记录只加解释：不造正文，缺口必须点名原因。"""
    artifact, root = zcode.live_honest_empty_artifact(tmp_path)
    result = registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)

    # 指针指向的那条消息本身就在语料里：正文没有丢，只是不在这一行。
    assert _event_by_native(result, "part-summary-text").content == zcode.SUMMARY_TEXT

    think = _event_by_native(result, "part-think-empty")
    assert think.kind is EventKind.REASONING
    assert not think.content, "empty native text must not become content"
    assert [
        (record.field_name, record.disposition, record.reason)
        for record in think.field_dispositions
    ] == [
        (
            "text",
            FieldDisposition.UNAVAILABLE,
            "data.text is empty or absent and reasoningEncryptedContent "
            "is not stored (null in all rows): native reasoning text is "
            "not recoverable",
        ),
    ]

    compact = _event_by_native(result, "part-compact-meta")
    assert compact.kind is EventKind.COMPACTION_SUMMARY
    assert not compact.content and not compact.summary
    assert [
        (record.field_name, record.disposition, record.reason)
        for record in compact.field_dispositions
    ] == [
        (
            "summaryMessageId",
            FieldDisposition.PRESERVED_BY_REFERENCE,
            "compaction row has no text/content; body is on the message "
            f"data.summaryMessageId={zcode.SUMMARY_POINTER} points at",
        ),
    ]

    attach = _event_by_native(result, "part-attach-text")
    assert attach.kind is EventKind.USER_MESSAGE
    assert not attach.content, "an attachment-only message must not invent a body"
    assert [
        (record.field_name, record.disposition, record.reason)
        for record in attach.field_dispositions
    ] == [
        (
            "text",
            FieldDisposition.UNAVAILABLE,
            "data.text is empty and sibling part(s) "
            f"{zcode.ATTACHMENT_FILE_PART} are the only other parts: "
            "attachment-only user message",
        ),
    ]
