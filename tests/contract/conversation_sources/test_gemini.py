"""Gemini 适配器契约（family ``gemini``，ADAPTER_VERSION 1.1.0）。

公开 seam 只有 registry：``registry.adapt_for`` / ``registry.detect_family``；
发现侧用 ``discovery.discover_client_sources``。生产代码不直接摸族模块函数。

夹具与构造器在 ``support/gemini.py``，通用断言助手在 ``support/artifacts.py``；
正文一律合成。材料搬运自 ``test_antigravity_gemini_record_coverage.py`` 的
gemini 部分（2 个用例）。
"""

from __future__ import annotations

import json
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.discovery import (
    discover_client_sources,
)
from personal_knowledge.core.conversation_events import EventKind
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import gemini as fixtures

USER_TEXT = "合成用户问题"
ASSISTANT_TEXT = "合成助手回答"
THOUGHT_SUBJECT = "合成计划"
THOUGHT_DESCRIPTION = "合成推理步骤"
INFO_TEXT = "合成信息消息"
ERROR_TEXT = "合成错误消息"


def _adapt_gemini(tmp_path: Path, document: dict):
    """写一份单 JSON 文档并经 registry seam 适配。"""
    artifact = fixtures.write_document(tmp_path, document)
    return registry.adapt_for(
        "gemini", artifacts.single(artifact), artifact_root=tmp_path,
    )


def test_gemini_user_list_assistant_string_thoughts_and_typed_info_error(
    tmp_path: Path,
) -> None:
    document = fixtures.messages_document(
        [
            fixtures.user_message(
                USER_TEXT, native_id="u1", timestamp="2026-04-22T07:23:00Z",
            ),
            fixtures.model_message(
                ASSISTANT_TEXT, native_id="g1", timestamp="2026-04-22T07:23:01Z",
                thoughts=[{
                    "subject": THOUGHT_SUBJECT,
                    "description": THOUGHT_DESCRIPTION,
                    "timestamp": "2026-04-22T07:23:01Z",
                }],
            ),
            {
                "id": "i1", "type": "info",
                "timestamp": "2026-04-22T07:23:02Z", "content": INFO_TEXT,
            },
            {
                "id": "e1", "type": "error",
                "timestamp": "2026-04-22T07:23:03Z", "content": ERROR_TEXT,
            },
        ],
        session_id="s-1",
    )
    result = _adapt_gemini(tmp_path, document)
    user = next(event for event in result.events if event.kind is EventKind.USER_MESSAGE)
    assistant = next(
        event for event in result.events if event.kind is EventKind.ASSISTANT_MESSAGE
    )
    assert user.content == USER_TEXT
    assert assistant.content == ASSISTANT_TEXT

    thought_events = [
        event for event in result.events
        if event is not assistant and THOUGHT_DESCRIPTION in artifacts.body(event)
    ]
    assert thought_events, "thoughts 被丢掉了"
    thought_reasons = " ".join(artifacts.reasons(event) for event in result.events)
    thought_summaries = " ".join(event.summary or "" for event in result.events)
    assert "thoughts" in thought_reasons or "thoughts" in thought_summaries

    info = next(event for event in result.events if INFO_TEXT in artifacts.body(event))
    error = next(event for event in result.events if ERROR_TEXT in artifacts.body(event))
    assert "type=info" in artifacts.reasons(info) or "type=info" in (info.summary or "")
    assert "type=error" in artifacts.reasons(error) or "type=error" in (error.summary or "")


def test_discovery_keeps_only_gemini_tmp_session_files(tmp_path: Path) -> None:
    content = json.dumps(
        fixtures.messages_document([fixtures.user_message("合成发现句")]),
        ensure_ascii=False,
    )
    gemini_root = tmp_path / ".gemini"
    session = gemini_root / "tmp" / "proj" / "chats" / "session-abc.json"
    session.parent.mkdir(parents=True)
    session.write_text(content, encoding="utf-8")
    decoy = gemini_root / "tmp" / "proj" / "chats" / "other.json"
    decoy.write_text(content, encoding="utf-8")
    outside = gemini_root / "kept.json"
    outside.write_text(content, encoding="utf-8")

    found = discover_client_sources(roots={"gemini": (gemini_root,)})
    assert session in found["gemini"]
    assert decoy not in found["gemini"]
    assert outside in found["gemini"]

    # 其他家族仍然跳过整个 tmp 目录（例外只给 gemini）。
    codex_root = tmp_path / ".codex"
    visible = codex_root / "sessions" / "rollout.jsonl"
    hidden = codex_root / "tmp" / "rollout.jsonl"
    visible.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    line = json.dumps({"type": "session_meta", "session_id": "s"}) + "\n"
    visible.write_text(line, encoding="utf-8")
    hidden.write_text(line, encoding="utf-8")
    codex_found = discover_client_sources(roots={"codex": (codex_root,)})
    assert visible in codex_found["codex"]
    assert hidden not in codex_found["codex"]
