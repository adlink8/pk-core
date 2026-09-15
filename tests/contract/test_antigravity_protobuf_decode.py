"""Antigravity live-store protobuf decoding contract (adapter 1.2.0).

The live store ships no ``.proto`` schema, but ``steps.step_payload`` is a binary
protobuf ``Step`` message whose wire format is self-describing. These tests build
payloads with the exact field layout observed across 70 real stores and assert the
transcript is *decoded* rather than preserved by reference.

Field map exercised here (see ``antigravity._decode_live_step``):

    step_type 14   f19/f2 user prompt; f19/f11/f1 attachment uri
    step_type 15   f20/f1 assistant reply (f20/f8 mirrors it, so it is skipped);
                   f20/f3 reasoning; f20/f7 tool call
    step_type 132  f5/f4 call reference; f140/f2/f1 tool result
    step_type 17   f24/f3 execution error
    step_type 23   f30/f5 compaction summary
    step_type 101  f114/f2 subagent message
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import antigravity
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifact,
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.protobuf_wire import (
    encode_field,
)
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
    RelationKind,
)

_EPOCH = 1788678250
_NANOS = 928526700
_TRAJECTORY = "2726a18f-ff25-432e-bf16-6870b064dfd5"
_EXPECTED_ISO = datetime.datetime.fromtimestamp(
    _EPOCH, tz=datetime.timezone.utc
).strftime("%Y-%m-%dT%H:%M:%SZ")

_LIVE_SCHEMA = """
CREATE TABLE trajectory_meta (
    trajectory_id text, cascade_id text, trajectory_type integer, source integer,
    PRIMARY KEY (trajectory_id)
);
CREATE TABLE steps (
    idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0,
    has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob,
    error_details blob, permissions blob, task_details blob, render_info blob,
    step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx)
);
CREATE TABLE parent_references (idx integer, data blob, PRIMARY KEY (idx));
"""


# ------------------------------------------------------------------ encoders


def vi(number: int, value: int) -> bytes:
    return encode_field(number, 0, value)


def ld(number: int, payload: bytes) -> bytes:
    return encode_field(number, 2, payload)


def st(number: int, text: str) -> bytes:
    return encode_field(number, 2, text.encode("utf-8"))


def meta(epoch: int = _EPOCH, nanos: int = _NANOS) -> bytes:
    """Field 5: step metadata where f5/f1 = {f1: epoch seconds, f2: nanoseconds}."""
    return ld(1, vi(1, epoch) + vi(2, nanos)) + vi(3, 2) + st(12, "step-uuid")


def step(step_type: int, *bodies: bytes) -> bytes:
    return vi(1, step_type) + vi(4, 3) + ld(5, meta()) + b"".join(bodies)


def user_step(text: str) -> bytes:
    return step(14, ld(19, st(2, text)))


def user_step_with_attachment(text: str, uri: str) -> bytes:
    return step(14, ld(19, ld(11, st(1, uri) + ld(2, st(1, text)))))


def assistant_step(
    reply: str, thinking: str, call_id: str, tool: str, args: str
) -> bytes:
    f20 = (
        st(1, reply)
        + st(3, thinking)
        + ld(7, st(1, call_id) + st(2, tool) + st(3, args))
        + st(8, reply)  # mirror of f1 -- must not duplicate the reply
    )
    return step(15, ld(20, f20))


def tool_execution_step(
    call_id: str, tool: str, args: str, result: str | None = None, *,
    notes: dict | None = None, raw_result: bytes | None = None,
) -> bytes:
    """A step_type=132 payload: f140/f1 annotations plus f140/f2/f1 result."""
    f5 = ld(4, st(1, call_id) + st(2, tool) + st(3, args))
    body = b"".join(ld(1, st(1, key) + st(2, value)) for key, value in (notes or {}).items())
    if raw_result is not None:
        body += ld(2, ld(1, raw_result))
    elif result is not None:
        body += ld(2, st(1, result))
    return vi(1, 132) + vi(4, 3) + ld(5, f5) + ld(140, body)


def error_step() -> bytes:
    f24 = ld(
        3,
        st(1, "Agent execution terminated due to error.")
        + st(2, "FAILED_PRECONDITION (code 400): region blocked")
        + vi(7, 400),
    )
    return step(17, ld(24, f24))


def compaction_step(summary: str) -> bytes:
    return step(23, ld(30, st(5, f"<summary>{summary}</summary>")))


def subagent_step(title: str, body: str) -> bytes:
    return step(101, ld(114, ld(2, st(1, title) + st(2, body)) + st(3, "agent_message")))


# -------------------------------------------------------------------- harness


def _live_db(tmp_path: Path, rows: list[tuple[int, bytes]]) -> Path:
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(_LIVE_SCHEMA)
        con.execute(
            "INSERT INTO trajectory_meta VALUES (?,?,?,?)",
            (_TRAJECTORY, "cascade-1", 4, 1),
        )
        for idx, (step_type, payload) in enumerate(rows):
            con.execute(
                "INSERT INTO steps (idx, step_type, status, step_payload, step_format) "
                "VALUES (?,?,?,?,0)",
                (idx, step_type, 3, payload),
            )
        con.commit()
    finally:
        con.close()
    return db


def _adapt(db: Path):
    artifact = SourceArtifact(
        artifact_id=db.name, family="antigravity", source_kind="sqlite",
        content_hash="h", capture_method="sqlite", relative_path=db.name,
        byte_size=db.stat().st_size,
    )
    return antigravity.adapt(
        SourceArtifactSet(artifacts=(artifact,)), artifact_root=db.parent
    )


def _only(result, kind: EventKind):
    picked = [e for e in result.events if e.kind is kind]
    assert len(picked) == 1, f"expected exactly one {kind.value}, got {len(picked)}"
    return picked[0]


# ---------------------------------------------------------------------- tests


class TestLiveTranscriptDecoded:
    def test_user_prompt_and_timestamp(self, tmp_path):
        db = _live_db(tmp_path, [(14, user_step("不是一共35个岗位吗？"))])
        event = _only(_adapt(db), EventKind.USER_MESSAGE)
        assert event.content == "不是一共35个岗位吗？"
        assert event.occurred_at == _EXPECTED_ISO

    def test_user_attachment_uri_and_prose(self, tmp_path):
        db = _live_db(tmp_path, [(
            14,
            user_step_with_attachment(
                "这个字体间隔太大了",
                "file:///d:/ADLINK/Myproject/career-os/cv.png",
            ),
        )])
        event = _only(_adapt(db), EventKind.USER_MESSAGE)
        assert event.content == "这个字体间隔太大了"
        assert "cv.png" in (event.summary or "")

    def test_assistant_reply_reasoning_and_call(self, tmp_path):
        db = _live_db(tmp_path, [(
            15,
            assistant_step(
                "### 抓取结论先行", "**Determining job listing**", "call_131892",
                "write_to_file", '{"CodeContent":"x"}',
            ),
        )])
        result = _adapt(db)
        reply = _only(result, EventKind.ASSISTANT_MESSAGE)
        assert reply.content == "### 抓取结论先行"
        thinking = _only(result, EventKind.REASONING)
        assert thinking.content == "**Determining job listing**"
        call = _only(result, EventKind.TOOL_CALL)
        assert call.content == '{"CodeContent":"x"}'
        assert call.summary == "write_to_file"

    def test_mirrored_field_does_not_duplicate_reply(self, tmp_path):
        # f20/f8 mirrors f20/f1; only one assistant event may be emitted.
        db = _live_db(tmp_path, [(
            15,
            assistant_step("唯一回复", "t", "call_1", "run_command", "{}"),
        )])
        result = _adapt(db)
        replies = [e for e in result.events if e.kind is EventKind.ASSISTANT_MESSAGE]
        assert len(replies) == 1

    def test_usage_counters_are_reported_as_raw_field_numbers(self, tmp_path):
        # Field census over the 70 real stores: f5/f9 carries f1 (constant
        # 1318) and f6 (constant 24) as configuration, alongside the varying
        # counters f2/f3/f5/f9/f10. Only counters are reported.
        usage_fields = (
            vi(1, 1318) + vi(2, 12443) + vi(3, 346) + vi(5, 4074)
            + vi(6, 24) + vi(9, 432)
        )
        f5 = ld(1, vi(1, _EPOCH) + vi(2, _NANOS)) + ld(9, usage_fields)
        payload = vi(1, 15) + vi(4, 3) + ld(5, f5) + ld(20, st(1, "hi"))
        db = _live_db(tmp_path, [(15, payload)])
        usage = _only(_adapt(db), EventKind.USAGE)
        assert "f2=12443" in usage.summary
        assert "f3=346" in usage.summary
        assert "f5=4074" in usage.summary
        assert "f9=432" in usage.summary
        # constant configuration fields are not counters and must be excluded
        assert "f1=" not in usage.summary
        assert "f6=" not in usage.summary

    def test_tool_result_summary_is_labelled_by_annotations(self, tmp_path):
        db = _live_db(tmp_path, [
            (132, tool_execution_step(
                "call_1", "run_command", "{}",
                "The command exited with code 0.",
                notes={"toolSummary": "List ADLINK directory",
                       "toolAction": "Listing ADLINK directory",
                       "CommandLine": "ls"},
            )),
        ])
        outcome = _only(_adapt(db), EventKind.TOOL_RESULT)
        assert outcome.content == "The command exited with code 0."
        assert "toolSummary=List ADLINK directory" in outcome.summary

    def test_nul_padded_utf16_tool_result_is_recovered(self, tmp_path):
        # PowerShell/wsl.exe on Windows emit UTF-16LE; the store keeps it
        # NUL-padded, which is not "printable" until the padding is dropped.
        text = "\nThe command exited with code 0.\nOutput:\n NAME    STATE\n wsl      RUNNING"
        db = _live_db(tmp_path, [
            (132, tool_execution_step(
                "call_1", "run_command", "{}",
                raw_result=text.encode("utf-16-le"),
            )),
        ])
        outcome = _only(_adapt(db), EventKind.TOOL_RESULT)
        assert outcome.content == text
        assert (
            outcome.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.COMPLETE
        )

    def test_annotation_only_execution_is_kept(self, tmp_path):
        # 99 of 10716 real executions carry annotations but no result text.
        # They must survive as events rather than being dropped as "empty".
        db = _live_db(tmp_path, [
            (15, assistant_step("ls", "t", "call_1", "list_dir", "{}")),
            (132, tool_execution_step(
                "call_1", "list_dir", "{}",
                notes={"toolSummary": "List ADLINK directory",
                       "DirectoryPath": "D:/ADLINK"},
            )),
        ])
        result = _adapt(db)
        outcome = _only(result, EventKind.TOOL_RESULT)
        assert outcome.content is None
        assert "List ADLINK directory" in outcome.summary
        assert "DirectoryPath=D:/ADLINK" in outcome.summary
        disp = outcome.field_dispositions[0]
        assert disp.disposition.value == "preserved_by_reference"
        assert any("annotations" in w for w in result.warnings)
        # the call/result pair is still linked
        assert len(result.relations) == 1

    def test_unrecoverable_result_bytes_are_referenced_not_dropped(self, tmp_path):
        binary = bytes(range(256)) * 2  # valid bytes, not text
        db = _live_db(tmp_path, [
            (132, tool_execution_step(
                "call_1", "run_command", "{}", raw_result=binary,
            )),
        ])
        result = _adapt(db)
        outcome = _only(result, EventKind.TOOL_RESULT)
        assert outcome.content is None
        assert "not recoverable text" in outcome.summary
        assert outcome.native_payload_ref

    def test_tool_result_links_back_to_call(self, tmp_path):
        db = _live_db(tmp_path, [
            (15, assistant_step("run it", "t", "call_131892", "run_command", "{}")),
            (132, tool_execution_step(
                "call_131892", "run_command", "{}", "The command exited with code 0.",
            )),
        ])
        result = _adapt(db)
        outcome = _only(result, EventKind.TOOL_RESULT)
        assert outcome.content == "The command exited with code 0."
        call = _only(result, EventKind.TOOL_CALL)
        assert len(result.relations) == 1
        relation = result.relations[0]
        assert relation.relation_kind is RelationKind.CALL_RESULT
        assert relation.source_event_id == outcome.event_id
        assert relation.target_event_id == call.event_id

    def test_reused_call_id_stays_unique_and_pairs_in_order(self, tmp_path):
        # Real artifacts reuse a call id across steps (call_285840 at idx
        # 826/874 of one store), so the native id must carry the step index and
        # each result must pair with the nearest unclaimed call.
        db = _live_db(tmp_path, [
            (15, assistant_step("first", "t", "call_dup", "run_command", "{}")),
            (132, tool_execution_step("call_dup", "run_command", "{}", "out-1")),
            (15, assistant_step("second", "t", "call_dup", "run_command", "{}")),
            (132, tool_execution_step("call_dup", "run_command", "{}", "out-2")),
        ])
        result = _adapt(db)
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
        db = _live_db(tmp_path, [(17, error_step())])
        event = _only(_adapt(db), EventKind.UNKNOWN_NATIVE)
        assert "Agent execution terminated" in event.summary
        assert "FAILED_PRECONDITION" in event.summary
        assert "400" in event.summary

    def test_compaction_summary_decoded(self, tmp_path):
        db = _live_db(tmp_path, [(23, compaction_step("任务概览"))])
        event = _only(_adapt(db), EventKind.COMPACTION_SUMMARY)
        assert "<summary>" in event.content
        assert "任务概览" in event.content

    def test_compaction_body_without_summary_marker_is_captured(self, tmp_path):
        # 49 of the 75 real summary bodies carry no <summary> tag, so the
        # marker must not be used as the selector.
        body = "# Session Continuation Summary\n\n## 1. Outstanding User Requests"
        db = _live_db(tmp_path, [(23, step(23, ld(30, st(5, body))))])
        event = _only(_adapt(db), EventKind.COMPACTION_SUMMARY)
        assert event.content == body

    def test_compaction_title_only_step_is_not_dropped(self, tmp_path):
        # The 28 body-less steps still hold a session title in f30/f4.
        payload = step(23, ld(
            30,
            st(4, "ADLINK Data Pipeline Analysis")
            + st(15, "file:///C:/brain/x/.system_generated/logs/transcript.jsonl"),
        ))
        db = _live_db(tmp_path, [(23, payload)])
        event = _only(_adapt(db), EventKind.COMPACTION_SUMMARY)
        assert event.content is None
        assert event.summary == "ADLINK Data Pipeline Analysis"

    def test_subagent_message_decoded(self, tmp_path):
        db = _live_db(tmp_path, [(101, subagent_step("标题", "子代理报告正文"))])
        event = _only(_adapt(db), EventKind.SUBAGENT_BOUNDARY)
        assert event.content == "子代理报告正文"
        assert event.summary == "标题"

    def test_content_fidelity_is_complete_when_decoded(self, tmp_path):
        db = _live_db(tmp_path, [
            (14, user_step("问题")),
            (15, assistant_step("回答", "想", "call_1", "run_command", "{}")),
            (132, tool_execution_step("call_1", "run_command", "{}", "输出")),
        ])
        result = _adapt(db)
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
        # A step carrying only metadata yields no content event.
        db = _live_db(tmp_path, [(15, step(15))])
        result = _adapt(db)
        assert [e for e in result.events if e.kind is not EventKind.SESSION_LIFECYCLE] == []
        assert any("no recoverable transcript content" in w for w in result.warnings)

    def test_unmatched_tool_result_is_warned(self, tmp_path):
        db = _live_db(tmp_path, [
            (132, tool_execution_step("call_orphan", "run_command", "{}", "输出")),
        ])
        result = _adapt(db)
        assert result.relations == ()
        assert any("no matching tool call" in w for w in result.warnings)


class TestMalformedPayloadStillPreserved:
    def test_truncated_payload_falls_back_to_reference(self, tmp_path):
        # Declares a 152-byte field but carries only a couple of bytes: the wire
        # format cannot be trusted, so nothing may be decoded from it.
        truncated = bytes([0x08, 0x0E, 0x20, 0x03, 0x2A, 0x98, 0x01, 0x0A, 0x22])
        db = _live_db(tmp_path, [(14, truncated)])
        result = _adapt(db)
        event = _only(result, EventKind.UNKNOWN_NATIVE)
        assert event.field_dispositions[0].disposition.value == "preserved_by_reference"
        assert (
            event.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.UNAVAILABLE
        )
        assert any("not well-formed protobuf" in w for w in result.warnings)

    def test_malformed_payload_does_not_break_other_steps(self, tmp_path):
        truncated = bytes([0x08, 0x0E, 0x2A, 0x98, 0x01])
        db = _live_db(tmp_path, [
            (14, user_step("正常问题")),
            (14, truncated),
        ])
        result = _adapt(db)
        assert _only(result, EventKind.USER_MESSAGE).content == "正常问题"
        assert len([e for e in result.events if e.kind is EventKind.UNKNOWN_NATIVE]) == 1
