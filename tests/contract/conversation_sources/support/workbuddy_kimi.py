"""workbuddy / kimi / kimi-work 的模块原生夹具与选择器。

一个生产模块（``personal_knowledge.adapters.conversation_sources.workbuddy_kimi``）
服务三家客户端，并且模块级常量 ``ADAPTER_VERSION`` 只有一份 —— 一次修复只升一个
版本号。所以三家的夹具、记录形状与选择器集中在这一个 support 文件里，测试文件
之间不互相 import。

夹具只构造 ``SourceArtifact`` 与合成记录：正文全是假句子，不复制真实对话、
凭据或账号。适配结果一律由测试侧经 ``registry`` 取得，这里不碰生产模块函数。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifact,
    SourceArtifactSet,
)
from tests.contract.conversation_sources.support import artifacts

# 本模块服务的家族（共享同一个 ADAPTER_VERSION）。
FAMILIES = ("workbuddy", "kimi", "kimi-work")
# 共用 old-wire / envelope 两种记录形状的两家客户端。
LOOP_FAMILIES = ("kimi", "kimi-work")


def wire(
    tmp_path: Path,
    label: str,
    relative_path: str,
    records: list[dict] | str | bytes,
    *,
    family: str,
) -> tuple[SourceArtifact, Path]:
    """一份原生 JSONL 记录 -> ``(artifact, artifact_root)``。

    ``label`` 在该次适配的 artifact set 内必须唯一（它决定 blob 名与 artifact id）。
    ``records`` 传 list[dict] 时按 JSONL 编码；传 str/bytes 时原样落盘，
    用于坏 JSON / 非紧凑缩进等畸形字节夹具。
    """
    raw = artifacts.jsonl(records) if isinstance(records, list) else records
    return artifacts.file_artifact(
        tmp_path, label, relative_path, raw, family=family,
    )


def wire_set(
    tmp_path: Path,
    items: list[tuple[str, str, list[dict]]],
    *,
    family: str,
) -> tuple[SourceArtifactSet, Path]:
    """多份原生 JSONL 记录 -> ``(SourceArtifactSet, artifact_root)``。

    ``items`` 是 ``(label, relative_path, records)`` 三元组；label 仍须唯一。
    ``records`` 可为 list[dict]（JSONL 编码）或 str/bytes（原样落盘，用于
    坏 JSON / 非紧凑缩进等畸形子代理夹具）。
    用于同批提交主会话 + 子代理 artifact 的布局回归。
    """
    set_artifacts: list[SourceArtifact] = []
    root: Path | None = None
    for label, relative_path, records in items:
        artifact, root = wire(tmp_path, label, relative_path, records, family=family)
        set_artifacts.append(artifact)
    assert root is not None and set_artifacts
    return SourceArtifactSet(tuple(set_artifacts)), root


def record_events(result):
    """结果里除会话级生命周期锚点（locator 以 ``#session`` 结尾）之外的事件。"""
    return [
        event for event in result.events
        if not (event.provenance.native_locator or "").endswith("#session")
    ]


def oversized_text(prefix: str, length: int) -> str:
    """显著超过适配器旧内容上限（工具 50k / 产出与推理 100k / 未知 10k）的正文。"""
    unit = prefix + "-"
    return (unit * (length // len(unit) + 1))[:length]


BIG_TOOL_INPUT = oversized_text("fixture-wbk-arg", 60_000)
BIG_TOOL_OUTPUT = oversized_text("fixture-wbk-out", 120_000)
BIG_REASONING = oversized_text("fixture-wbk-reasoning", 120_000)
BIG_UNKNOWN = oversized_text("fixture-wbk-unknown", 20_000)
BIG_MESSAGE = oversized_text("fixture-wbk-message", 5_000)
BIG_SPLICED = oversized_text("fixture-wbk-spliced", 5_000)
BIG_NESTED = oversized_text("fixture-wbk-nested", 5_000)


def oversize_loop_records() -> list[dict]:
    """kimi / kimi-work 信封流：工具入参/产出、spliced 与未知记录均超限。"""
    def envelope(seq: int, etype: str, payload: dict) -> dict:
        return {
            "kind": "event",
            "seq": seq,
            "envelope": {
                "type": etype, "seq": seq, "session_id": "fixture-session",
                "timestamp": f"2026-01-01T00:00:0{seq}Z",
                "payload": payload,
            },
        }

    return [
        envelope(1, "tool.call.started", {
            "toolCallId": "call-big", "name": "fixture-tool", "args": BIG_TOOL_INPUT,
        }),
        envelope(2, "tool.result", {
            "toolCallId": "call-big", "output": {"text": BIG_TOOL_OUTPUT},
        }),
        envelope(3, "context.spliced", {
            "messages": [{"role": "user", "content": BIG_SPLICED}],
        }),
        envelope(4, "cron.fired", {"note": BIG_UNKNOWN}),
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "text": BIG_NESTED},
            "time": 1_700_000_000_900,
        },
    ]


def oversize_workbuddy_records() -> list[dict]:
    """workbuddy 扁平记录：消息、推理、工具入参/产出与未知记录均超限。"""
    return [
        {"type": "message", "role": "user", "text": BIG_MESSAGE},
        {"type": "reasoning", "content": [],
         "rawContent": [{"type": "reasoning_text", "text": BIG_REASONING}]},
        {"type": "function_call", "arguments": BIG_TOOL_INPUT},
        {"type": "function_call_result", "output": {"text": BIG_TOOL_OUTPUT}},
        {"type": "mystery-record", "blob": BIG_UNKNOWN},
    ]


def oversize_wire(
    tmp_path: Path,
    label: str,
    records: list[dict],
    *,
    family: str,
) -> tuple[SourceArtifact, Path]:
    """超限正文记录 -> ``(artifact, artifact_root)``。"""
    return wire(
        tmp_path, label, f"sessions/fixture/{label}.jsonl", records, family=family,
    )


def content_index(events) -> dict:
    """有正文的事件按正文索引。"""
    return {event.content: event for event in events if event.content}


def event_containing(events, needle: str):
    """第一条 content / summary / disposition reason 里出现 ``needle`` 的事件。"""
    for event in events:
        blob = " ".join((
            event.content or "",
            event.summary or "",
            artifacts.reasons(event),
        ))
        if needle in blob:
            return event
    return None
