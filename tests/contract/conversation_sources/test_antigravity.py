"""Antigravity 适配器契约（family ``antigravity``，ADAPTER_VERSION 1.2.1）。

公开 seam 只有 registry：``registry.adapt_for`` / ``registry.detect_family`` /
``registry.capability_for``。生产代码也只经 registry 消费适配器，所以产品契约
在族级入口上，不在 ``antigravity.adapt`` 这类模块函数上。

夹具与构造器在 ``support/antigravity.py``，通用断言助手在 ``support/artifacts.py``；
正文一律合成，不读权威库、不复制真实会话。材料逐条搬运自：

  - ``test_antigravity_gemini_record_coverage.py``（antigravity 部分，3）
  - ``test_antigravity_protobuf_decode.py``（21）
  - ``test_antigravity_protobuf_fix.py``（11）
  - ``test_conversation_store_adapters.py::TestAntigravity``（4）
  - ``test_conversation_source_fixes.py`` Fix 1（3）
"""

from __future__ import annotations

import json
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import discovery, registry
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
    RelationKind,
)
from tests.contract.conversation_sources.support import antigravity as fixtures
from tests.contract.conversation_sources.support import artifacts

# 合成正文常量：形状取自真实字段布局，句子本身是假的。
USER_PROMPT = "合成问题句"
ASSISTANT_REPLY = "### 合成结论先行"
REASONING_TRACE = "**合成推理轨迹**"
TOOL_ARGUMENTS = '{"CodeContent":"x"}'
COMPACTION_BODY = "合成任务概览"
SESSION_TITLE = "Synthetic Session Title"
TOOL_SUMMARY = "List synthetic directory"
TOOL_DIRECTORY = "D:/synthetic"

# 超限合成注解：显著超过旧的注解渲染上限（单值 300 / 合计 1200）。
LONG_ANNOTATION = "fixture-annotation-" * 400
# 超限 JSON 步骤正文：超过旧的 2048 摘要上限。
LONG_JSON_BODY = "fixture-json-body-" * 400
# 超限 legacy 层级库正文（非消息步骤 / 子轨迹）：超过旧的 2048 摘要上限。
LONG_LEGACY_BODY = "fixture-legacy-body-" * 400


# -------------------------------------------------------------------- harness

def _adapt_live(db: Path):
    """经 registry seam 适配一份 live 夹具库。"""
    return registry.adapt_for(
        "antigravity", artifacts.single(fixtures.live_artifact(db)),
        artifact_root=db.parent,
    )


def _capture_live(tmp_path: Path, db: Path):
    """经真实 capture seam 抓 live 库，再经 registry seam 适配。"""
    tables, columns = discovery.SQLITE_ALLOWLISTS["antigravity"]
    artifact, root = artifacts.captured_sqlite(
        db, tmp_path / "store", allowed_tables=tables, allowed_columns=columns,
        byte_limit=10_000_000, count_limit=10, family="antigravity",
        mirror_path=db.name,
    )
    return registry.adapt_for(
        "antigravity", artifacts.single(artifact), artifact_root=root,
    )


def _only(result, kind: EventKind):
    picked = [event for event in result.events if event.kind is kind]
    assert len(picked) == 1, f"expected exactly one {kind.value}, got {len(picked)}"
    return picked[0]


def _content_events(result):
    return [
        event for event in result.events if event.kind is not EventKind.SESSION_LIFECYCLE
    ]


# --------------------------------------------------------- live transcript


