"""mimo / opencode 记录覆盖契约（一个生产模块一个测试文件）。

公开 seam：``registry.adapt_for(family, artifact_set, artifact_root=...)`` 与
``registry.detect_family(family, artifact, artifact_root=...)``。
mimo 与 opencode 由同一个生产模块（``mimo_opencode``）服务并共享一个
``ADAPTER_VERSION``，所以两家写在同一个文件里、用 ``parametrize`` 表达。

可观察行为：
  * **live** 形状（``session``/``message``/``part``）的正文在 ``part.data`` 里，
    不是 ``message.data``；text part 跟随父消息角色；
  * 加密推理不冒充正文，明文推理保留全文，subtask 的 prompt 是正文，
    patch 没有正文但写明 ``type=patch``；
  * 空 session 明说没有 message / part；
  * 相邻凭据表永不进入 artifact，canary 值永不进入事件。

不变量：夹具是合成库与合成句子，不读权威库，不复制真实对话。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import mimo_opencode as mio


@pytest.fixture(params=mio.FAMILIES)
def live(tmp_path, request):
    """live 形状合成库的适配结果 ``(family, result)``。"""
    family = request.param
    artifact, root = mio.live_store(tmp_path, family)
    result = registry.adapt_for(
        family, artifacts.single(artifact), artifact_root=root,
    )
    return family, result


@pytest.fixture(params=mio.FAMILIES)
def visible(tmp_path, request):
    """visible 形状（经真实 capture seam 抓取）的适配结果 ``(family, result)``。"""
    family = request.param
    artifact, root = mio.captured_store(tmp_path)
    result = registry.adapt_for(
        family, artifacts.single(artifact), artifact_root=root,
    )
    return family, result


# ------------------------------------------------------------------- live 形状

def test_text_part_follows_parent_role(live):
    _family, result = live
    user = mio.event_named(result, "p-user")
    assistant = mio.event_named(result, "p-asst")
    assert user.kind is EventKind.USER_MESSAGE
    assert user.content == mio.LIVE_USER_TEXT
    assert assistant.kind is EventKind.ASSISTANT_MESSAGE
    assert assistant.content == mio.LIVE_ASSISTANT_TEXT


def test_encrypted_reasoning_is_explained_and_plaintext_is_kept(live):
    _family, result = live
    encrypted = mio.event_named(result, "p-reason-enc")
    assert encrypted.kind is not EventKind.UNKNOWN_NATIVE
    assert encrypted.kind is EventKind.REASONING
    assert not encrypted.content
    reasons = [item.reason for item in encrypted.field_dispositions]
    assert any(
        "reasoningEncryptedContent" in reason
        and "无本地密钥" in reason
        and "不能解密" in reason
        for reason in reasons
    )

    plain = mio.event_named(result, "p-reason-plain")
    assert plain.kind is EventKind.REASONING
    assert plain.content == mio.LIVE_REASONING_TEXT


def test_subtask_prompt_is_content(live):
    _family, result = live
    event = mio.event_named(result, "p-subtask")
    assert event.content == mio.LIVE_SUBTASK_PROMPT


def test_patch_without_text_is_not_bare_unknown(live):
    _family, result = live
    event = mio.event_named(result, "p-patch")
    assert event.field_dispositions
    assert any(
        "type=patch" in item.reason
        and "hash" in item.reason
        and "files" in item.reason
        and "没有正文" in item.reason
        for item in event.field_dispositions
    )


def test_empty_session_explains_missing_message_and_part(live):
    _family, result = live
    events = [
        event for event in result.events
        if event.provenance.native_session_id == "s-empty"
    ]
    assert len(events) == 1
    event = events[0]
    assert event.kind is EventKind.SESSION_LIFECYCLE
    assert event.field_dispositions
    assert any(
        "session" in item.reason
        and "没有" in item.reason
        and "message" in item.reason
        and "part" in item.reason
        for item in event.field_dispositions
    )


# ---------------------------------------------------------------- visible 形状

def test_detect_accepts_the_conversation_tables(tmp_path):
    artifact, root = mio.captured_store(tmp_path)
    assert registry.detect_family("mimo", artifact, artifact_root=root) is True


def test_family_stays_inside_the_module_it_belongs_to(visible):
    family, result = visible
    assert family in ("mimo", "opencode")
    # 经 seam 看到的家族身份与请求的家族键一致（两家共用一套解析原语）。
    assert result.family == family


def test_message_relations(visible):
    _family, result = visible
    assert len(result.sessions) == 1
    kinds = {event.kind for event in result.events}
    assert EventKind.USER_MESSAGE in kinds
    assert EventKind.ASSISTANT_MESSAGE in kinds
    assert EventKind.REASONING in kinds


def test_exact_message_content_is_not_stored_as_summary(visible):
    _family, result = visible
    message = next(
        event for event in result.events
        if event.kind is EventKind.USER_MESSAGE
    )
    assert message.content == mio.VISIBLE_USER_TEXT
    assert message.summary is None


def test_canary_never_in_events(visible):
    _family, result = visible
    assert artifacts.CANARY not in artifacts.event_text(result)


# ---------------------------------------------------------------- 超限正文

def test_mimo_oversized_bodies_are_kept_in_full(tmp_path):
    """工具入参/产出、推理与 compaction 正文都是正文，不设内容上限。"""
    for family in mio.FAMILIES:
        artifact, root = mio.live_oversize_store(tmp_path, family)
        result = registry.adapt_for(
            family, artifacts.single(artifact), artifact_root=root,
        )
        contents = {event.content for event in result.events}
        for label, expected in (
            ("tool input", mio.BIG_TOOL_INPUT),
            ("tool output", mio.BIG_TOOL_OUTPUT),
            ("reasoning", mio.BIG_REASONING),
            ("compaction", mio.BIG_COMPACTION),
        ):
            assert expected in contents, (
                f"{family}: {label} was cut; full text must land in content"
            )
        assert not artifacts.any_reason(result, "truncated"), (
            f"{family}: content cap removed, no truncation disposition allowed"
        )


# --------------------------- 丢失可见性 / usage 裸词收敛 / 占位标题（P2 收尾）

@pytest.fixture(params=mio.FAMILIES)
def loss(tmp_path, request):
    """丢失可见性 live 库的适配结果 ``(family, result)``。"""
    family = request.param
    artifact, root = mio.live_loss_store(tmp_path, family)
    result = registry.adapt_for(
        family, artifacts.single(artifact), artifact_root=root,
    )
    return family, result


def test_orphan_parts_are_counted_in_warnings(loss):
    """父消息不在 artifact 里的 part 必须计数进 warnings，不许静默消失。"""
    _family, result = loss
    assert any("1 orphan part(s)" in w for w in result.warnings), result.warnings


def test_malformed_json_payloads_are_counted_in_warnings(loss):
    """坏 JSON 载荷解码为空必须计数进 warnings，不许静默变 {}。"""
    _family, result = loss
    assert any(
        "1 malformed JSON payload(s)" in w for w in result.warnings
    ), result.warnings


def test_bare_counter_words_outside_token_context_make_no_usage(loss):
    """顶层裸词 ``input`` / ``read`` 数字不是 token 计数，不得伪造 USAGE。"""
    _family, result = loss
    bare_usage = [
        event for event in result.events
        if event.provenance.native_event_id == "p-bare:usage"
    ]
    assert bare_usage == [], (
        "bare word counters outside a token context fabricated a USAGE event"
    )


def test_usage_dict_and_tokens_aggregate_map_canonically(loss):
    """裸词在 token 上下文（usage 字典 / tokens 聚合）里照常映射成规范 usage。"""
    _family, result = loss
    assert mio.event_named(result, "m-ok:usage").summary == (
        mio.LOSS_USAGE_DICT_SUMMARY
    )
    assert mio.event_named(result, "p-tokens:usage").summary == (
        mio.LOSS_TOKENS_AGGREGATE_SUMMARY
    )


def test_placeholder_session_title_is_filtered(loss):
    """系统注入的脚手架标题不得成为 session title（对齐 codex / zcode）。"""
    _family, result = loss
    session = next(
        s for s in result.sessions if s.native_session_id == "s-loss"
    )
    assert session.title is None
