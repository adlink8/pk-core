"""Grok 家族（生产模块 ``grok``，ADAPTER_VERSION 1.4.0）原生夹具 builder。

只构造合成 / 脱敏字节，正文全是假句子，不读权威库。测试文件经 registry
seam 消费这些产物，所以这里不 import 任何测试文件。
"""

from __future__ import annotations

import json
from pathlib import Path

# 目录捕获 allowlist：role 信封 transcript + compaction + subagents。
GROK_DIRECTORY_INCLUDE = (
    "summary.md",
    "chat_history.jsonl",
    "compaction.md",
    "subagents.json",
)

# 原生 ``type`` 打标 transcript（无 ``role`` 信封）。
TYPED_TRANSCRIPT_INCLUDE = ("summary.json", "chat_history.jsonl")

# 一段完整会话目录：summary + 原生 transcript + 事件侧信道。
SESSION_INCLUDE = ("summary.json", "chat_history.jsonl", "events.jsonl")

# 整段会话目录夹具的合成正文。
SESSION_ID = "fixture-grok-session-1"
SESSION_USER_TEXT = "把这句话留下来。"
SESSION_ASSISTANT_TEXT = "这句话留下来了。"
SESSION_TOOL_TEXT = "工具回了一句假话。"
SESSION_REASONING_SUMMARY = "先想了一下。"


def make_grok_directory(base: Path) -> None:
    """summary.md + ``role`` 信封 transcript + compaction + subagents。"""
    base.mkdir(parents=True, exist_ok=True)
    (base / "summary.md").write_text(
        "# Summary\ngrok_session_s1\nA grok session summary\n", encoding="utf-8"
    )
    (base / "chat_history.jsonl").write_text(
        '{"id":"g1","role":"user","content":"grok prompt","timestamp":"2026-07-01T10:00:00Z"}\n'
        '{"id":"g2","role":"assistant","content":"grok answer","timestamp":"2026-07-01T10:00:01Z"}\n',
        encoding="utf-8",
    )
    (base / "compaction.md").write_text(
        "# Compaction\nCompacted earlier turns.\n", encoding="utf-8"
    )
    (base / "subagents.json").write_text(
        json.dumps(
            [{"id": "sub1", "name": "grok-sub", "created_at": "2026-07-01T10:00:02Z"}]
        )
        + "\n",
        encoding="utf-8",
    )


def make_typed_grok_directory(base: Path) -> None:
    """原生 ``type`` 打标 transcript：多 KB system 前缀 + typed parts。

    真实 ``chat_history.jsonl`` 以多 KB system prompt 开头、每条记录打 ``type``
    而不是 ``role``；老实现用 ``role`` 信封和 512 字节探针窗口读它，整段
    transcript 在发现阶段就被排除，会话退化成无正文的 summary 壳。
    """
    base.mkdir(parents=True, exist_ok=True)
    (base / "summary.json").write_text(
        json.dumps({
            "info": {"id": "01a05b59-typed-0000-0000-000000000000", "cwd": "D:\\proj"},
            "session_summary": "typed transcript",
            "created_at": "2026-07-01T10:00:00Z",
            "updated_at": "2026-07-01T10:00:09Z",
            "num_messages": 5,
            "num_chat_messages": 4,
            "current_model_id": "grok-4.6-build",
        }),
        encoding="utf-8",
    )
    (base / "chat_history.jsonl").write_text(
        json.dumps({"type": "system", "content": "system preamble " * 200}) + "\n"
        + json.dumps({"type": "user", "content": [{"type": "text", "text": "typed prompt"}]}) + "\n"
        + json.dumps({
            "type": "assistant",
            "content": "typed answer",
            "tool_calls": [{
                "id": "call-1", "name": "read_file",
                "arguments": json.dumps({"target_file": "a.md"}),
            }],
            "model_id": "grok-4.6-build",
        }) + "\n"
        + json.dumps({"type": "tool_result", "tool_call_id": "call-1", "content": "file body"}) + "\n"
        + json.dumps({
            "type": "reasoning", "id": "rs-1",
            "summary": [{"type": "summary_text", "text": "why"}],
            "encrypted_content": "CIPHERTEXT",
        }) + "\n",
        encoding="utf-8",
    )


def make_session_directory(directory: Path) -> None:
    """一个会话目录：summary + 原生 transcript + 事件侧信道。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps({
            "info": {"id": SESSION_ID},
            "session_summary": "假会话",
            "created_at": "2026-07-01T10:00:00Z",
            "updated_at": "2026-07-01T10:00:05Z",
        }),
        encoding="utf-8",
    )
    (directory / "chat_history.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "content": [{"type": "text", "text": SESSION_USER_TEXT}],
            }),
            json.dumps({"type": "assistant", "content": SESSION_ASSISTANT_TEXT}),
            json.dumps({"type": "tool_result", "content": SESSION_TOOL_TEXT}),
            json.dumps({
                "type": "reasoning",
                "id": "rs-fixture",
                "status": "done",
                "summary": [{"type": "summary_text", "text": SESSION_REASONING_SUMMARY}],
                "encrypted_content": "not-a-real-ciphertext",
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    (directory / "events.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-07-01T10:00:01Z", "type": "phase_changed"}),
            json.dumps({"ts": "2026-07-01T10:00:02Z", "type": "tool_started"}),
        ]) + "\n",
        encoding="utf-8",
    )