class TestLiveTranscriptDecoded:
    """live 库 ``steps.step_payload`` 的 protobuf 正文必须被解码，不按引用保留。"""

    def test_user_prompt_and_timestamp(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(14, fixtures.user_step(USER_PROMPT))])
        event = _only(_adapt_live(db), EventKind.USER_MESSAGE)
        assert event.content == USER_PROMPT
        assert event.occurred_at == fixtures.EXPECTED_ISO

    def test_user_attachment_uri_and_prose(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(
            14,
            fixtures.user_step_with_attachment(
                "合成附件说明",
                "file:///d:/synthetic/notes/photo.png",
            ),
        )])
        event = _only(_adapt_live(db), EventKind.USER_MESSAGE)
        assert event.content == "合成附件说明"
        assert "photo.png" in (event.summary or "")

    def test_assistant_reply_reasoning_and_call(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(
            15,
            fixtures.assistant_step(
                ASSISTANT_REPLY, REASONING_TRACE, "call_131892",
                "write_to_file", TOOL_ARGUMENTS,
            ),
        )])
        result = _adapt_live(db)
        reply = _only(result, EventKind.ASSISTANT_MESSAGE)
        assert reply.content == ASSISTANT_REPLY
        thinking = _only(result, EventKind.REASONING)
        assert thinking.content == REASONING_TRACE
        call = _only(result, EventKind.TOOL_CALL)
        assert call.content == TOOL_ARGUMENTS
        assert call.summary == "write_to_file"

    def test_mirrored_field_does_not_duplicate_reply(self, tmp_path):
        # f20/f8 镜像 f20/f1：只允许产出一条助手事件。
        db = fixtures.live_db(tmp_path, [(
            15,
            fixtures.assistant_step("唯一回复", "t", "call_1", "run_command", "{}"),
        )])
        result = _adapt_live(db)
        replies = [e for e in result.events if e.kind is EventKind.ASSISTANT_MESSAGE]
        assert len(replies) == 1

    def test_usage_counters_are_reported_as_raw_field_numbers(self, tmp_path):
        # 字段普查：f5/f9 里 f1(常量 1318) 与 f6(常量 24) 是配置项，其余
        # f2/f3/f5/f9/f10 才是变化的计数器；只报计数器。
        usage_fields = (
            fixtures.vi(1, 1318) + fixtures.vi(2, 12443) + fixtures.vi(3, 346)
            + fixtures.vi(5, 4074) + fixtures.vi(6, 24) + fixtures.vi(9, 432)
        )
        f5 = (
            fixtures.ld(1, fixtures.vi(1, fixtures.EPOCH) + fixtures.vi(2, fixtures.NANOS))
            + fixtures.ld(9, usage_fields)
        )
        payload = (
            fixtures.vi(1, 15) + fixtures.vi(4, 3) + fixtures.ld(5, f5)
            + fixtures.ld(20, fixtures.st(1, "hi"))
        )
        db = fixtures.live_db(tmp_path, [(15, payload)])
        usage = _only(_adapt_live(db), EventKind.USAGE)
        assert "f2=12443" in usage.summary
        assert "f3=346" in usage.summary
        assert "f5=4074" in usage.summary
        assert "f9=432" in usage.summary
        # 常量配置字段不是计数器，必须排除
        assert "f1=" not in usage.summary
        assert "f6=" not in usage.summary

    def test_tool_result_summary_is_labelled_by_annotations(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (132, fixtures.tool_execution_step(
                "call_1", "run_command", "{}",
                "The command exited with code 0.",
                notes={"toolSummary": TOOL_SUMMARY,
                       "toolAction": "Listing synthetic directory",
                       "CommandLine": "ls"},
            )),
        ])
        outcome = _only(_adapt_live(db), EventKind.TOOL_RESULT)
        assert outcome.content == "The command exited with code 0."
        assert f"toolSummary={TOOL_SUMMARY}" in outcome.summary

    def test_nul_padded_utf16_tool_result_is_recovered(self, tmp_path):
        # Windows 的 PowerShell/wsl.exe 输出 UTF-16LE；库里存成 NUL 补齐的形态，
        # 去掉补齐字节之前不算「可打印文本」。
        text = "\nThe command exited with code 0.\nOutput:\n NAME    STATE\n wsl      RUNNING"
        db = fixtures.live_db(tmp_path, [
            (132, fixtures.tool_execution_step(
                "call_1", "run_command", "{}",
                raw_result=text.encode("utf-16-le"),
            )),
        ])
        outcome = _only(_adapt_live(db), EventKind.TOOL_RESULT)
        assert outcome.content == text
        assert (
            outcome.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.COMPLETE
        )

    def test_annotation_only_execution_is_kept(self, tmp_path):
        # 真实库里 10716 次执行中的 99 次带注解但没有结果文本；它们必须作为
        # 事件存活，不能被当成「空」丢弃。注解是此时唯一存活的原生正文，
        # 因此逐字进 content（旧契约只把渲染后的摘要放进 summary）。
        db = fixtures.live_db(tmp_path, [
            (15, fixtures.assistant_step("ls", "t", "call_1", "list_dir", "{}")),
            (132, fixtures.tool_execution_step(
                "call_1", "list_dir", "{}",
                notes={"toolSummary": TOOL_SUMMARY, "DirectoryPath": TOOL_DIRECTORY},
            )),
        ])
        result = _adapt_live(db)
        outcome = _only(result, EventKind.TOOL_RESULT)
        assert TOOL_SUMMARY in (outcome.content or "")
        assert f"DirectoryPath={TOOL_DIRECTORY}" in (outcome.content or "")
        assert TOOL_SUMMARY in outcome.summary
        assert f"DirectoryPath={TOOL_DIRECTORY}" in outcome.summary
        disp = outcome.field_dispositions[0]
        assert disp.disposition.value == "preserved_by_reference"
        assert any("annotations" in w for w in result.warnings)
        # call/result 仍然成对
        assert len(result.relations) == 1

    def test_annotation_only_execution_keeps_full_annotations(self, tmp_path):
        # 没有结果文本时，注解就是唯一存活的原生正文：全文必须进 content，
        # 不能被旧的 300/1200 渲染上限截断。
        db = fixtures.live_db(tmp_path, [
            (132, fixtures.tool_execution_step(
                "call_1", "run_command", "{}",
                notes={"CommandLine": LONG_ANNOTATION},
            )),
        ])
        outcome = _only(_adapt_live(db), EventKind.TOOL_RESULT)
        assert LONG_ANNOTATION in (outcome.content or ""), (
            "annotation-only body was cut; full text must land in content"
        )
        assert len(outcome.summary or "") <= 1200

    def test_unrecoverable_result_bytes_are_referenced_not_dropped(self, tmp_path):
        binary = bytes(range(256)) * 2  # 合法字节，但不是文本
        db = fixtures.live_db(tmp_path, [
            (132, fixtures.tool_execution_step(
                "call_1", "run_command", "{}", raw_result=binary,
            )),
        ])
        result = _adapt_live(db)
        outcome = _only(result, EventKind.TOOL_RESULT)
        assert outcome.content is None
        assert "not recoverable text" in outcome.summary
        assert outcome.native_payload_ref

    def test_tool_result_links_back_to_call(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (15, fixtures.assistant_step("run it", "t", "call_131892", "run_command", "{}")),
            (132, fixtures.tool_execution_step(
                "call_131892", "run_command", "{}", "The command exited with code 0.",
            )),
        ])
        result = _adapt_live(db)
        outcome = _only(result, EventKind.TOOL_RESULT)
        assert outcome.content == "The command exited with code 0."
        call = _only(result, EventKind.TOOL_CALL)
        assert len(result.relations) == 1
        relation = result.relations[0]
        assert relation.relation_kind is RelationKind.CALL_RESULT
        assert relation.source_event_id == outcome.event_id
        assert relation.target_event_id == call.event_id

    def test_reused_call_id_stays_unique_and_pairs_in_order(self, tmp_path):
        # 真实产物会跨 step 复用同一个 call id，所以原生 id 必须带上 step 序号，
        # 每个结果要配到最近一个尚未被认领的调用上。
        db = fixtures.live_db(tmp_path, [
            (15, fixtures.assistant_step("first", "t", "call_dup", "run_command", "{}")),
            (132, fixtures.tool_execution_step("call_dup", "run_command", "{}", "out-1")),
            (15, fixtures.assistant_step("second", "t", "call_dup", "run_command", "{}")),
            (132, fixtures.tool_execution_step("call_dup", "run_command", "{}", "out-2")),
        ])
        result = _adapt_live(db)
        ids = [e.event_id for e in result.events]
        assert len(ids) == len(set(ids))
        calls = [e for e in result.events if e.kind is EventKind.TOOL_CALL]
        outcomes = [e for e in result.events if e.kind is EventKind.TOOL_RESULT]
        assert len(calls) == 2
        assert len(outcomes) == 2
        pairs = {r.source_event_id: r.target_event_id for r in result.relations}
        assert pairs[outcomes[0].event_id] == calls[0].event_id
        assert pairs[outcomes[1].event_id] == calls[1].event_id
        assert len({r.relation_id for r in result.relations}) == 2

    def test_execution_error_becomes_unknown_native(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(17, fixtures.error_step())])
        event = _only(_adapt_live(db), EventKind.UNKNOWN_NATIVE)
        assert "Agent execution terminated" in event.summary
        assert "FAILED_PRECONDITION" in event.summary
        assert "400" in event.summary

    def test_compaction_summary_decoded(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(23, fixtures.compaction_step(COMPACTION_BODY))])
        event = _only(_adapt_live(db), EventKind.COMPACTION_SUMMARY)
        assert "<summary>" in event.content
        assert COMPACTION_BODY in event.content

    def test_compaction_body_without_summary_marker_is_captured(self, tmp_path):
        # 真实摘要里只有少数带 <summary> 标签，所以标签不能当选择器。
        body = "# Synthetic Continuation Summary\n\n## 1. Outstanding Items"
        db = fixtures.live_db(tmp_path, [(23, fixtures.step(23, fixtures.ld(30, fixtures.st(5, body))))])
        event = _only(_adapt_live(db), EventKind.COMPACTION_SUMMARY)
        assert event.content == body

    def test_compaction_title_only_step_is_not_dropped(self, tmp_path):
        # 没有正文的 step 仍在 f30/f4 里带着会话标题。
        payload = fixtures.step(23, fixtures.ld(
            30,
            fixtures.st(4, SESSION_TITLE)
            + fixtures.st(15, "file:///C:/synthetic/logs/transcript.jsonl"),
        ))
        db = fixtures.live_db(tmp_path, [(23, payload)])
        event = _only(_adapt_live(db), EventKind.COMPACTION_SUMMARY)
        assert event.content is None
        assert event.summary == SESSION_TITLE

    def test_subagent_message_decoded(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (101, fixtures.subagent_step("合成子代理标题", "合成子代理报告正文")),
        ])
        event = _only(_adapt_live(db), EventKind.SUBAGENT_BOUNDARY)
        assert event.content == "合成子代理报告正文"
        assert event.summary == "合成子代理标题"

    def test_content_fidelity_is_complete_when_decoded(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (14, fixtures.user_step("问题")),
            (15, fixtures.assistant_step("回答", "想", "call_1", "run_command", "{}")),
            (132, fixtures.tool_execution_step("call_1", "run_command", "{}", "输出")),
        ])
        result = _adapt_live(db)
        assert (
            result.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.COMPLETE
        )
        user = _only(result, EventKind.USER_MESSAGE)
        assert (
            user.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.COMPLETE
        )

    def test_empty_step_is_skipped_not_invented(self, tmp_path):
        # 只带元数据的 step 不产正文事件。适配器 1.2.1 起把这类空 step 保留成
        # 可解释的 unknown（disposition reason 里点名 step_type），所以断言落在
        # 「不造正文」上——旧文件写作「非 lifecycle 事件为空」，与 1.2.1 行为
        # （空 part 不许静默丢弃）已经不符，这里按现行契约等价改写。
        db = fixtures.live_db(tmp_path, [(15, fixtures.step(15))])
        result = _adapt_live(db)
        kept = _content_events(result)
        assert not [event for event in kept if event.content]
        assert kept and "step_type=15" in artifacts.reasons(kept[0])
        assert any("no recoverable transcript content" in w for w in result.warnings)

    def test_unmatched_tool_result_is_warned(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (132, fixtures.tool_execution_step("call_orphan", "run_command", "{}", "输出")),
        ])
        result = _adapt_live(db)
        assert result.relations == ()
        assert any("no matching tool call" in w for w in result.warnings)


