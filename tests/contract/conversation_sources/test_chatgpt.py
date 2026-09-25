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


def test_detect_rejects_agentsview_reconcile_scratch_db(tmp_path):
    """AgentView 自己的 reconcile 临时库带 store 前缀，但不是会话库。

    真机 ``~/.agentsview/`` 里除了 store 还有 ``.agentsview-reconcile-<pid>.db``
    （只有一张 ``candidates`` 表）。按子串锚点它们会被认领，于是 chatgpt 的
    发现层认领数虚报成 3（真实是 1），然后在抓取阶段以
    ``sqlite_snapshot:KeyError`` 失败 —— 数据不错，但台账在撒谎。
    """
    name = ".agentsview-reconcile-3235378876.db"
    artifact, root = artifacts.probe_file(
        tmp_path, name, b"", source_kind="sqlite"
    )
    assert registry.detect_family("chatgpt", artifact, artifact_root=root) is False


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


# --------------------------------- 时间戳零归一化补口（P2 收尾）

def test_chatgpt_timestamps_go_through_normalize(tmp_path):
    """AgentsView 行的时间戳可能是 epoch-ms 数字串或非 Z 时区的 ISO。

    它们必须在适配器里归一成规范 UTC ``Z`` 形状，而不是原样透传给
    ``occurred_at`` / ``started_at`` / ``ended_at``。
    """
    db = tmp_path / "sessions.db"
    chatgpt.make_agentsview_db(
        db,
        sessions=(
            # epoch 毫秒数字串 -> 2023-11-14T22:13:20Z
            (chatgpt.PATHLESS_SESSION_ID, "chatgpt", "1700000000000",
             "1700000000500", None, None),
        ),
        messages=(
            # 带非 UTC 时区的 ISO -> 2026-07-01T04:00:00Z
            (
                "message-tz", chatgpt.PATHLESS_SESSION_ID, 1, "user",
                chatgpt.PATHLESS_USER_TEXT, "2026-07-01T12:00:00+08:00", 0, 0,
            ),
        ),
    )
    artifact, root = chatgpt.captured_agentsview(db, tmp_path)
    result = registry.adapt_for(
        "chatgpt", artifacts.single(artifact), artifact_root=root
    )

    session = result.sessions[0]
    assert session.started_at == "2023-11-14T22:13:20Z"
    assert session.ended_at == "2023-11-14T22:13:20.500Z"

    lifecycle = artifacts.event_with(result, EventKind.SESSION_LIFECYCLE)
    assert lifecycle.occurred_at == "2023-11-14T22:13:20Z"

    message = artifacts.event_with(result, EventKind.USER_MESSAGE)
    assert message.occurred_at == "2026-07-01T04:00:00Z"
