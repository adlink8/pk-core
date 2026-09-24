"""claude_qoder 原生夹具：claude 与 qoder 共享同一个生产模块与 ADAPTER_VERSION。

两个家族共用同一套 ``message`` DAG / content-block 记录形状，差异只在
detect 标记与记录类型覆盖（claude 的侧链子代理 vs qoder 的 config /
lifecycle 类记录）。所以它们的用例必须住在同一个测试文件里。

测试文件只经 ``registry`` 族级 seam 消费适配器；这里负责造夹具并调用
``registry.adapt_for(family, …)`` / 供 ``registry.detect_family`` 使用。
夹具全是合成句子，不读权威库。
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import registry
from tests.contract.conversation_sources.support import artifacts

# ------------------------------------------------------------------- 合成正文
# claude 侧链子代理：每一行都带 agentId。
SUBAGENT_AGENT_ID = "agent-fixture-1"
SUBAGENT_SESSION_ID = "s-sub"
SUBAGENT_USER_TEXT = "夹具：子代理请看这段假文本。"
SUBAGENT_ASSISTANT_TEXT = "夹具：子代理回复这句假文本。"

# qoder：没有 isCompactSummary、只有 message 的 jsonl。
NO_COMPACT_SESSION_ID = "s-q"
NO_COMPACT_USER_TEXT = "夹具：没有压缩标记的用户句。"
NO_COMPACT_ASSISTANT_TEXT = "夹具：没有压缩标记的助手句。"

# qoder：未映射的原生类型 + 没有正文的图片块。
UNMAPPED_SESSION_ID = "s-map"
UNMAPPED_DIRECTORY = "D:/fixture/fake-workspace"
UNMAPPED_CAPTION = "夹具：图片说明这句是假的。"
UNMAPPED_USER_TEXT = "夹具：用户句旁边有一张没有正文的图。"
UNMAPPED_TYPE_NAMES = (
    "active-leaf",
    "runtime-config",
    "workspace-directories",
    "worktree-state",
    "image",
)

# qoder：last-prompt 没有 message 时靠 lastPrompt 保留正文。
LAST_PROMPT_SESSION_ID = "s-lp"
LAST_PROMPT_AGENT_ID = "agent-fixture-lp"
LAST_PROMPT_TEXT = "夹具：上一次提示只有这句假文本。"
LAST_PROMPT_AGENT_TEXT = "夹具：子代理在 last-prompt 旁边的假回复。"

# qoder：同一文件内 epoch 毫秒整数与 ISO 串混排（真实导出的时间形状）。
# 时间值被断言，正文不被断言，故正文用合成占位句。
QODER_EPOCH_MS = 1780575834000
QODER_ISO_1 = "2026-06-04T12:23:57.053Z"
QODER_ISO_2 = "2026-06-04T12:23:58.900Z"
QODER_ISO_3 = "2026-06-04T12:24:10.000Z"
MIXED_TIMESTAMP_SESSION_ID = "s1"
MIXED_TIMESTAMP_USER_TEXT = "夹具：清洗步骤这句是假的。"
MIXED_TIMESTAMP_ASSISTANT_TEXT = "夹具：先看数据源这句是假的。"
MIXED_TIMESTAMP_COMPACT_TEXT = "compacted"

# 规范化后的时间必须是 UTC ISO Z 串。
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$")

# 超限合成正文：显著超过适配器旧的内容上限（附件/入参 50k、产出/推理 100k）。
def oversized_text(prefix: str, length: int) -> str:
    unit = prefix + "-"
    return (unit * (length // len(unit) + 1))[:length]


BIG_ATTACHMENT = oversized_text("fixture-attachment", 60_000)
BIG_TOOL_INPUT = oversized_text("fixture-tool-arg", 60_000)
BIG_TOOL_OUTPUT = oversized_text("fixture-tool-out", 120_000)
BIG_REASONING = oversized_text("fixture-reasoning", 120_000)

OVERSIZE_SESSION_ID = "s-big"


def oversize_records() -> list[dict]:
    """附件、工具入参/产出与推理正文全部超过旧的内容上限。"""
    return [
        {
            "type": "attachment",
            "uuid": "att-1",
            "parentUuid": None,
            "sessionId": OVERSIZE_SESSION_ID,
            "timestamp": "2026-01-04T00:00:00Z",
            "attachment": {"type": "file-diff", "addedLines": [BIG_ATTACHMENT]},
        },
        {
            "type": "assistant",
            "uuid": "a-big",
            "parentUuid": "att-1",
            "sessionId": OVERSIZE_SESSION_ID,
            "timestamp": "2026-01-04T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": BIG_REASONING},
                    {"type": "tool_use", "id": "call-big", "name": "fixture-tool",
                     "input": {"command": BIG_TOOL_INPUT}},
                    {"type": "tool_result", "tool_use_id": "call-big",
                     "content": BIG_TOOL_OUTPUT},
                ],
            },
        },
    ]


def iso_utc(epoch_seconds: int) -> str:
    """epoch 秒 -> ``YYYY-MM-DDTHH:MM:SSZ``。"""
    return datetime.datetime.fromtimestamp(
        epoch_seconds, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------- 夹具

def subagent_records() -> list[dict]:
    """整份文件每一行都带 agentId，且主会话不可解析（纯侧链）。"""
    return [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "agentId": SUBAGENT_AGENT_ID,
            "sessionId": SUBAGENT_SESSION_ID,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": SUBAGENT_USER_TEXT}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "agentId": SUBAGENT_AGENT_ID,
            "sessionId": SUBAGENT_SESSION_ID,
            "timestamp": "2026-01-01T00:00:01Z",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": SUBAGENT_ASSISTANT_TEXT}],
            },
        },
    ]


def no_compact_records() -> list[dict]:
    """没有 isCompactSummary、但有 message 信封的 qoder jsonl。"""
    return [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": NO_COMPACT_SESSION_ID,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": NO_COMPACT_USER_TEXT}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": NO_COMPACT_SESSION_ID,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": NO_COMPACT_ASSISTANT_TEXT}],
            },
        },
    ]


def unmapped_records() -> list[dict]:
    """qoder 未映射的原生类型 + 一张没有正文、只有 caption 的图片。"""
    return [
        {
            "type": "active-leaf",
            "uuid": "al",
            "parentUuid": None,
            "sessionId": UNMAPPED_SESSION_ID,
            "leafUuid": "leaf-1",
            "explicit": False,
        },
        {
            "type": "runtime-config",
            "uuid": "rc",
            "parentUuid": "al",
            "sessionId": UNMAPPED_SESSION_ID,
            "model": "fixture-model",
            "timestamp": "2026-01-02T00:00:00Z",
        },
        {
            "type": "worktree-state",
            "uuid": "wt",
            "parentUuid": "rc",
            "sessionId": UNMAPPED_SESSION_ID,
            "worktreeSession": None,
        },
        {
            "type": "workspace-directories",
            "uuid": "wd",
            "parentUuid": "wt",
            "sessionId": UNMAPPED_SESSION_ID,
            "directories": [UNMAPPED_DIRECTORY],
        },
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": "wd",
            "sessionId": UNMAPPED_SESSION_ID,
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": UNMAPPED_USER_TEXT},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "aaaa",
                        },
                    },
                    {"type": "image", "text": UNMAPPED_CAPTION, "source": {"type": "url"}},
                ],
            },
        },
    ]


def last_prompt_records() -> list[dict]:
    """last-prompt 记录没有 message 信封，正文只在 lastPrompt 里。"""
    return [
        {
            "type": "last-prompt",
            "uuid": "lp",
            "parentUuid": None,
            "sessionId": LAST_PROMPT_SESSION_ID,
            "lastPrompt": LAST_PROMPT_TEXT,
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "lp",
            "agentId": LAST_PROMPT_AGENT_ID,
            "sessionId": LAST_PROMPT_SESSION_ID,
            "timestamp": "2026-01-03T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": LAST_PROMPT_AGENT_TEXT}],
            },
        },
    ]


def mixed_timestamp_records() -> list[dict]:
    """同一文件内 epoch 毫秒整数与 ISO 串混排（真实 qoder 导出的形状）。"""
    return [
        {"type": "runtime-config", "sessionId": MIXED_TIMESTAMP_SESSION_ID, "model": "auto",
         "timestamp": QODER_EPOCH_MS},
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "timestamp": QODER_ISO_1, "sessionId": MIXED_TIMESTAMP_SESSION_ID,
         "message": {"role": "user",
                     "content": [{"type": "text", "text": MIXED_TIMESTAMP_USER_TEXT}]}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "timestamp": QODER_ISO_2, "sessionId": MIXED_TIMESTAMP_SESSION_ID,
         "message": {"role": "assistant", "model": "auto",
                     "stop_reason": "tool_use",
                     "content": [{"type": "text", "text": MIXED_TIMESTAMP_ASSISTANT_TEXT}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1",
         "isCompactSummary": True,
         "timestamp": QODER_ISO_3, "sessionId": MIXED_TIMESTAMP_SESSION_ID,
         "message": {"role": "user",
                     "content": [{"type": "text", "text": MIXED_TIMESTAMP_COMPACT_TEXT}]}},
    ]


def adapt_family(family: str, tmp_path: Path, name: str, records: list[dict]):
    """经 capture seam 落盘、经 registry seam 适配。

    返回 ``(artifact, artifact_root, result)``；``artifact`` 与
    ``artifact_root`` 供 ``registry.detect_family`` 复用同一份捕获产物。
    """
    src = tmp_path / name
    src.write_text(artifacts.jsonl(records), encoding="utf-8")
    artifact, root = artifacts.captured_file(
        src,
        tmp_path / "capture",
        relative_path=name,
        byte_limit=1_000_000,
        count_limit=1,
    )
    result = registry.adapt_for(
        family, artifacts.single(artifact), artifact_root=root,
    )
    return artifact, root, result


# ------------------------------------------------------------------ assertions

def events_by_session(result) -> dict[str, list]:
    """把事件按 session_id 分组。"""
    grouped: dict[str, list] = defaultdict(list)
    for event in result.events:
        grouped[event.session_id].append(event)
    return grouped


def explained(event) -> str:
    """一条事件的摘要 + 全部 field disposition reason 拼接。"""
    return " ".join(
        part
        for part in (
            event.summary or "",
            *(record.reason for record in event.field_dispositions),
        )
        if part
    )
