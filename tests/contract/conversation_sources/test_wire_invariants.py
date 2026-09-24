"""跨模块 wire 不变量：能读到的正文进 ``content``，读不出的点名原生字段。

公开 seam 只有 registry：``registry.adapt_for(family, artifact_set,
artifact_root=...)`` / ``registry.detect_family(family, artifact,
artifact_root=...)``。生产代码也只经 registry 消费适配器，所以**跨模块**的接线
不变量在这里断言，而不是在某个家族模块的函数上。

一条断言口径（本文件存在的理由）：

  * 原生 wire 里能读到的用户 / 助手正文必须进 ``content``；
  * 读不出正文的记录必须在 ``field_dispositions.reason`` 里点名原生字段或类型
    （例如 ``encrypted_content`` / ``session_fork`` / ``step_type=17``）;
  * 不许留下裸 ``unknown_native`` —— 无正文、无摘要、无 disposition 三者皆空
    的事件，用 :func:`support.artifacts.bare_unknowns` 兜这条。

本文件合并了旧的两份按格式文件（``test_parse_by_wire_format.py`` 与
``test_adapter_structure_comparison.py``）。旧文件各写各的夹具，其中一份还
``from tests.contract...test_antigravity_gemini_record_coverage import ...`` 摸了
另一份测试文件的私有 helper —— 这个病根在此消灭：夹具一律取
``support/artifacts.py``（公共底座）与 ``support/<module>.py``（按生产模块一一
对应），测试文件之间互不 import。

不变量：夹具只造 ``SourceArtifact`` 与合成字节，正文全是假句子，不读权威库。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifactSet
from personal_knowledge.core.conversation_events import EventKind
from tests.contract.conversation_sources.support import (
    antigravity as antigravity_fixtures,
)
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import chatgpt as chatgpt_fixtures
from tests.contract.conversation_sources.support import (
    claude_qoder as claude_qoder_fixtures,
)
from tests.contract.conversation_sources.support import codex as codex_fixtures
from tests.contract.conversation_sources.support import copilot as copilot_fixtures
from tests.contract.conversation_sources.support import cursor as cursor_fixtures
from tests.contract.conversation_sources.support import gemini as gemini_fixtures
from tests.contract.conversation_sources.support import grok as grok_fixtures
from tests.contract.conversation_sources.support import (
    mimo_opencode as mimo_opencode_fixtures,
)
from tests.contract.conversation_sources.support import pi as pi_fixtures
from tests.contract.conversation_sources.support import (
    workbuddy_kimi as workbuddy_kimi_fixtures,
)
from tests.contract.conversation_sources.support import zcode as zcode_fixtures

# 跨 wire 复用的同一句合成正文：同一条口径在每个 wire 上用同一句表达。
# 常量取自公共底座 ``support/artifacts.py``，不是本文件新造的魔法字符串。
USER = artifacts.USER_TEXT
ASSISTANT = artifacts.ASSISTANT_TEXT


# --------------------------------------------------------------------- harness


def _adapt(family: str, artifact, artifact_root: Path):
    """族级适配入口：artifact set + root，一律经 registry。"""
    return registry.adapt_for(
        family, artifacts.single(artifact), artifact_root=artifact_root,
    )


def _extracted(result, user: str, assistant: str) -> dict[EventKind, str]:
    """结果里这两句合成正文各落在哪种事件上（正文为空的一律不计）。"""
    found: dict[EventKind, str] = {}
    for event in result.events:
        if event.content == user:
            found[EventKind.USER_MESSAGE] = event.content
        elif event.content == assistant:
            found[EventKind.ASSISTANT_MESSAGE] = event.content
    return found


# ------------------------------------------------------------------ wire 记录


def _message_blocks_records(user: str, assistant: str) -> list[dict]:
    """``jsonl.message_blocks``：claude / qoder 共用的 ``message`` content-block。

    ``stop_reason`` 是 claude 的 detect 标记（qoder 认 ``message`` 信封），
    带上它这条 wire 才对两家都成立 —— 这正是「同一 wire 服务两家」的含义。
    """
    return [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s-wire",
            "message": {"role": "user", "content": [{"type": "text", "text": user}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "s-wire",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": assistant}],
            },
        },
    ]


def _workbuddy_records(user: str, assistant: str) -> list[dict]:
    """``jsonl.workbuddy_message``：``type=message`` + ``input_text``/``output_text``。"""
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": user}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": assistant}],
        },
    ]


def _kimi_wire_records(user: str, assistant: str) -> list[dict]:
    """``jsonl.kimi_wire``：``turn.prompt`` + ``context.append_loop_event``。"""
    return [
        {"type": "turn.prompt", "input": [{"type": "text", "text": user}]},
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": assistant}},
        },
    ]


def _kimi_envelope_records(user: str) -> list[dict]:
    """``jsonl.kimi_envelope``：``kind=event`` 信封，正文在 ``payload.prompt``。"""
    return [
        {
            "kind": "event",
            "envelope": {
                "type": "turn.started",
                "session_id": "s-wire",
                "payload": {"prompt": user, "turnId": 1},
            },
        },
        {
            "kind": "event",
            "envelope": {
                "type": "prompt.completed",
                "session_id": "s-wire",
                "payload": {"promptId": "prompt-only"},
            },
        },
    ]


def _cursor_rows(user: str, assistant: str) -> list[dict]:
    """``jsonl.cursor_transcript``：``role`` + ``message.content`` 块。"""
    return [
        {"role": "user", "message": {"content": [{"type": "text", "text": user}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": assistant}]}},
    ]


def _chatgpt_rows(db_root: Path, user: str, assistant: str) -> dict:
    """``sqlite.chatgpt_flat``：AgentsView 形态 ``sessions`` / ``messages`` 行。

    第三行 ``content`` 为 NULL：这条读不出正文的记录必须在 reason 里点名
    ``content missing``。夹具经真实 capture seam 抓取（allowlist 见生产模块）。
    """
    sessions = (
        (
            chatgpt_fixtures.FLAT_SESSION_ID, "chatgpt", "2026-09-01T00:00:00Z",
            None, None, None,
        ),
    )
    messages = (
        (
            "message-user", chatgpt_fixtures.FLAT_SESSION_ID, 1, "user",
            user, "2026-09-01T00:00:01Z", 0, 0,
        ),
        (
            "message-assistant", chatgpt_fixtures.FLAT_SESSION_ID, 2, "assistant",
            assistant, "2026-09-01T00:00:02Z", 0, 0,
        ),
        (
            "message-empty", chatgpt_fixtures.FLAT_SESSION_ID, 3, "assistant",
            None, "2026-09-01T00:00:03Z", 0, 0,
        ),
    )
    db = db_root / "sessions.db"
    db_root.mkdir(parents=True, exist_ok=True)
    chatgpt_fixtures.make_agentsview_db(db, sessions=sessions, messages=messages)
    artifact, root = chatgpt_fixtures.captured_agentsview(db, db_root)
    return {
        "artifact": artifact,
        "root": root,
        "encoding": json.dumps(
            {"sessions": sessions, "messages": messages}, ensure_ascii=False,
        ),
    }


# --------------------------------------------------- 14 个 wire 格式的适配入口


def _message_blocks(tmp_path: Path, family: str):
    root = tmp_path / family
    root.mkdir(parents=True, exist_ok=True)
    _artifact, _root, result = claude_qoder_fixtures.adapt_family(
        family, root, f"{family}.jsonl", _message_blocks_records(USER, ASSISTANT),
    )
    return result


def _codex_payload(tmp_path: Path):
    """codex rollout：模块夹具一条会话里放齐尚未还原的原生类型。"""
    _records, result = codex_fixtures.adapt_coverage(tmp_path)
    return result


def _workbuddy_message(tmp_path: Path):
    artifact, root = workbuddy_kimi_fixtures.wire(
        tmp_path, "wire.workbuddy", "session.jsonl",
        _workbuddy_records(USER, ASSISTANT), family="workbuddy",
    )
    return _adapt("workbuddy", artifact, root)


def _kimi_wire(tmp_path: Path):
    artifact, root = workbuddy_kimi_fixtures.wire(
        tmp_path, "wire.kimi", "wire.jsonl",
        _kimi_wire_records(USER, ASSISTANT), family="kimi",
    )
    return _adapt("kimi", artifact, root)


def _kimi_envelope(tmp_path: Path):
    artifact, root = workbuddy_kimi_fixtures.wire(
        tmp_path, "wire.kimi.envelope", "session.jsonl",
        _kimi_envelope_records(USER), family="kimi",
    )
    return _adapt("kimi", artifact, root)


def _copilot_events(tmp_path: Path):
    artifact, root = copilot_fixtures.captured_trace(tmp_path)
    return _adapt("copilot", artifact, root)


def _cursor_transcript(tmp_path: Path):
    src = cursor_fixtures.write_cursor_transcript(
        tmp_path / "source", _cursor_rows(USER, ASSISTANT),
    )
    artifact, root = artifacts.captured_file(
        src, tmp_path / "capture",
        relative_path=cursor_fixtures.JSONL_RELATIVE_PATH,
        byte_limit=1_000_000, count_limit=1,
    )
    return _adapt("cursor", artifact, root)


def _pi_message(tmp_path: Path):
    artifact, root = pi_fixtures.session_artifact(tmp_path)
    return _adapt("pi", artifact, root)


def _gemini_messages(tmp_path: Path):
    document = gemini_fixtures.messages_document(
        [
            gemini_fixtures.user_message(USER),
            gemini_fixtures.model_message(ASSISTANT),
        ],
        session_id="s-wire",
    )
    artifact = gemini_fixtures.write_document(tmp_path, document)
    return _adapt("gemini", artifact, tmp_path)


def _grok_session(tmp_path: Path):
    """``directory.grok_session``：整段会话目录经真实 capture seam 抓取。"""
    src = tmp_path / "session"
    grok_fixtures.make_session_directory(src)
    _manifest, captured = artifacts.captured_directory(
        src, tmp_path, include_relative=grok_fixtures.SESSION_INCLUDE,
        byte_limit=1_000_000, count_limit=8,
    )
    return registry.adapt_for(
        "grok", SourceArtifactSet(artifacts=captured),
        artifact_root=tmp_path / "artifacts",
    )


def _zcode_part(tmp_path: Path):
    """``sqlite.zcode_part``：在线 ``session`` / ``message`` / ``part`` 库。"""
    artifact, root = zcode_fixtures.live_artifact(tmp_path)
    return _adapt("zcode", artifact, root)


def _live_part(tmp_path: Path, family: str):
    """``sqlite.live_part``：mimo / opencode 共用的 live 形状库。"""
    artifact, root = mimo_opencode_fixtures.live_store(tmp_path, family)
    return _adapt(family, artifact, root)


def _chatgpt_flat(tmp_path: Path):
    rows = _chatgpt_rows(tmp_path, USER, ASSISTANT)
    return _adapt("chatgpt", rows["artifact"], rows["root"])


def _antigravity_steps(tmp_path: Path):
    """``sqlite.antigravity_steps``：``steps.step_payload`` 里的二进制 protobuf。"""
    db = antigravity_fixtures.live_db(
        tmp_path,
        [
            (14, antigravity_fixtures.user_step(USER)),
            (15, antigravity_fixtures.assistant_reply_step(ASSISTANT)),
            (17, antigravity_fixtures.error_step()),
        ],
    )
    return _adapt("antigravity", antigravity_fixtures.live_artifact(db), db.parent)


# wire 格式 -> (适配函数, 期望用户句, 期望助手句 | None, 读不出时必须点名的原生片段)
#
# 「期望助手句 = None」= 这条 wire 在夹具里没有可读的助手正文，只要求缺口被点名。
# 「原生片段 = None」= 该 wire 的夹具没有「读不出正文」的记录，gap 断言由
# ``bare_unknowns`` 兜底。
WIRES: dict[str, tuple[Callable[[Path], object], str, str | None, str | None]] = {
    "jsonl.message_blocks": (
        lambda path: _message_blocks(path, "claude"), USER, ASSISTANT, None,
    ),
    "jsonl.codex_payload": (
        _codex_payload,
        codex_fixtures.COMPACTED_USER_TEXT,
        codex_fixtures.COMPACTED_ASSISTANT_TEXT,
        "encrypted_content",
    ),
    "jsonl.workbuddy_message": (_workbuddy_message, USER, ASSISTANT, None),
    "jsonl.kimi_wire": (_kimi_wire, USER, ASSISTANT, None),
    "jsonl.kimi_envelope": (_kimi_envelope, USER, None, "prompt.completed"),
    "jsonl.copilot_events": (
        _copilot_events,
        copilot_fixtures.USER_TEXT,
        copilot_fixtures.ASSISTANT_TEXT,
        "session.truncation",
    ),
    "jsonl.cursor_transcript": (_cursor_transcript, USER, ASSISTANT, None),
    "jsonl.pi_message": (
        _pi_message, pi_fixtures.USER_TEXT, pi_fixtures.ASSISTANT_TEXT, "image",
    ),
    "json.gemini_messages": (_gemini_messages, USER, ASSISTANT, None),
    "directory.grok_session": (
        _grok_session,
        grok_fixtures.SESSION_USER_TEXT,
        grok_fixtures.SESSION_ASSISTANT_TEXT,
        "encrypted_content",
    ),
    "sqlite.zcode_part": (
        _zcode_part,
        zcode_fixtures.LIVE_USER_TEXT,
        zcode_fixtures.LIVE_ASSISTANT_TEXT,
        "session_fork",
    ),
    "sqlite.live_part": (
        lambda path: _live_part(path, "opencode"),
        mimo_opencode_fixtures.LIVE_USER_TEXT,
        mimo_opencode_fixtures.LIVE_ASSISTANT_TEXT,
        "reasoningEncryptedContent",
    ),
    "sqlite.chatgpt_flat": (
        _chatgpt_flat,
        USER,
        ASSISTANT,
        "content missing",
    ),
    "sqlite.antigravity_steps": (
        _antigravity_steps, USER, ASSISTANT, "step_type=17",
    ),
}


@pytest.mark.parametrize("fmt", list(WIRES))
def test_wire_extracts_readable_text_or_names_the_gap(tmp_path: Path, fmt: str) -> None:
    """14 个 wire 格式统一口径：正文进 content，缺口点名原生字段，不留裸 unknown。"""
    adapt, user, assistant, reason_needle = WIRES[fmt]
    result = adapt(tmp_path)

    assert artifacts.has_event(result, EventKind.USER_MESSAGE, user), fmt
    if assistant is not None:
        assert artifacts.has_event(result, EventKind.ASSISTANT_MESSAGE, assistant), fmt
    if reason_needle is not None:
        assert artifacts.any_reason(result, reason_needle), fmt
    assert not artifacts.bare_unknowns(result), fmt


def test_same_wire_is_shared_by_both_products(tmp_path: Path) -> None:
    """``message_blocks`` 与 ``live_part`` 各服务两家客户端：一 wire 一句正文。

    吸收旧 ``test_adapter_structure_comparison.py`` 的
    ``test_claude_and_qoder_share_one_wire_and_one_parse``：这里不仅要求两家抽出
    的正文相同，还要求落在同一种事件上，并把 mimo / opencode 这一组也纳入。
    """
    expected = {EventKind.USER_MESSAGE: USER, EventKind.ASSISTANT_MESSAGE: ASSISTANT}

    records = _message_blocks_records(USER, ASSISTANT)
    for family in ("claude", "qoder"):
        root = tmp_path / family
        root.mkdir(parents=True, exist_ok=True)
        artifact, artifact_root, result = claude_qoder_fixtures.adapt_family(
            family, root, f"{family}.jsonl", records,
        )
        assert registry.detect_family(
            family, artifact, artifact_root=artifact_root,
        ) is True, family
        assert _extracted(result, USER, ASSISTANT) == expected, family

    live_user = mimo_opencode_fixtures.LIVE_USER_TEXT
    live_assistant = mimo_opencode_fixtures.LIVE_ASSISTANT_TEXT
    live_expected = {
        EventKind.USER_MESSAGE: live_user,
        EventKind.ASSISTANT_MESSAGE: live_assistant,
    }
    for family in ("mimo", "opencode"):
        artifact, root = mimo_opencode_fixtures.live_store(tmp_path / family, family)
        result = _adapt(family, artifact, root)
        assert _extracted(result, live_user, live_assistant) == live_expected, family


def test_workbuddy_and_kimi_are_different_wires(tmp_path: Path) -> None:
    """同一模块服务两家客户端，但原生 wire 不同：正文相同、原生标记不串门。"""
    workbuddy_records = _workbuddy_records(USER, ASSISTANT)
    kimi_records = _kimi_wire_records(USER, ASSISTANT)
    workbuddy_encoding = artifacts.jsonl(workbuddy_records)
    kimi_encoding = artifacts.jsonl(kimi_records)

    assert workbuddy_encoding != kimi_encoding
    assert "turn.prompt" not in workbuddy_encoding
    assert "input_text" not in kimi_encoding

    workbuddy = _adapt(
        "workbuddy",
        *workbuddy_kimi_fixtures.wire(
            tmp_path / "workbuddy", "wire.workbuddy", "session.jsonl",
            workbuddy_records, family="workbuddy",
        ),
    )
    kimi = _adapt(
        "kimi",
        *workbuddy_kimi_fixtures.wire(
            tmp_path / "kimi", "wire.kimi", "wire.jsonl",
            kimi_records, family="kimi",
        ),
    )
    for family, result in (("workbuddy", workbuddy), ("kimi", kimi)):
        assert artifacts.has_event(result, EventKind.USER_MESSAGE, USER), family
        assert artifacts.has_event(
            result, EventKind.ASSISTANT_MESSAGE, ASSISTANT,
        ), family


def test_same_sentence_survives_distinct_native_structures(tmp_path: Path) -> None:
    """同一句正文落在互不相同的原生结构里，抽出的正文与事件种类相同。

    吸收旧 ``test_adapter_structure_comparison.py`` 的
    ``test_different_wires_still_extract_the_same_sentence``：旧文件在 7 个 wire
    （codex / workbuddy / kimi / copilot / cursor / gemini / grok）上手写记录，
    这里换成「夹具能接受调用方句子」的 7 个结构（claude 代表 message_blocks，
    另加 chatgpt 的 sqlite 行与 antigravity 的二进制 protobuf），codex / copilot
    / grok 的固定夹具句子改由上面的参数化用例逐 wire 断言。
    """
    structures: dict[str, tuple[str, object]] = {}

    records = _message_blocks_records(USER, ASSISTANT)
    root = tmp_path / "shape-claude"
    root.mkdir(parents=True, exist_ok=True)
    _artifact, _root, result = claude_qoder_fixtures.adapt_family(
        "claude", root, "claude.jsonl", records,
    )
    structures["jsonl.message_blocks"] = (artifacts.jsonl(records), result)

    workbuddy_records = _workbuddy_records(USER, ASSISTANT)
    artifact, artifact_root = workbuddy_kimi_fixtures.wire(
        tmp_path / "shape-workbuddy", "wire.workbuddy", "session.jsonl",
        workbuddy_records, family="workbuddy",
    )
    structures["jsonl.workbuddy_message"] = (
        artifacts.jsonl(workbuddy_records),
        _adapt("workbuddy", artifact, artifact_root),
    )

    kimi_records = _kimi_wire_records(USER, ASSISTANT)
    artifact, artifact_root = workbuddy_kimi_fixtures.wire(
        tmp_path / "shape-kimi", "wire.kimi", "wire.jsonl",
        kimi_records, family="kimi",
    )
    structures["jsonl.kimi_wire"] = (
        artifacts.jsonl(kimi_records),
        _adapt("kimi", artifact, artifact_root),
    )

    rows = _cursor_rows(USER, ASSISTANT)
    src = cursor_fixtures.write_cursor_transcript(tmp_path / "shape-cursor", rows)
    artifact, artifact_root = artifacts.captured_file(
        src, tmp_path / "shape-cursor" / "capture",
        relative_path=cursor_fixtures.JSONL_RELATIVE_PATH,
        byte_limit=1_000_000, count_limit=1,
    )
    structures["jsonl.cursor_transcript"] = (
        artifacts.jsonl(rows), _adapt("cursor", artifact, artifact_root),
    )

    document = gemini_fixtures.messages_document(
        [
            gemini_fixtures.user_message(USER),
            gemini_fixtures.model_message(ASSISTANT),
        ],
        session_id="s-wire",
    )
    artifact = gemini_fixtures.write_document(tmp_path / "shape-gemini", document)
    structures["json.gemini_messages"] = (
        json.dumps(document, ensure_ascii=False),
        _adapt("gemini", artifact, tmp_path / "shape-gemini"),
    )

    chatgpt_rows = _chatgpt_rows(tmp_path / "shape-chatgpt", USER, ASSISTANT)
    structures["sqlite.chatgpt_flat"] = (
        chatgpt_rows["encoding"],
        _adapt("chatgpt", chatgpt_rows["artifact"], chatgpt_rows["root"]),
    )

    steps = [
        (14, antigravity_fixtures.user_step(USER)),
        (15, antigravity_fixtures.assistant_reply_step(ASSISTANT)),
    ]
    db = antigravity_fixtures.live_db(tmp_path / "shape-antigravity", steps)
    structures["sqlite.antigravity_steps"] = (
        b"".join(payload for _step_type, payload in steps).hex(),
        _adapt("antigravity", antigravity_fixtures.live_artifact(db), db.parent),
    )

    # 原生编码两两不同：同一个结构不许出现在两个 wire 名下。
    encodings = {name: encoding for name, (encoding, _result) in structures.items()}
    assert len(set(encodings.values())) == len(encodings), encodings
    # 原生标记互不串门（旧文件的原样断言）。
    assert "turn.prompt" not in encodings["jsonl.workbuddy_message"]
    assert "input_text" not in encodings["jsonl.kimi_wire"]
    assert '"type": "gemini"' in encodings["json.gemini_messages"]
    assert "response_item" not in encodings["jsonl.cursor_transcript"]

    # 抽出同一句正文，落在同一对事件种类上。
    expected = {EventKind.USER_MESSAGE: USER, EventKind.ASSISTANT_MESSAGE: ASSISTANT}
    extracted = {
        name: _extracted(result, USER, ASSISTANT)
        for name, (_encoding, result) in structures.items()
    }
    assert extracted == {name: expected for name in structures}
