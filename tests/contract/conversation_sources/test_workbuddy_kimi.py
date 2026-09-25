"""workbuddy / kimi / kimi-work 记录覆盖契约（一个生产模块一个测试文件）。

公开 seam：``registry.adapt_for(family, artifact_set, artifact_root=...)``。
一家客户端一个家族键，但 workbuddy / kimi / kimi-work 由同一个生产模块
（``workbuddy_kimi``）服务、共享同一个 ``ADAPTER_VERSION``，所以它们共用一个
测试文件，其中 kimi / kimi-work 走同一套 wire，用 ``parametrize`` 表达。

可观察行为：
  * 每条原生记录都成为事件，能读到的用户 / 助手 / 推理正文进入 ``content``；
  * 读不出正文时 ``field_dispositions.reason`` 写明原生类型或缺正文的字段；
  * 会话 ``started_at`` / ``ended_at`` 取全部记录时间的极值，不倒挂。

不变量：夹具是合成记录，不写权威库，不复制真实对话。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind, RelationKind
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import workbuddy_kimi as wbk


def _assert_named_type(event, native_type: str) -> None:
    assert event is not None, native_type
    assert native_type in artifacts.reasons(event)
    assert not (
        event.kind is EventKind.UNKNOWN_NATIVE and not event.field_dispositions
    )


# ------------------------------------------------------------------ kimi wire

@pytest.mark.parametrize("family", wbk.LOOP_FAMILIES)
def test_old_wire_bodies_and_typeless_records(tmp_path, family):
    user = "夹具用户提问甲"
    appended_user = "夹具上下文用户乙"
    appended_assistant = "夹具上下文助手丙"
    part_text = "夹具助手正文丁"
    part_think = "夹具推理戊"
    steer = "夹具转向己"
    records = [
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": user}],
            "time": 1_700_000_000_000,
        },
        {
            "type": "context.append_message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": appended_user}],
            },
            "time": 1_700_000_000_100,
        },
        {
            "type": "context.append_message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": appended_assistant}],
            },
            "time": 1_700_000_000_200,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "part": {"type": "text", "text": part_text},
            },
            "time": 1_700_000_000_300,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "part": {"type": "think", "think": part_think},
            },
            "time": 1_700_000_000_400,
        },
        {
            "type": "llm.request",
            "model": "fixture-model",
            "messageCount": 1,
            "time": 1_700_000_000_500,
        },
        {
            "type": "config.update",
            "profileName": "fixture",
            "time": 1_700_000_000_600,
        },
        {
            "type": "turn.steer",
            "input": [{"type": "text", "text": steer}],
            "time": 1_700_000_000_700,
        },
    ]
    if family == "kimi-work":
        records.append({
            "type": "tools.register_user_tool",
            "name": "fixture_tool",
            "time": 1_700_000_000_800,
        })

    artifact, root = wbk.wire(
        tmp_path, f"{family}.old_wire", "sessions/fixture/wire.jsonl", records,
        family=family,
    )
    result = registry.adapt_for(family, artifacts.single(artifact), artifact_root=root)
    events = wbk.record_events(result)
    assert len(events) >= len(records)

    by_content = wbk.content_index(events)
    assert by_content[user].kind is EventKind.USER_MESSAGE
    assert by_content[appended_user].kind is EventKind.USER_MESSAGE
    assert by_content[appended_assistant].kind is EventKind.ASSISTANT_MESSAGE
    assert by_content[part_text].kind is EventKind.ASSISTANT_MESSAGE
    assert by_content[part_think].kind is EventKind.REASONING

    _assert_named_type(wbk.event_containing(events, "llm.request"), "llm.request")
    _assert_named_type(wbk.event_containing(events, "config.update"), "config.update")

    steer_event = by_content.get(steer) or wbk.event_containing(events, steer)
    assert steer_event is not None
    assert steer in (steer_event.content or "")
    assert steer_event.content

    if family == "kimi-work":
        _assert_named_type(
            wbk.event_containing(events, "tools.register_user_tool"),
            "tools.register_user_tool",
        )


@pytest.mark.parametrize("family", wbk.LOOP_FAMILIES)
def test_envelope_prompt_and_unmapped_types(tmp_path, family):
    prompt = "夹具信封用户提问"
    records = [
        {
            "kind": "event",
            "seq": 1,
            "envelope": {
                "type": "turn.started",
                "seq": 1,
                "session_id": "fixture-session",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {"prompt": prompt, "turnId": 1},
            },
        },
        {
            "kind": "event",
            "seq": 2,
            "envelope": {
                "type": "prompt.completed",
                "seq": 2,
                "session_id": "fixture-session",
                "timestamp": "2026-01-01T00:00:01Z",
                "payload": {"promptId": "prompt-fixture-1"},
            },
        },
        {
            "kind": "event",
            "seq": 3,
            "envelope": {
                "type": "permission.approval.requested",
                "seq": 3,
                "session_id": "fixture-session",
                "timestamp": "2026-01-01T00:00:02Z",
                "payload": {"action": "review"},
            },
        },
        {
            "kind": "event",
            "seq": 4,
            "envelope": {
                "type": "cron.fired",
                "seq": 4,
                "session_id": "fixture-session",
                "timestamp": "2026-01-01T00:00:03Z",
                "payload": {"jobId": "job-fixture"},
            },
        },
    ]
    artifact, root = wbk.wire(
        tmp_path, f"{family}.envelope", "server/events/session_fixture.jsonl", records,
        family=family,
    )
    result = registry.adapt_for(family, artifacts.single(artifact), artifact_root=root)
    events = wbk.record_events(result)
    assert len(events) >= len(records)

    started = wbk.event_containing(events, prompt)
    assert started is not None
    assert prompt in (started.content or "")
    started_ok = started.kind is EventKind.USER_MESSAGE or (
        "prompt" in artifacts.reasons(started)
    )
    assert started_ok

    completed = wbk.event_containing(events, "prompt.completed") or wbk.event_containing(
        events, "promptId",
    )
    assert completed is not None
    assert "promptId" in artifacts.reasons(completed)
    assert "只有" in artifacts.reasons(completed)

    assert all(event.kind is not EventKind.ASSISTANT_MESSAGE for event in events)
    joined = " ".join(artifacts.reasons(event) for event in result.events)
    assert "wire.jsonl" in joined
    assert "本文件" in joined

    _assert_named_type(
        wbk.event_containing(events, "permission.approval.requested"),
        "permission.approval.requested",
    )
    _assert_named_type(wbk.event_containing(events, "cron.fired"), "cron.fired")


# --------------------------------------------------------------- workbuddy

def test_workbuddy_messages_reasoning_and_titles(tmp_path):
    user = "夹具工作伙伴用户"
    assistant = "夹具工作伙伴助手"
    reasoning = "夹具工作伙伴推理"
    ai_title = "夹具智能标题"
    custom_title = "夹具自定义标题"
    records = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": user}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": assistant}],
        },
        {
            "type": "reasoning",
            "content": [],
            "rawContent": [{"type": "reasoning_text", "text": reasoning}],
        },
        {"type": "ai-title", "aiTitle": ai_title, "sessionId": "fixture"},
        {"type": "custom-title", "customTitle": custom_title, "sessionId": "fixture"},
        {
            "type": "session-meta",
            "sessionId": "fixture",
            "meta": {"codebuddy.ai/hostKind": "desktop"},
        },
    ]
    artifact, root = wbk.wire(
        tmp_path, "workbuddy.session", "projects/fixture/session.jsonl", records,
        family="workbuddy",
    )
    result = registry.adapt_for(
        "workbuddy", artifacts.single(artifact), artifact_root=root,
    )
    events = wbk.record_events(result)
    assert len(events) >= len(records)

    by_content = wbk.content_index(events)
    assert by_content[user].kind is EventKind.USER_MESSAGE
    assert by_content[assistant].kind is EventKind.ASSISTANT_MESSAGE
    assert by_content[reasoning].kind is EventKind.REASONING

    for native_type, title in (
        ("ai-title", ai_title),
        ("custom-title", custom_title),
        ("session-meta", None),
    ):
        event = wbk.event_containing(events, native_type)
        _assert_named_type(event, native_type)
        if title is not None:
            assert title in ((event.content or "") + (event.summary or ""))


# ------------------------------------------------- 会话起止时间（不倒挂）

def test_session_time_bounds_never_reverse(tmp_path):
    # 记录0 用 created_at（较晚），记录1 用毫秒纪元 time（较早）：
    # 旧逻辑 started=records[0] > ended=records[-1] 会倒挂；新逻辑取极值修正。
    later = datetime(2026, 7, 1, 10, 0, 3, tzinfo=timezone.utc)
    earlier = datetime(2026, 7, 1, 10, 0, 1, tzinfo=timezone.utc)
    records = [
        {"created_at": later.isoformat().replace("+00:00", "Z")},
        {"time": int(earlier.timestamp() * 1000)},
    ]
    artifact, root = wbk.wire(
        tmp_path, "kimi.time_bounds", "wire.jsonl", records, family="kimi",
    )
    result = registry.adapt_for("kimi", artifacts.single(artifact), artifact_root=root)
    started, ended = result.sessions[0].started_at, result.sessions[0].ended_at
    assert started is not None and ended is not None
    assert started <= ended
    assert "10:00:01" in started  # 最早来自毫秒纪元记录
    assert "10:00:03" in ended    # 最晚来自 created_at 记录


def test_kimi_session_time_bounds_empty(tmp_path):
    # 空记录、或记录里没有任何时间字段：起止都是 None，不发明时间。
    for index, records in enumerate(([], [{}, {"foo": 1}])):
        artifact, root = wbk.wire(
            tmp_path, f"kimi.time_bounds_empty.{index}", "wire.jsonl", records,
            family="kimi",
        )
        result = registry.adapt_for(
            "kimi", artifacts.single(artifact), artifact_root=root,
        )
        assert (
            result.sessions[0].started_at, result.sessions[0].ended_at,
        ) == (None, None)


def test_kimi_wire_session_key_from_path_not_stem(tmp_path):
    """wire 日志记录里没有 session id 时，会话键取路径里的 session_<uuid>。

    实测 2026-09-25：97 份 agents/<agent>/wire.jsonl 历史快照的记录均无
    session id，旧的文件名词干兜底把全部快照折叠成一个伪会话 ``wire``。
    """
    session_uuid = "691f0a59-371f-498c-bda4-a5c5fa3ceb3a"
    records = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1_786_153_201_779},
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "夹具路径会话键提问"}],
            "time": 1_700_000_000_000,
        },
    ]
    artifact, root = wbk.wire(
        tmp_path, "kimi.wire_path_key",
        f"sessions/wd_career-os_eb4239af38e6/session_{session_uuid}/agents/main/wire.jsonl",
        records, family="kimi",
    )
    result = registry.adapt_for("kimi", artifacts.single(artifact), artifact_root=root)
    anchors = [
        event for event in result.events
        if (event.provenance.native_locator or "").endswith("#session")
    ]
    assert anchors, "会话生命周期锚点缺失"
    assert all(
        event.provenance.native_session_id == f"session_{session_uuid}"
        for event in anchors
    )
    assert result.sessions[0].native_session_id == f"session_{session_uuid}"


# ---------------------------------------- kimi agents/ 布局：子代理不再冒充主会话

def test_kimi_agents_layout_subfiles_are_subagents_not_main(tmp_path):
    """kimi 真实布局 ``agents/main`` 是主会话，``agents/agent-N`` 是子代理。

    同批提交主会话 + 子代理 wire 时不得再抛 "exactly one main artifact"
    （旧判定只认 ``subagents/`` 段，把 agent 文件当主 artifact，导致整批
    适配失败）；agent 文件走子代理路径：SUBAGENT_BOUNDARY 事件 + SUBAGENT
    关系指向主会话生命周期锚点。
    """
    session_uuid = "9aa399e2-ed55-4cdf-82c8-f6db235b3f0c"
    base = f"sessions/wd_fixture_eb4239af38e6/session_{session_uuid}/agents"
    artifact_set, root = wbk.wire_set(tmp_path, [
        (
            "kimi.agents.main", f"{base}/main/wire.jsonl",
            [{"type": "turn.prompt",
              "input": [{"type": "text", "text": "主会话提问"}],
              "time": 1_758_768_000_000}],
        ),
        (
            "kimi.agents.agent0", f"{base}/agent-0/wire.jsonl",
            [{"type": "turn.prompt",
              "input": [{"type": "text", "text": "子代理零提问"}],
              "time": 1_758_768_010_000}],
        ),
        (
            "kimi.agents.agent1", f"{base}/agent-1/wire.jsonl",
            [{"type": "turn.prompt",
              "input": [{"type": "text", "text": "子代理一提问"}],
              "time": 1_758_768_020_000}],
        ),
    ], family="kimi")

    result = registry.adapt_for("kimi", artifact_set, artifact_root=root)

    boundary_locators = [
        event.provenance.native_locator for event in result.events
        if event.kind is EventKind.SUBAGENT_BOUNDARY
        and (event.provenance.native_locator or "").endswith("#boundary")
    ]
    assert any("agent-0" in locator for locator in boundary_locators)
    assert any("agent-1" in locator for locator in boundary_locators)
    assert not any(f"{base}/main" in locator for locator in boundary_locators), (
        "agents/main 是主会话自身的 wire，不得判成子代理"
    )

    main_anchor = next(
        event for event in result.events
        if (event.provenance.native_locator or "").endswith("#session")
        and f"{base}/main" in event.provenance.native_locator
    )
    subagent_relations = [
        relation for relation in result.relations
        if relation.relation_kind is RelationKind.SUBAGENT
    ]
    assert len(subagent_relations) == 2
    assert all(
        relation.target_event_id == main_anchor.event_id
        for relation in subagent_relations
    )
    assert result.sessions
    assert result.sessions[0].native_session_id == f"session_{session_uuid}"


def test_workbuddy_subagents_layout_still_routes_subagent_files(tmp_path):
    """workbuddy 的 ``subagents/`` 布局行为不变：子代理文件不冒充主会话。"""
    artifact_set, root = wbk.wire_set(tmp_path, [
        (
            "workbuddy.subagents.main", "projects/fixture/session.jsonl",
            [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "主会话提问"}]}],
        ),
        (
            "workbuddy.subagents.agent", "projects/fixture/subagents/agent-1.jsonl",
            [{"type": "message", "role": "user", "text": "子代理提问"}],
        ),
    ], family="workbuddy")

    result = registry.adapt_for("workbuddy", artifact_set, artifact_root=root)

    assert any(
        relation.relation_kind is RelationKind.SUBAGENT
        for relation in result.relations
    )
    assert any(
        "subagents/agent-1.jsonl" in (event.provenance.native_locator or "")
        and event.kind is EventKind.SUBAGENT_BOUNDARY
        for event in result.events
    )


# --------------------------------------------- 时间戳规范化（秒 / 字符串 / ISO）

def test_timestamp_normalizes_seconds_digit_strings_and_iso(tmp_path):
    """occurred_at 统一为 ISO-8601 UTC（Z 后缀）。

    旧实现只认 ``int > 1e12``（毫秒）：10 位纪元秒（int 或数字字符串）与
    数字字符串毫秒原样透传成 ``"1758768000"`` 这类垃圾 occurred_at。
    """
    records = [
        {"type": "turn.prompt", "input": [{"type": "text", "text": "秒级整数"}],
         "time": 1_758_768_000},
        {"type": "turn.prompt", "input": [{"type": "text", "text": "秒级字符串"}],
         "time": "1758768060"},
        {"type": "turn.prompt", "input": [{"type": "text", "text": "毫秒字符串"}],
         "time": "1758768120000"},
        {"type": "turn.prompt", "input": [{"type": "text", "text": "带偏移ISO"}],
         "time": "2026-01-01T08:00:00+08:00"},
    ]
    artifact, root = wbk.wire(
        tmp_path, "kimi.timestamps", "wire.jsonl", records, family="kimi",
    )
    result = registry.adapt_for("kimi", artifacts.single(artifact), artifact_root=root)

    by_content = wbk.content_index(wbk.record_events(result))
    assert by_content["秒级整数"].occurred_at == "2025-09-25T02:40:00Z"
    assert by_content["秒级字符串"].occurred_at == "2025-09-25T02:41:00Z"
    assert by_content["毫秒字符串"].occurred_at == "2025-09-25T02:42:00Z"
    assert by_content["带偏移ISO"].occurred_at == "2026-01-01T00:00:00Z"


# --------------------------------------------------------------- 超限正文

def test_kimi_oversized_bodies_are_kept_in_full(tmp_path):
    """工具入参/产出、spliced 与未知记录正文都是正文，不设内容上限。"""
    for family in wbk.LOOP_FAMILIES:
        artifact, root = wbk.oversize_wire(
            tmp_path, f"{family}.oversize", wbk.oversize_loop_records(),
            family=family,
        )
        result = registry.adapt_for(
            family, artifacts.single(artifact), artifact_root=root,
        )
        for label, expected in (
            ("tool input", wbk.BIG_TOOL_INPUT),
            ("tool output", wbk.BIG_TOOL_OUTPUT),
            ("spliced", wbk.BIG_SPLICED),
            ("unknown record", wbk.BIG_UNKNOWN),
            ("nested text", wbk.BIG_NESTED),
        ):
            assert any(
                expected in (event.content or "") for event in result.events
            ), f"{family}: {label} was cut; full text must land in content"
        assert not artifacts.any_reason(result, "truncated"), (
            f"{family}: content cap removed, no truncation disposition allowed"
        )


def test_workbuddy_oversized_bodies_are_kept_in_full(tmp_path):
    """workbuddy 消息、推理、工具入参/产出与未知记录正文都不设上限。"""
    artifact, root = wbk.oversize_wire(
        tmp_path, "workbuddy.oversize", wbk.oversize_workbuddy_records(),
        family="workbuddy",
    )
    result = registry.adapt_for(
        "workbuddy", artifacts.single(artifact), artifact_root=root,
    )
    contents = {event.content for event in result.events}
    for label, expected in (
        ("message", wbk.BIG_MESSAGE),
        ("reasoning", wbk.BIG_REASONING),
        ("tool input", wbk.BIG_TOOL_INPUT),
        ("tool output", wbk.BIG_TOOL_OUTPUT),
    ):
        assert expected in contents, f"{label} was cut; full text must land in content"
    assert any(
        wbk.BIG_UNKNOWN in (event.content or "") for event in result.events
    ), "unknown record was cut; full text must land in content"
    assert not artifacts.any_reason(result, "truncated"), (
        "content cap removed: no truncation disposition may be produced"
    )


# ---------------------------------------- 探测器全函数（三家共用一份 detect）
# 探测器是全函数：畸形文件判「不是我」，不得把解析异常抛给发现层（会被记成
# probe_error）。非 UTF-8 / UTF-16 字节 = 写了一半或别的编码落地的轨迹。
# 期望值是独立字面量 False。
NON_UTF8_LINE = bytes([0xFF, 0xFE, 0x00]) + b'{"type":"session"}'
UTF16_LINE = '{"type":"session"}'.encode("utf-16")


@pytest.mark.parametrize("family", ["workbuddy", "kimi", "kimi-work"])
@pytest.mark.parametrize(
    "name,raw",
    [("session.jsonl", NON_UTF8_LINE), ("session.jsonl", UTF16_LINE)],
)
def test_workbuddy_kimi_detector_rejects_malformed_bytes_without_raising(
    tmp_path, family, name, raw
):
    artifact, root = artifacts.probe_file(tmp_path, name, raw)
    assert registry.detect_family(family, artifact, artifact_root=root) is False


# ------------------------------- 批次 B 修复包回归（P1-11 / P2 x4）

def test_session_time_bounds_mixed_naive_and_aware(tmp_path):
    """P1-11：naive / aware / 带偏移 / 毫秒纪元混排时起止仍按同一时间线取极值。

    naive 时间戳假定 UTC（与本模块 _timestamp 的既有约定一致）。
    """
    epoch_ms = int(datetime(2026, 7, 1, 10, 0, 1, tzinfo=timezone.utc).timestamp() * 1000)
    records = [
        {"created_at": "2026-07-01T10:00:03"},            # naive，最晚
        {"time": epoch_ms},                               # 毫秒纪元，最早
        {"timestamp": "2026-07-01T12:00:02+02:00"},       # 带偏移，居中
    ]
    artifact, root = wbk.wire(
        tmp_path, "kimi.time_bounds_tz", "wire.jsonl", records, family="kimi",
    )
    result = registry.adapt_for("kimi", artifacts.single(artifact), artifact_root=root)
    started, ended = result.sessions[0].started_at, result.sessions[0].ended_at
    assert started == "2026-07-01T10:00:01Z"
    assert ended == "2026-07-01T10:00:03Z"


def test_session_time_bounds_compares_naive_as_utc(monkeypatch):
    """P1-11 比较层单测：绕过 _timestamp 归一，直接喂 naive/aware 混排串。

    naive 若被按本地时区解释（本机 UTC+8 时 ``12:00:03`` 会被算成 04:00Z），
    min/max 就在错误时间线上取值；必须统一转 aware-UTC 后比较。
    """
    from personal_knowledge.adapters.conversation_sources import (
        workbuddy_kimi as wbk_module,
    )

    monkeypatch.setattr(wbk_module, "_record_time", lambda record: record["raw"])
    started, ended = wbk_module._session_time_bounds([
        {"raw": "2026-07-01T12:00:03"},        # naive，实际是全天最晚
        {"raw": "2026-07-01T10:00:01+00:00"},  # aware，实际最早
    ])
    assert started == "2026-07-01T10:00:01+00:00"
    assert ended == "2026-07-01T12:00:03"


def test_envelope_subagent_boundaries_keep_distinct_native_ids(tmp_path):
    """P2：subagent.started/completed/failed 共享 subagentId，native_event_id
    必须带生命周期后缀区分，否则下游按 native_event_id 去重会把三条折叠。"""
    def boundary(seq: int, phase: str) -> dict:
        return {
            "kind": "event",
            "seq": seq,
            "envelope": {
                "type": f"subagent.{phase}", "seq": seq,
                "session_id": "fixture-session",
                "timestamp": f"2026-01-01T00:00:0{seq}Z",
                "payload": {"subagentId": "agent-9"},
            },
        }

    records = [boundary(1, "started"), boundary(2, "completed"), boundary(3, "failed")]
    artifact, root = wbk.wire(
        tmp_path, "kimi.subagent_ids", "server/events/session_fixture.jsonl",
        records, family="kimi",
    )
    result = registry.adapt_for("kimi", artifacts.single(artifact), artifact_root=root)
    sub_events = [
        event for event in wbk.record_events(result)
        if event.kind is EventKind.SUBAGENT_BOUNDARY
        and (event.provenance.native_event_id or "").startswith("agent-9")
    ]
    assert len(sub_events) == 3
    assert {event.provenance.native_event_id for event in sub_events} == {
        "agent-9#started", "agent-9#completed", "agent-9#failed",
    }
    assert len({event.event_id for event in sub_events}) == 3
    # 关系层仍按 subagentId 每个子代理一条，不随后缀膨胀。
    subagent_relations = [
        relation for relation in result.relations
        if relation.relation_kind is RelationKind.SUBAGENT
    ]
    assert len(subagent_relations) == 1


def test_kimi_bad_json_subagent_file_does_not_break_main_session(tmp_path):
    """P2：一份坏 JSON 的 agent-*.jsonl 只计入 warnings，不炸整场适配。"""
    base = "sessions/wd_fixture_eb4239af38e6/session_691f0a59-371f-498c-bda4-a5c5fa3ceb3a/agents"
    artifact_set, root = wbk.wire_set(tmp_path, [
        (
            "kimi.badjson.main", f"{base}/main/wire.jsonl",
            [{"type": "turn.prompt",
              "input": [{"type": "text", "text": "主会话提问"}],
              "time": 1_758_768_000_000}],
        ),
        (
            "kimi.badjson.agent0", f"{base}/agent-0/wire.jsonl",
            '{"type": "turn.prompt", "time": 1758768010000\n{"broken": \n',
        ),
    ], family="kimi")

    result = registry.adapt_for("kimi", artifact_set, artifact_root=root)

    assert result.sessions, "主会话必须照常产出"
    assert any(
        event.kind is EventKind.USER_MESSAGE and "主会话提问" in (event.content or "")
        for event in wbk.record_events(result)
    )
    assert any("agent0" in warning for warning in result.warnings), result.warnings
    # 坏文件整体跳过：不产出子代理边界，也不产出子代理关系。
    assert not any(
        relation.relation_kind is RelationKind.SUBAGENT
        for relation in result.relations
    )


def test_kimi_envelope_subagent_child_lifecycle_uses_envelope_timestamp(tmp_path):
    """P2：envelope 格式子代理文件的首条时间在 envelope.timestamp，子代理
    lifecycle 锚点不得取成 None。"""
    base = "sessions/wd_fixture_eb4239af38e6/session_691f0a59-371f-498c-bda4-a5c5fa3ceb3a/agents"
    artifact_set, root = wbk.wire_set(tmp_path, [
        (
            "kimi.envsub.main", f"{base}/main/wire.jsonl",
            [{"type": "turn.prompt",
              "input": [{"type": "text", "text": "主会话提问"}],
              "time": 1_758_768_000_000}],
        ),
        (
            "kimi.envsub.agent0", f"{base}/agent-0/events.jsonl",
            [{
                "kind": "event",
                "seq": 1,
                "envelope": {
                    "type": "turn.started", "seq": 1,
                    "session_id": "child-fixture",
                    "timestamp": "2026-01-01T00:00:05Z",
                    "payload": {"prompt": "子代理提问", "turnId": 1},
                },
            }],
        ),
    ], family="kimi")

    result = registry.adapt_for("kimi", artifact_set, artifact_root=root)

    child_anchor = next(
        event for event in result.events
        if (event.provenance.native_locator or "").endswith("#session")
        and "agent-0" in event.provenance.native_locator
    )
    assert child_anchor.occurred_at == "2026-01-01T00:00:05Z"


@pytest.mark.parametrize("family", wbk.LOOP_FAMILIES)
@pytest.mark.parametrize(
    "name,kind_pair",
    [
        ("compact", '"kind":"event"'),
        ("single_space", '"kind": "event"'),
        ("wide_space", '"kind"  :  "event"'),
        ("tab", '"kind":\t"event"'),
    ],
)
def test_kimi_detector_tolerates_whitespace_around_kind_colon(
    tmp_path, family, name, kind_pair
):
    """P2：detect 的 kind:event 标记不得假定紧凑 JSON（冒号后无空格）。"""
    raw = "{" + kind_pair + ',"seq":1,"envelope":{"type":"turn.started"}}\n'
    artifact, root = artifacts.file_artifact(
        tmp_path, f"{family}.detect_ws_{name}", "server/events/session_fixture.jsonl",
        raw, family=family,
    )
    assert registry.detect_family(family, artifact, artifact_root=root) is True
