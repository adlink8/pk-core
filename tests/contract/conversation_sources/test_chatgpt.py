"""chatgpt 家族适配器契约：AgentsView pathless 兼容通道 + 保真度降级。

本机没有 ChatGPT 的本地文件，适配器只读 **AgentsView 形态的 sqlite**
（pathless 兼容通道）。断言一律经 ``registry`` 的族级 seam（``adapt_for`` /
``detect_family``），不直接调 ``chatgpt.adapt(...)``。
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
)

from tests.contract.conversation_sources.support import artifacts, chatgpt


@pytest.fixture(scope="module")
def pathless(tmp_path_factory):
    """捕获一份 pathless AgentsView 快照并适配一次。"""
    tmp = tmp_path_factory.mktemp("chatgpt")
    db = tmp / "sessions.db"
    chatgpt.make_agentsview_db(
        db,
        sessions=(
            (
                chatgpt.PATHLESS_SESSION_ID, "chatgpt", "2026-07-01T10:00:00Z",
                None, None, None,
            ),
        ),
        messages=(
            (
                "message-1", chatgpt.PATHLESS_SESSION_ID, 1, "user",
                chatgpt.PATHLESS_USER_TEXT, "2026-07-01T10:00:01Z", 0, 0,
            ),
        ),
    )
    artifact, root = chatgpt.captured_agentsview(db, tmp)
    return registry.adapt_for(
        "chatgpt", artifacts.single(artifact), artifact_root=root
    )


def test_family(pathless):
    assert pathless.family == "chatgpt"


def test_detect_binds_to_agentsview(tmp_path):
    """AgentsView 形态的 sqlite 快照是该家族唯一的本地识别锚点。"""
    db = tmp_path / "sessions.db"
    chatgpt.make_agentsview_db(db)
    artifact, root = chatgpt.captured_agentsview(db, tmp_path)
    assert registry.detect_family("chatgpt", artifact, artifact_root=root) is True


def test_native_reconstruction_unavailable(pathless):
    """兼容观测通道不冒充原生重建：保真度停在 partial 并留下警告。"""
    result = pathless
    assert (
        result.fidelity.level(FidelityDimension.SOURCE_AVAILABILITY)
        is FidelityLevel.PARTIAL
    )
    assert (
        result.fidelity.level(FidelityDimension.STRUCTURE_COMPLETENESS)
        is FidelityLevel.PARTIAL
    )
    assert any("native reconstruction unavailable" in w for w in result.warnings)


def test_pathless_session_and_exact_compatibility_message(pathless):
    result = pathless
    assert len(result.sessions) == 1
    assert result.sessions[0].native_session_id == chatgpt.PATHLESS_SESSION_ID
    message = artifacts.event_with(result, EventKind.USER_MESSAGE)
    assert message.content == chatgpt.PATHLESS_USER_TEXT
    assert message.summary is None


def test_chatgpt_sqlite_messages_explain_missing_content(tmp_path):
    db = tmp_path / "sessions.db"
    chatgpt.make_agentsview_db(
        db,
        sessions=(
            (
                chatgpt.FLAT_SESSION_ID, "chatgpt", "2026-09-01T00:00:00Z",
                None, None, None,
            ),
        ),
        messages=(
            (
                "message-filled", chatgpt.FLAT_SESSION_ID, 1, "user",
                chatgpt.FLAT_FILLED_TEXT, "2026-09-01T00:00:01Z", 0, 0,
            ),
            (
                "message-empty", chatgpt.FLAT_SESSION_ID, 2, "assistant",
                None, "2026-09-01T00:00:02Z", 0, 0,
            ),
        ),
        canary=artifacts.CANARY,
    )
    artifact, root = chatgpt.captured_agentsview(db, tmp_path)
    result = registry.adapt_for(
        "chatgpt", artifacts.single(artifact), artifact_root=root
    )

    messages = tuple(
        event
        for event in result.events
        if event.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
    )
    assert len(messages) == 2
    filled = next(
        event for event in messages if event.content == chatgpt.FLAT_FILLED_TEXT
    )
    assert filled.summary is None
    missing = next(event for event in messages if event is not filled)
    assert not (missing.content or "").strip()
    reasons = artifacts.reasons(missing).lower()
    assert "content" in reasons
    assert "missing" in reasons or "缺失" in reasons

    # 隐私边界：允许清单外的凭据表整表排除，哨兵值不得抵达事件。
    assert any("credentials" in d for d in artifact.privacy_dispositions)
    assert artifacts.CANARY not in artifacts.event_text(result)