class TestMalformedPayloadStillPreserved:
    """不合法 protobuf 只能按引用保留，不许猜正文。"""

    def test_truncated_payload_falls_back_to_reference(self, tmp_path):
        # 声明的长度大于缓冲区：wire format 不可信，不得从中解码任何内容。
        db = fixtures.live_db(tmp_path, [(14, fixtures.TRUNCATED_PAYLOAD)])
        result = _adapt_live(db)
        event = _only(result, EventKind.UNKNOWN_NATIVE)
        assert event.field_dispositions[0].disposition.value == "preserved_by_reference"
        assert (
            event.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.UNAVAILABLE
        )
        assert any("not well-formed protobuf" in w for w in result.warnings)

    def test_malformed_payload_does_not_break_other_steps(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (14, fixtures.user_step("合成正常问题")),
            (14, fixtures.TRUNCATED_PAYLOAD),
        ])
        result = _adapt_live(db)
        assert _only(result, EventKind.USER_MESSAGE).content == "合成正常问题"
        assert len([e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]) == 1


# --------------------------------------------- step_payload 类别：protobuf / JSON


class TestProtobufPayloadPreservedByReference:
    """覆盖 ``steps.step_payload`` 的类别 c：字节不合法 protobuf 时的落点。"""

    def test_protobuf_yields_unknown_native_without_crash(self, tmp_path):
        db = fixtures.live_db(
            tmp_path, [(0, fixtures.TRUNCATED_PAYLOAD), (0, fixtures.TRUNCATED_PAYLOAD)],
        )
        result = _adapt_live(db)
        assert result.family == "antigravity"
        unknown = [e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]
        assert len(unknown) == 2
        for ev in unknown:
            assert ev.ordinal is not None
            assert ev.summary and "step_type=" in ev.summary

    def test_protobuf_has_explicit_step_payload_field_disposition(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(0, fixtures.TRUNCATED_PAYLOAD)])
        result = _adapt_live(db)
        events_with_disp = [e for e in result.events if e.field_dispositions]
        assert len(events_with_disp) == 1
        disp = events_with_disp[0].field_dispositions[0]
        assert disp.field_name == "step_payload"
        assert disp.disposition.value == "preserved_by_reference"
        assert "protobuf" in disp.reason.lower()

    def test_content_availability_unavailable_on_protobuf_step(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(0, fixtures.TRUNCATED_PAYLOAD)])
        result = _adapt_live(db)
        events_with_disp = [e for e in result.events if e.field_dispositions]
        ev = events_with_disp[0]
        assert (
            ev.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.UNAVAILABLE
        )

    def test_no_n_by_m_step_duplication(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(0, fixtures.TRUNCATED_PAYLOAD)] * 38)
        result = _adapt_live(db)
        unknown = [e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]
        assert len(unknown) == 38
        ids = [e.event_id for e in result.events]
        assert len(ids) == len(set(ids))

    def test_warning_states_protobuf_reason(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(0, fixtures.TRUNCATED_PAYLOAD)])
        result = _adapt_live(db)
        joined = " ".join(result.warnings)
        assert "protobuf" in joined
        assert "schema" in joined

    def test_capability_declares_wire_format_decoding(self):
        # 1.2.x 把「无 schema 就标 unavailable」换成真实的 wire-format 解码；
        # 只有非法载荷才回落到按引用保留。
        capability = registry.capability_for("antigravity")
        assert "wire_format" in capability.capabilities["content_availability"]
        assert capability.adapter_version == "1.3.0"


