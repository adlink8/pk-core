"""codex 原生夹具：一份 rollout JSONL，放齐文档 Codex 表中尚未还原的原生类型。

一个生产模块一个 support 文件；测试文件只经 ``registry`` 族级 seam 消费适配器，
这里负责造夹具并调用 ``registry.adapt_for("codex", …)``。

夹具是手写的最小 JSONL，正文全是合成占位句（``noted the fixture`` /
``keep this user line`` / ``draw a blue square`` …），不复制真实对话，
也不读权威库。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.adapters.conversation_sources import registry
from tests.contract.conversation_sources.support import artifacts

# ----------------------------------------------------------------- 事件原因
# 密文在没有本地密钥时无法还原。测试锁定这句原因，而不是假装能解密。
ENCRYPTED_REASON = "no local key; encrypted_content ciphertext cannot be decrypted"
TOKEN_COUNT_REASON = (
    "token_count is a usage record; missing usage fields: "
    "info, input_tokens, output_tokens"
)
INTER_AGENT_REASON = "inter_agent_communication_metadata has no message body"

# ------------------------------------------------------------- 合成正文/载荷
ENC_REASONING_SUMMARY = "noted the fixture"
COMPACTED_USER_TEXT = "keep this user line"
COMPACTED_ASSISTANT_TEXT = "keep this assistant line"
IMAGE_PROMPT = "draw a blue square"
GOAL_OBJECTIVE = "sort the widgets"

# 每条记录的 1-based 行号：locator 形如 ``<relative_path>#L<n>``。
SESSION_META_LINE = 1
ENCRYPTED_REASONING_LINE = 2
COMPACTED_LINE = 3
IMAGE_GENERATION_LINE = 4
THREAD_GOAL_LINE = 5
TOKEN_COUNT_LINE = 6
TOKEN_USAGE_LINE = 7
INTER_AGENT_LINE = 8

# 夹具落盘的相对路径（artifact.relative_path，也是 locator 前缀）。
COVERAGE_RELATIVE_PATH = "rollout-fixture.jsonl"


def coverage_records() -> list[dict]:
    """一条会话里放齐文档 Codex 表中尚未还原的原生类型。"""
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sess-fixture", "cwd": "/tmp/fixture"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "rs-fixture",
                "summary": [{"type": "summary_text", "text": ENC_REASONING_SUMMARY}],
                "content": None,
                "encrypted_content": "not-a-real-ciphertext",
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "compacted",
            "payload": {
                "message": "stand-in compaction",
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": COMPACTED_USER_TEXT}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": COMPACTED_ASSISTANT_TEXT}
                        ],
                    },
                ],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "image_generation_call",
                "id": "ig-fixture",
                "status": "completed",
                "revised_prompt": IMAGE_PROMPT,
            },
        },
        {
            "timestamp": "2026-01-01T00:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_goal_updated",
                "goal": {"objective": GOAL_OBJECTIVE, "status": "active"},
            },
        },
        {
            "timestamp": "2026-01-01T00:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
                "rate_limits": {"limit_id": "codex"},
            },
        },
        {
            "timestamp": "2026-01-01T00:00:06Z",
            "type": "token_usage_record",
            "payload": {
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 1,
                    "total_tokens": 5,
                },
            },
        },
        {
            "timestamp": "2026-01-01T00:00:07Z",
            "type": "inter_agent_communication_metadata",
            "payload": {"trigger_turn": False},
        },
    ]


def adapt_coverage(tmp_path: Path):
    """写覆盖夹具并经 registry seam 适配，返回 ``(records, result)``。"""
    records = coverage_records()
    artifact, root = artifacts.file_artifact(
        tmp_path,
        "codex.coverage",
        COVERAGE_RELATIVE_PATH,
        artifacts.jsonl(records),
        family="codex",
    )
    result = registry.adapt_for(
        "codex", artifacts.single(artifact), artifact_root=root,
    )
    return records, result


# ------------------------------------------------------------------ assertions

# ------------------------------------------------------------ 超限正文夹具

def oversized_text(prefix: str, length: int) -> str:
    """显著超过适配器旧内容上限（100 000）的合成正文。"""
    unit = prefix + "-"
    return (unit * (length // len(unit) + 1))[:length]


BIG_INPUT = oversized_text("fixture-codex-arg", 120_000)
BIG_OUTPUT = oversized_text("fixture-codex-out", 120_000)
BIG_ITEM_MESSAGE = oversized_text("fixture-codex-item", 120_000)
BIG_EVENT_MESSAGE = oversized_text("fixture-codex-event", 120_000)
BIG_COMPACTED = oversized_text("fixture-codex-compacted", 120_000)
BIG_REASONING = oversized_text("fixture-codex-reasoning", 120_000)
BIG_STDERR = oversized_text("fixture-codex-stderr", 4_000)

OVERSIZE_RELATIVE_PATH = "rollout-oversize-fixture.jsonl"


def oversize_records() -> list[dict]:
    """每条记录都携带超过旧内容上限的正文。"""
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sess-oversize", "cwd": "/tmp/fixture"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-fixture",
                "call_id": "call-fixture",
                "name": "fixture-tool",
                "arguments": BIG_INPUT,
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-fixture",
                "output": BIG_OUTPUT,
            },
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {"type": "agent_message", "id": "am-fixture",
                        "message": BIG_ITEM_MESSAGE},
        },
        {
            "timestamp": "2026-01-01T00:00:04Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": BIG_EVENT_MESSAGE},
        },
        {
            "timestamp": "2026-01-01T00:00:05Z",
            "type": "compacted",
            "payload": {"message": BIG_COMPACTED},
        },
        {
            "timestamp": "2026-01-01T00:00:06Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "id": "rs-big", "content": BIG_REASONING},
        },
        {
            "timestamp": "2026-01-01T00:00:07Z",
            "type": "event_msg",
            "payload": {"type": "exec_command_end", "call_id": "call-fixture",
                        "stderr": BIG_STDERR},
        },
    ]


def adapt_oversize(tmp_path: Path):
    """写超限夹具并经 registry seam 适配，返回 ``(records, result)``。"""
    records = oversize_records()
    artifact, root = artifacts.file_artifact(
        tmp_path,
        "codex.oversize",
        OVERSIZE_RELATIVE_PATH,
        artifacts.jsonl(records),
        family="codex",
    )
    result = registry.adapt_for(
        "codex", artifacts.single(artifact), artifact_root=root,
    )
    return records, result


def native_line(locator: str) -> int | None:
    """locator（``<relative_path>#L<n>``）里的 1-based 行号；没有则 None。"""
    marker = "#L"
    if marker not in locator:
        return None
    digits = []
    for char in locator.split(marker, 1)[1]:
        if char.isdigit():
            digits.append(char)
        else:
            break
    return int("".join(digits)) if digits else None


def covered_lines(result) -> set[int]:
    """结果里出现过的原生行号集合。"""
    return {
        line
        for line in (native_line(event.provenance.native_locator) for event in result.events)
        if line is not None
    }
