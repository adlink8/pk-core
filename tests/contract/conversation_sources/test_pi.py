"""pi 家族适配器契约（模块 ``pi``，ADAPTER_VERSION 1.4.0）。

registry 是唯一的族级 seam：本文件只经 ``registry.adapt_for`` 调用适配器，
不直接摸 ``pi.adapt``。

夹具见 ``support/pi.py``，是一段合成 JSONL 事件流（session / user /
assistant / 带 image 块的 toolResult），正文全是合成句子。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind, RelationKind
from tests.contract.conversation_sources.support import artifacts, pi

FAMILY = "pi"


def test_pi_text_blocks_and_imageless_tool_result(tmp_path) -> None:
    """文本块进 content；没有 text 的 image 块必须留下带 reason 的事件。"""
    artifact, root = pi.session_artifact(tmp_path)
    result = registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)

    user = next(event for event in result.events if event.kind is EventKind.USER_MESSAGE)
    assistant = next(event for event in result.events if event.kind is EventKind.ASSISTANT_MESSAGE)
    tool = next(event for event in result.events if event.kind is EventKind.TOOL_RESULT)
    assert user.content == "fixture user block"
    assert assistant.content == "fixture assistant block"
    assert tool.content == "fixture tool text"

    image_events = [
        event for event in result.events
        if "image" in artifacts.reasons(event) and "text" in artifacts.reasons(event)
        and ("没有" in artifacts.reasons(event) or "no text" in artifacts.reasons(event).lower())
    ]
    assert len(image_events) >= 2
    for event in image_events:
        assert "qq" not in (event.content or "")
        assert "rr" not in (event.content or "")


# 探测器是全函数：畸形文件判「不是我」，不得把解析异常抛给发现层（会被记成
# probe_error）。非 UTF-8 / UTF-16 字节 = 写了一半或别的编码落地的会话。
# 期望值是独立字面量 False。
NON_UTF8_LINE = bytes([0xFF, 0xFE, 0x00]) + b'{"type":"conversation"}'
UTF16_LINE = '{"type":"conversation"}'.encode("utf-16")


@pytest.mark.parametrize(
    "name,raw",
    [("session.jsonl", NON_UTF8_LINE), ("session.jsonl", UTF16_LINE)],
)
def test_pi_detector_rejects_malformed_bytes_without_raising(tmp_path, name, raw):
    artifact, root = artifacts.probe_file(tmp_path, name, raw)
    assert registry.detect_family(FAMILY, artifact, artifact_root=root) is False


# ----------------------------------------------------------------- 回归补钉

def _adapt_pi_records(tmp_path, records, *, label="pi.case"):
    artifact, root = artifacts.file_artifact(
        Path(tmp_path), label, "session.jsonl", artifacts.jsonl(records), family="pi",
    )
    return registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)


def test_pi_reasoning_and_tool_arguments_are_never_truncated(tmp_path):
    # P1-18 回归：thinking 正文与 toolCall 参数都是事件正文，正文不设上限。
    long_thinking = "t" * 3_000
    long_argument = "a" * 1_000
    result = _adapt_pi_records(tmp_path, [
        {"type": "session", "id": "pi-sess", "timestamp": "2026-09-01T00:00:00Z"},
        {
            "type": "message",
            "id": "pi-assistant",
            "timestamp": "2026-09-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": long_thinking},
                    {"type": "toolCall", "name": "run",
                     "arguments": {"cmd": long_argument}},
                ],
            },
        },
    ])
    reasoning = next(e for e in result.events if e.kind is EventKind.REASONING)
    assert reasoning.content == long_thinking
    tool_call = next(e for e in result.events if e.kind is EventKind.TOOL_CALL)
    assert long_argument in (tool_call.content or "")


def test_pi_timestamps_normalized_to_utc_z(tmp_path):
    # P2 回归：record timestamp（ISO 带时区偏移 / epoch 毫秒）统一归一化。
    result = _adapt_pi_records(tmp_path, [
        {"type": "session", "id": "pi-sess", "timestamp": "2026-09-01T08:00:00+08:00"},
        {"type": "message", "id": "pi-user", "timestamp": 1782900000123,
         "message": {"role": "user", "content": "fixture pi ts"}},
    ])
    session = result.sessions[0]
    assert session.started_at == "2026-09-01T00:00:00Z"
    assert session.ended_at == "2026-07-01T10:00:00.123Z"
    user = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
    assert user.occurred_at == "2026-07-01T10:00:00.123Z"


def test_pi_tolerates_malformed_jsonl_lines(tmp_path):
    # P2 回归：一行坏 JSON 不再炸掉整个会话——坏行计数进 warnings。
    good_prefix = [
        {"type": "session", "id": "pi-sess", "timestamp": "2026-09-01T00:00:00Z"},
    ]
    good_suffix = [
        {"type": "message", "id": "pi-user", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": "fixture pi survives"}},
    ]
    raw = artifacts.jsonl(good_prefix) + "{broken json\n" + artifacts.jsonl(good_suffix)
    artifact, root = artifacts.file_artifact(
        Path(tmp_path), "pi.tolerant", "session.jsonl", raw, family="pi",
    )
    result = registry.adapt_for(FAMILY, artifacts.single(artifact), artifact_root=root)
    assert any(
        e.kind is EventKind.USER_MESSAGE and e.content == "fixture pi survives"
        for e in result.events
    ), "good lines must survive a corrupt line"
    assert any("malformed" in w and "1" in w for w in result.warnings)


def test_pi_compaction_relation_uses_dict_index(tmp_path):
    # P2 回归（行为钉）：compaction 定位 firstKeptEntryId 得到
    # COMPACTED_RANGE 指向被压缩区间最后一条事件——替代 O(n²) 的 list.index
    # 之后关系必须一个不差。
    result = _adapt_pi_records(tmp_path, [
        {"type": "session", "id": "pi-sess", "timestamp": "2026-09-01T00:00:00Z"},
        {"type": "message", "id": "pi-m1", "message_id": "m1",
         "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": "compacted away"}},
        {"type": "message", "id": "pi-m2", "message_id": "m2",
         "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": "kept"}},
        {"type": "compaction", "id": "pi-c1", "message_id": "c1",
         "firstKeptEntryId": "m2", "summary": "older turns compacted",
         "timestamp": "2026-09-01T00:00:03Z"},
    ])
    relations = [r for r in result.relations
                 if r.relation_kind is RelationKind.COMPACTED_RANGE]
    assert len(relations) == 1
    kept = next(e for e in result.events
                if e.kind is EventKind.ASSISTANT_MESSAGE)
    compacted = next(e for e in result.events
                     if e.kind is EventKind.USER_MESSAGE)
    assert relations[0].target_event_id == compacted.event_id
    assert relations[0].source_event_id != kept.event_id
    compaction_event = next(
        e for e in result.events if e.kind is EventKind.COMPACTION_SUMMARY
    )
    assert relations[0].source_event_id == compaction_event.event_id


def test_pi_detector_scans_beyond_the_first_line(tmp_path):
    # P2 回归：真实导出的第一条记录可能带超大注入正文（>16K），探测器必须
    # 分块扫到后面的 conversation/session 记录，而不是只看首行。
    big_first = json.dumps({
        "type": "message",
        "message": {"role": "user", "content": "m" * 20_000},
    })
    marker = json.dumps({"type": "session", "id": "pi-sess"})
    raw = (big_first + "\n" + marker + "\n").encode("utf-8")
    artifact, root = artifacts.probe_file(tmp_path, "session.jsonl", raw)
    assert registry.detect_family(FAMILY, artifact, artifact_root=root) is True


def test_pi_detector_stops_at_scan_cap(tmp_path):
    # P2 回归：扫描有总量上限——marker 落在上限之外的文件判「不是我」。
    filler = b'{"type":"message","content":"' + b"x" * 300_000 + b'"}\n'
    raw = filler + b'{"type":"session","id":"pi-sess"}\n'
    artifact, root = artifacts.probe_file(tmp_path, "session.jsonl", raw)
    assert registry.detect_family(FAMILY, artifact, artifact_root=root) is False