class TestJsonPayloadClassified:
    """同样一列可以装 UTF-8 JSON 载荷（未来/替代存储），也要映射成类型化事件。"""

    def test_json_payload_maps_user_and_assistant(self, tmp_path):
        payload = json.dumps(
            {"phase": "a", "items": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]}
        ).encode("utf-8")
        db = fixtures.live_db(tmp_path, [(0, payload)])
        result = _adapt_live(db)
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        user = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
        assert user.content == "hello"
        assert user.summary is None
        assistant = next(e for e in result.events if e.kind is EventKind.ASSISTANT_MESSAGE)
        assert assistant.content == "hi there"

    def test_json_with_usage_yields_usage_event(self, tmp_path):
        payload = json.dumps(
            {"items": [{
                "role": "assistant", "content": "a",
                "usage": {"input_tokens": 12, "output_tokens": 5},
            }]}
        ).encode("utf-8")
        db = fixtures.live_db(tmp_path, [(0, payload)])
        result = _adapt_live(db)
        usage = [e for e in result.events if e.kind is EventKind.USAGE]
        assert len(usage) == 1
        assert "input_tokens=12" in usage[0].summary

    def test_json_list_payload(self, tmp_path):
        payload = json.dumps([
            {"role": "user", "content": "q1"},
            {"role": "tool", "content": "tool out"},
        ]).encode("utf-8")
        db = fixtures.live_db(tmp_path, [(0, payload)])
        result = _adapt_live(db)
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.TOOL_CALL in kinds

    def test_json_unrecognized_role_becomes_unknown(self, tmp_path):
        payload = json.dumps({"role": "weird_thing", "content": "x"}).encode("utf-8")
        db = fixtures.live_db(tmp_path, [(0, payload)])
        result = _adapt_live(db)
        unknown = [e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]
        assert len(unknown) == 1

    def test_json_warning_reported(self, tmp_path):
        payload = json.dumps({"role": "user", "content": "hi"}).encode("utf-8")
        db = fixtures.live_db(tmp_path, [(0, payload)])
        result = _adapt_live(db)
        assert any("JSON" in w for w in result.warnings)

    def test_json_non_message_body_lands_in_content(self, tmp_path):
        """非消息角色的 JSON 正文过去只进 2048 摘要，现在必须进 content。"""
        payload = json.dumps(
            {"role": "tool", "content": LONG_JSON_BODY}
        ).encode("utf-8")
        db = fixtures.live_db(tmp_path, [(0, payload)])
        result = _adapt_live(db)
        call = _only(result, EventKind.TOOL_CALL)
        assert call.content == LONG_JSON_BODY


