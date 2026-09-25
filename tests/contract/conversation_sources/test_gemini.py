"""Gemini 适配器契约（family ``gemini``，ADAPTER_VERSION 1.2.0）。

公开 seam 只有 registry：``registry.adapt_for`` / ``registry.detect_family``；
发现侧用 ``discovery.discover_client_sources``。生产代码不直接摸族模块函数。

夹具与构造器在 ``support/gemini.py``，通用断言助手在 ``support/artifacts.py``；
正文一律合成。材料搬运自 ``test_antigravity_gemini_record_coverage.py`` 的
gemini 部分（2 个用例）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.discovery import (
    discover_client_sources,
)
from personal_knowledge.adapters.conversation_sources import gemini as gemini_module
from personal_knowledge.core.conversation_events import EventContractError, EventKind
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


# --------------------------------------------------------- 批次 B 回归补测

# 独立字面量期望值（不由被测代码同款算法重算）：
#   1745308979000 ms -> 2025-04-22T08:02:59Z
#   1745308980000 ms -> 2025-04-22T08:03:00Z
#   1745308981000 ms == 1745308981 s -> 2025-04-22T08:03:01Z
#   2026-04-22T09:23:03+02:00        -> 2026-04-22T07:23:03Z
def test_gemini_timestamps_normalize_to_utc_z(tmp_path: Path) -> None:
    document = fixtures.messages_document(
        [
            # 原生 epoch 毫秒 int
            {"id": "u1", "type": "user", "content": [{"text": USER_TEXT}],
             "timestamp": 1745308980000},
            # 数字串毫秒
            {"id": "g1", "type": "gemini", "content": ASSISTANT_TEXT,
             "timestamp": "1745308981000"},
            # 10 位秒级纪元（normalize_timestamp 覆盖不了，需本文件预处理）
            {"id": "u2", "type": "user", "content": [{"text": "second"}],
             "timestamp": 1745308981},
            # 带时区偏移的 ISO
            {"id": "g2", "type": "gemini", "content": "third",
             "timestamp": "2026-04-22T09:23:03+02:00"},
        ],
        session_id="s-ts",
        created_at=1745308979000,
    )
    result = _adapt_gemini(tmp_path, document)

    by_native = {
        event.provenance.native_event_id: event for event in result.events
    }
    assert by_native["u1"].occurred_at == "2025-04-22T08:03:00Z"
    assert by_native["g1"].occurred_at == "2025-04-22T08:03:01Z"
    assert by_native["u2"].occurred_at == "2025-04-22T08:03:01Z"
    assert by_native["g2"].occurred_at == "2026-04-22T07:23:03Z"
    # 会话生命线事件的 created_at 同样归一，不得把裸毫秒 int 透传入库。
    lifecycle = artifacts.event_with(result, EventKind.SESSION_LIFECYCLE)
    assert lifecycle.occurred_at == "2025-04-22T08:02:59Z"


def test_gemini_title_skips_system_placeholder_blocks(tmp_path: Path) -> None:
    document = fixtures.messages_document(
        [
            {"id": "u1", "type": "user",
             "content": [{"text": "<INSTRUCTIONS>你是一个编码代理</INSTRUCTIONS>\nAGENTS.md 约定"}]},
            fixtures.user_message(USER_TEXT, native_id="u2"),
        ],
        session_id="s-title",
    )
    result = _adapt_gemini(tmp_path, document)
    assert result.sessions, "有 sessionId 必须产出会话"
    assert result.sessions[0].title == USER_TEXT


def test_gemini_fallback_ids_stable_when_mid_file_message_inserted(tmp_path: Path) -> None:
    """无原生 id 的消息：中部补一条消息不许让后续兜底 id 全部轮换。"""

    def build(root: Path, extra: list[dict]):
        messages = [
            {"type": "user", "content": [{"text": "alpha"}],
             "timestamp": "2026-04-22T07:23:00Z"},
            *extra,
            {"type": "user", "content": [{"text": "beta"}],
             "timestamp": "2026-04-22T07:23:05Z"},
        ]
        document = fixtures.messages_document(messages, session_id="s-ids")
        artifact = fixtures.write_document(root, document)
        return registry.adapt_for(
            "gemini", artifacts.single(artifact), artifact_root=root,
        )

    base = build(tmp_path / "base", [])
    with_extra = build(tmp_path / "extra", [
        {"type": "user", "content": [{"text": "inserted"}],
         "timestamp": "2026-04-22T07:23:02Z"},
    ])

    def native_ids(result):
        return {
            event.content: event.provenance.native_event_id
            for event in result.events
            if event.kind is EventKind.USER_MESSAGE
        }

    base_ids = native_ids(base)
    assert set(base_ids) == {"alpha", "beta"}
    # 中部插入只新增自己的 id，alpha/beta 的兜底 id 原样不动（路径无关、
    # 位置无关）。
    for text, native_id in base_ids.items():
        assert native_ids(with_extra)[text] == native_id
        assert native_id.startswith("msg-") and native_id[:5] != "msg-0"


def test_gemini_duplicate_identical_messages_get_distinct_ids(tmp_path: Path) -> None:
    document = fixtures.messages_document(
        [
            {"type": "user", "content": [{"text": "same"}],
             "timestamp": "2026-04-22T07:23:00Z"},
            {"type": "user", "content": [{"text": "same"}],
             "timestamp": "2026-04-22T07:23:00Z"},
        ],
        session_id="s-dup",
    )
    result = _adapt_gemini(tmp_path, document)
    ids = [
        event.provenance.native_event_id
        for event in result.events if event.kind is EventKind.USER_MESSAGE
    ]
    assert len(ids) == 2 and len(set(ids)) == 2, ids


def test_gemini_unknown_content_dict_gets_disposition_not_repr(tmp_path: Path) -> None:
    document = fixtures.messages_document(
        [{"id": "u1", "type": "user", "content": {"weird": {"nested": [1, 2]}}}],
        session_id="s-dict",
    )
    result = _adapt_gemini(tmp_path, document)
    user = artifacts.event_with(result, EventKind.USER_MESSAGE)
    # Python repr 不许落盘成正文。
    assert user.content is None
    assert "'weird'" not in (user.summary or "")
    assert "without a known text field" in artifacts.reasons(user)
    assert "weird" in artifacts.reasons(user)


def test_gemini_oversized_document_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超 max_bytes：adapt 报错、detect 判「不是我」，都不许整读进内存。"""
    monkeypatch.setattr(gemini_module, "MAX_JSON_BYTES", 64)
    document = fixtures.messages_document(
        [fixtures.user_message(USER_TEXT)], session_id="s-big",
    )
    artifact = fixtures.write_document(tmp_path, document)
    with pytest.raises(EventContractError):
        registry.adapt_for(
            "gemini", artifacts.single(artifact), artifact_root=tmp_path,
        )
    assert registry.detect_family("gemini", artifact, artifact_root=tmp_path) is False
