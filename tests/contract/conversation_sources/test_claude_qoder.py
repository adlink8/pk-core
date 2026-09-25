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
# P1-10 起 queue-operation 归 SESSION_LIFECYCLE（会话运维状态），不再落成
# system 消息；正文保全语义不变。

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
        result, EventKind.SESSION_LIFECYCLE, fixtures.QUEUE_OPERATION_TEXT
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
        result, EventKind.SESSION_LIFECYCLE, fixtures.QUEUE_OPERATION_SHORT_TEXT
    )
    assert artifacts.has_event(
        result, EventKind.ASSISTANT_MESSAGE, fixtures.QUEUE_OPERATION_ASSISTANT_TEXT
    )


# ------------------------------------------------ P1-10 会话键 / role 污染

@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_meta_record_types_never_become_system_role(tmp_path, family):
    """P1-10：运维/元数据记录不得落成 system 消息（role 污染根因）。

    权威库试点实测：qoder 65.4% / claude 57.7% 的 canonical 消息 role=system，
    根因是 attachment / mode / active-leaf 等无信封记录全部被归类为
    system_message。现在它们必须是 FILE_CONTEXT / SESSION_LIFECYCLE，
    且字段信息经 summary 不丢。
    """
    _, _, result = fixtures.adapt_family(
        family, tmp_path, "meta-records.jsonl", fixtures.meta_records(),
    )

    system_events = [
        event for event in result.events
        if event.kind is EventKind.SYSTEM_MESSAGE
    ]
    assert system_events == [], (
        "运维/元数据记录仍然被归类为 system 消息（role 污染未修复）"
    )
    allowed = {EventKind.SESSION_LIFECYCLE, EventKind.FILE_CONTEXT}
    kinds = {event.kind for event in result.events}
    assert kinds <= allowed, kinds
    for type_name in (
        *fixtures.META_SESSION_STATE_TYPES, *fixtures.META_FILE_CONTEXT_TYPES
    ):
        assert any(
            type_name in fixtures.explained(event) for event in result.events
        ), f"{type_name} 的原生信息没有落在任何事件上"


@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_genuine_system_subtypes_keep_system_role_unknown_stays_unknown(
    tmp_path, family,
):
    """真系统行（api_error / away_summary）仍是 system；未知 subtype 不再冒充。

    未知 subtype 归 UNKNOWN_NATIVE（按原生位置保全），不得顺手落成
    system 消息。
    """
    _, _, result = fixtures.adapt_family(
        family, tmp_path, "system-subtypes.jsonl", fixtures.system_subtype_records(),
    )

    by_native = {
        event.provenance.native_event_id: event for event in result.events
    }
    assert by_native["sys-1"].kind is EventKind.SYSTEM_MESSAGE
    assert (by_native["sys-1"].summary or "").startswith("api_error:")
    assert by_native["sys-2"].kind is EventKind.SYSTEM_MESSAGE
    assert by_native["sys-3"].kind is EventKind.UNKNOWN_NATIVE
    assert by_native["sys-3"].kind is not EventKind.SYSTEM_MESSAGE


# ------------------------------------------------ P1-7 会话键 stem 兜底

@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_session_key_falls_back_to_uuid_stem(tmp_path, family):
    """记录无会话 id 且文件名词干是 uuid 形态时，词干即原生会话键。"""
    name = f"{fixtures.NO_KEY_SESSION_FILE_UUID}.jsonl"
    _, _, result = fixtures.adapt_family(
        family, tmp_path, name, fixtures.no_session_key_records(),
    )
    assert result.sessions
    for session in result.sessions:
        assert session.native_session_id == fixtures.NO_KEY_SESSION_FILE_UUID


@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_session_key_uses_deterministic_path_key_when_stem_is_not_an_id(
    tmp_path, family,
):
    """导出副本 / 重命名文件的词干不是会话 id，不得产生伪会话键。

    词干不匹配 uuid 形态时必须用确定性全路径键（``<family>-path:<path>``，
    与 grok.py 的 ``grok-path:`` 模式等价），不同文件不再撞同一个键。
    """
    name = "session copy.jsonl"
    _, _, result = fixtures.adapt_family(
        family, tmp_path, name, fixtures.no_session_key_records(),
    )
    expected = f"{family}-path:{name}"
    assert result.sessions
    for session in result.sessions:
        assert session.native_session_id == expected


# ------------------------------------------------ P2 会话标题兜底

@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_session_title_skips_placeholders_and_subagent_messages(tmp_path, family):
    """标题不得取命令占位（``<...>`` 包裹），也不得取子代理首句。"""
    _, _, result = fixtures.adapt_family(
        family, tmp_path, "title.jsonl", fixtures.title_records(),
    )
    titles = {session.title for session in result.sessions}
    assert fixtures.TITLE_REAL_TEXT in titles, titles
    assert fixtures.TITLE_PLACEHOLDER_TEXT not in titles
    assert fixtures.TITLE_SUBAGENT_TEXT not in titles


@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_session_title_skips_agents_md_injection_block(tmp_path, family):
    """AGENTS.md 注入块不是用户说的话，不得成为标题。"""
    _, _, result = fixtures.adapt_family(
        family, tmp_path, "title-agents.jsonl",
        fixtures.title_agents_injection_records(),
    )
    assert result.sessions
    assert all(
        session.title == fixtures.TITLE_REAL_TEXT for session in result.sessions
    ), [session.title for session in result.sessions]


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


# ------------------------------------------------ P2 detect 有界分块探测

@pytest.mark.parametrize("family", ["claude", "qoder"])
def test_detector_scans_past_a_giant_first_record_without_reading_whole_file(
    tmp_path, family,
):
    """P2：首条记录超过 16K 时探测器仍能找到其后的 DAG 记录。

    分块扫描取代全文件读内存：记录形状判定在扫描上限内逐块进行，真实
    导出（首条用户 prompt 本身可达数十 KB）不再因为窗口截断而漏判。
    """
    raw = artifacts.jsonl(fixtures.deep_first_record_records())
    assert len(raw.encode("utf-8")) > 16_384, (
        "fixture must exceed the single-chunk window for this test to mean anything"
    )
    artifact, root = artifacts.probe_file(tmp_path, "deep-first.jsonl", raw)
    assert registry.detect_family(family, artifact, artifact_root=root) is True