# ------------------------------------------------------------- 记录覆盖形状


class TestLiveRecordCoverage:
    def test_antigravity_user_and_assistant_text_land_in_content(self, tmp_path):
        db = fixtures.live_db(tmp_path, [
            (14, fixtures.user_step("合成用户这一步")),
            (15, fixtures.assistant_reply_step("合成助手这一步")),
        ])
        result = _adapt_live(db)
        user = next(event for event in result.events if event.kind is EventKind.USER_MESSAGE)
        assistant = next(
            event for event in result.events if event.kind is EventKind.ASSISTANT_MESSAGE
        )
        assert user.content == "合成用户这一步"
        assert assistant.content == "合成助手这一步"

    def test_antigravity_step_type_17_error_is_explained(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(17, fixtures.error_step())])
        result = _adapt_live(db)
        kept = _content_events(result)
        assert len(kept) == 1
        event = kept[0]
        assert "step_type=17" in artifacts.reasons(event)
        assert "执行错误" in artifacts.reasons(event)
        assert "region blocked" in artifacts.body(event)

    def test_antigravity_empty_part_is_not_silently_dropped(self, tmp_path):
        db = fixtures.live_db(tmp_path, [(14, fixtures.user_step_without_body())])
        result = _adapt_live(db)
        kept = _content_events(result)
        assert len(kept) == 1
        assert "step_type=14" in artifacts.reasons(kept[0])


