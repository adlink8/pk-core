"""copilot 家族原生 JSONL trace 夹具 builder（合成、脱敏）。

一个生产模块一个夹具模块；跨模块复用的 builder 才放 ``support/artifacts.py``。
这里只构造 copilot 原生的 dotted 事件流：正文全为合成占位句，绝不复制真实
会话正文、凭据或邮箱。
"""

from __future__ import annotations

from pathlib import Path

from tests.contract.conversation_sources.support import artifacts

# 合成正文常量：夹具与断言共用同一份，避免魔法字符串在两侧漂移。
USER_TEXT = "fixture-user-text"
ASSISTANT_TEXT = "fixture-assistant-text"
REASONING_TEXT = "fixture-reasoning-text"
COMPACTION_TEXT = "fixture-compaction-summary"
ABORT_REASON = "fixture-abort-reason"
NOTIFICATION_TEXT = "fixture-notification"
SYSTEM_TEXT = "fixture-system-message"
ERROR_TEXT = "fixture-session-error"

# 源槽相对路径：适配器把它写进 provenance locator。
RELATIVE_PATH = "session-state/fixture/events.jsonl"

# 超限合成正文：显著超过适配器旧的内容上限（工具入参 50k / 工具产出 100k），
# 用来断言正文逐字进 content，而不是被裁到上限。
def oversized_text(prefix: str, length: int) -> str:
    unit = prefix + "-"
    return (unit * (length // len(unit) + 1))[:length]


BIG_TOOL_INPUT = oversized_text("fixture-tool-arg", 60_000)
BIG_TOOL_OUTPUT = oversized_text("fixture-tool-out", 120_000)


def tool_records() -> list[dict]:
    """一对超限的工具调用/产出记录，共用原生 tool id。"""
    return [
        {
            "type": "tool.execution_start",
            "id": "t1",
            "timestamp": "2026-09-01T00:00:03Z",
            "data": {
                "sessionId": "sess-fixture",
                "toolId": "tool-1",
                "toolName": "fixture-tool",
                "arguments": BIG_TOOL_INPUT,
            },
        },
        {
            "type": "tool.execution_complete",
            "id": "t1-done",
            "timestamp": "2026-09-01T00:00:04Z",
            "data": {
                "sessionId": "sess-fixture",
                "toolId": "tool-1",
                "result": BIG_TOOL_OUTPUT,
            },
        },
    ]


def captured_trace_with_big_tools(tmp_path: Path):
    """抓一份只含超限工具记录的合成 trace，返回 ``(artifact, artifact_root)``。"""
    src = tmp_path / "source-big" / "events.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(artifacts.jsonl(tool_records()), encoding="utf-8")
    return artifacts.captured_file(
        src,
        tmp_path / "capture-big",
        relative_path=RELATIVE_PATH,
        byte_limit=1_000_000,
        count_limit=1,
    )


def trace_records() -> list[dict]:
    """一份最小 vscode-copilot 点号事件流。

    覆盖消息、``reasoningText``、compaction 摘要，以及正文散布在
    ``content`` / ``message`` / ``reason`` 三个字段上的控制记录，外加一条
    完全没有正文的 ``session.truncation``。
    """
    return [
        {
            "type": "user.message",
            "id": "u1",
            "timestamp": "2026-09-01T00:00:00Z",
            "data": {"sessionId": "sess-fixture", "content": USER_TEXT},
        },
        {
            "type": "assistant.message",
            "id": "a1",
            "timestamp": "2026-09-01T00:00:01Z",
            "data": {
                "sessionId": "sess-fixture",
                "content": ASSISTANT_TEXT,
                "reasoningText": REASONING_TEXT,
            },
        },
        {
            "type": "session.compaction",
            "id": "c1",
            "timestamp": "2026-09-01T00:00:02Z",
            "data": {
                "sessionId": "sess-fixture",
                "summaryContent": COMPACTION_TEXT,
            },
        },
        {"type": "abort", "id": "ab1", "data": {"reason": ABORT_REASON}},
        {
            "type": "system.notification",
            "id": "n1",
            "data": {"message": NOTIFICATION_TEXT},
        },
        {
            "type": "system.message",
            "id": "sm1",
            "data": {"content": SYSTEM_TEXT},
        },
        {
            "type": "session.error",
            "id": "e1",
            "data": {"message": ERROR_TEXT},
        },
        {"type": "session.truncation", "id": "t1", "data": {}},
    ]


def captured_trace(tmp_path: Path):
    """经真实 capture seam 抓一份合成 trace，返回 ``(artifact, artifact_root)``。"""
    src = tmp_path / "source" / "events.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(artifacts.jsonl(trace_records()), encoding="utf-8")
    return artifacts.captured_file(
        src,
        tmp_path / "capture",
        relative_path=RELATIVE_PATH,
        byte_limit=1_000_000,
        count_limit=1,
    )
