"""pi 家族适配器契约（模块 ``pi``，ADAPTER_VERSION 1.4.0）。

registry 是唯一的族级 seam：本文件只经 ``registry.adapt_for`` 调用适配器，
不直接摸 ``pi.adapt``。

夹具见 ``support/pi.py``，是一段合成 JSONL 事件流（session / user /
assistant / 带 image 块的 toolResult），正文全是合成句子。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import EventKind
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