# --------------------------------------------------------- legacy 层级库


def _capture_legacy_hierarchy(tmp_path: Path, **fixture_kwargs):
    """写一份 legacy 层级库并经真实 capture seam 抓取。"""
    db = tmp_path / "store-src" / "trajectory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    fixtures.legacy_hierarchy_db(db, **fixture_kwargs)
    return artifacts.captured_sqlite(
        db, tmp_path / "blobs",
        allowed_tables=fixtures.LEGACY_ALLOWED_TABLES,
        allowed_columns=fixtures.LEGACY_ALLOWED_COLUMNS,
    )


def _adapt_legacy_hierarchy(tmp_path: Path, **fixture_kwargs):
    artifact, root = _capture_legacy_hierarchy(tmp_path, **fixture_kwargs)
    return registry.adapt_for(
        "antigravity", artifacts.single(artifact), artifact_root=root,
    )


class TestLegacyHierarchyStore:
    """trajectory/step/subtrajectory 层级库：检测、关系、正文、凭据边界。"""

    def test_detect(self, tmp_path):
        artifact, root = _capture_legacy_hierarchy(tmp_path)
        assert registry.detect_family("antigravity", artifact, artifact_root=root) is True

    def test_hierarchy_relations(self, tmp_path):
        artifact, root = _capture_legacy_hierarchy(tmp_path)
        result = registry.adapt_for(
            "antigravity", artifacts.single(artifact), artifact_root=root,
        )
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        rels = {r.relation_kind for r in result.relations}
        assert RelationKind.PARENT_CHILD in rels

    def test_exact_message_content_is_not_stored_as_summary(self, tmp_path):
        artifact, root = _capture_legacy_hierarchy(tmp_path)
        result = registry.adapt_for(
            "antigravity", artifacts.single(artifact), artifact_root=root,
        )
        message = next(
            event for event in result.events if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "antigravity prompt"
        assert message.summary is None

    def test_canary_never_in_events(self, tmp_path):
        artifact, root = _capture_legacy_hierarchy(tmp_path)
        result = registry.adapt_for(
            "antigravity", artifacts.single(artifact), artifact_root=root,
        )
        assert artifacts.CANARY not in artifacts.event_text(result)

    def test_legacy_non_message_body_lands_in_content(self, tmp_path):
        """非消息 kind 的 legacy 正文过去只进 2048 摘要，现在必须进 content。"""
        result = _adapt_legacy_hierarchy(tmp_path, long_non_message=LONG_LEGACY_BODY)
        call = _only(result, EventKind.TOOL_CALL)
        assert call.content == LONG_LEGACY_BODY

    def test_legacy_subtrajectory_body_lands_in_content(self, tmp_path):
        """子轨迹正文过去只进 2048 摘要，现在必须进 content。"""
        result = _adapt_legacy_hierarchy(tmp_path, long_subtrajectory=LONG_LEGACY_BODY)
        boundary = _only(result, EventKind.SUBAGENT_BOUNDARY)
        assert boundary.content == LONG_LEGACY_BODY


# ------------------------------------------------------- live 会话时间边界


class TestLiveSessionTimeBounds:
    """live 的 trajectory_meta 没有时间列：会话跨度取事件 occurred_at 的极值。"""

    def test_session_bounds_are_event_min_max(self, tmp_path):
        epoch_first, epoch_middle, epoch_last = 1788678250, 1788678400, 1788678600
        db = fixtures.live_db(tmp_path, [
            (14, fixtures.user_step("first prompt", epoch=epoch_first)),
            (15, fixtures.assistant_reply_step("middle reply", epoch=epoch_middle)),
            (14, fixtures.user_step("last prompt", epoch=epoch_last)),
        ])
        result = _capture_live(tmp_path, db)
        assert len(result.sessions) == 1
        session = result.sessions[0]
        timed = [e.occurred_at for e in result.events if e.occurred_at]
        assert session.started_at == min(timed) == fixtures.iso_utc(epoch_first)
        assert session.ended_at == max(timed) == fixtures.iso_utc(epoch_last)

    def test_session_without_timed_events_stays_empty_and_warns(self, tmp_path):
        # 载荷不是合法 protobuf、解码不出时间：会话时间保持 None 且告警说明。
        db = fixtures.live_db(tmp_path, [(0, fixtures.TRUNCATED_PAYLOAD)])
        result = _capture_live(tmp_path, db)
        session = result.sessions[0]
        assert session.started_at is None
        assert session.ended_at is None
        assert any("no event carrying a timestamp" in w for w in result.warnings)


class TestLegacyMissingCreatedAt:
    def test_trajectory_without_created_at_column_warns_not_crashes(self, tmp_path):
        # 漂移 schema 的 trajectories 表没有 created_at 列：直接索引 sqlite3.Row
        # 会抛 IndexError；现在置空并在 warnings 里说明，绝不静默吞掉。
        db = fixtures.legacy_db_without_created_at(tmp_path)
        result = registry.adapt_for(
            "antigravity", artifacts.single(fixtures.legacy_artifact(db)),
            artifact_root=tmp_path,
        )
        assert len(result.sessions) == 1
        session = result.sessions[0]
        assert session.started_at is None
        assert any("created_at" in w for w in result.warnings)
        # step 时间列存在时事件时间戳不受影响。
        step_events = [e for e in result.events if e.kind is EventKind.USER_MESSAGE]
        assert step_events and step_events[0].occurred_at == "2026-09-16T03:37:12Z"
