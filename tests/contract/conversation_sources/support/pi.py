"""pi 家族原生夹具构建器（模块 ``pi``，ADAPTER_VERSION 1.4.0）。

Pi 导出独立的 JSONL 事件流：``session`` 开场记录、``message`` 记录按 role 分
（``user`` / ``assistant`` / ``toolResult``），工具结果的 ``content`` 里可以只有
``image`` 块而没有 ``text`` —— 这种块必须留下自己的事件与 field disposition，
不能消失。正文全是合成句子，不含真实会话。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from tests.contract.conversation_sources.support import artifacts

# 合成正文常量：断言侧引用同一份，避免魔法字符串在夹具与测试之间漂移。
USER_TEXT = "fixture user block"
ASSISTANT_TEXT = "fixture assistant block"
TOOL_TEXT = "fixture tool text"


def session_records() -> list[dict]:
    """一段合成事件流：session + user + assistant + 带两个 image 块的 toolResult。"""
    return [
        {"type": "session", "id": "pi-sess", "timestamp": "2026-09-01T00:00:00Z"},
        {
            "type": "message",
            "id": "pi-user",
            "timestamp": "2026-09-01T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": USER_TEXT}]},
        },
        {
            "type": "message",
            "id": "pi-assistant",
            "timestamp": "2026-09-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": ASSISTANT_TEXT}],
            },
        },
        {
            "type": "message",
            "id": "pi-tool",
            "timestamp": "2026-09-01T00:00:03Z",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "content": [
                    {"type": "text", "text": TOOL_TEXT},
                    {"type": "image", "data": "qq", "mimeType": "image/png"},
                    {"type": "image", "data": "rr", "mimeType": "image/png"},
                ],
            },
        },
    ]


def session_artifact(
    directory: Path,
    *,
    label: str = "pi.session",
) -> tuple[SourceArtifact, Path]:
    """合成事件流 → 内容寻址 blob → ``(artifact, artifact_root)``。"""
    return artifacts.file_artifact(
        Path(directory), label, "session.jsonl",
        artifacts.jsonl(session_records()), family="pi",
    )
