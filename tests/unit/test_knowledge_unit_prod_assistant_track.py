"""Phase 41 Plan 02：assistant 轨抽取器测试（task 1/2/4/6）。

覆盖：
- task 1：AssistantExtractionResult / ExtractionResult 类型集合互斥；
  TrackConfig 前缀构造期断言；ASSISTANT_TRACK 前缀恰 3 字符；prompt 文件存在。
- task 2：assistant run 产出 as| 前缀 + evidence_scope='assistant' + 3 新类型；
  quote 回查锚截断后 assistant 原文；role_mismatch → terminal_failed；
  user 轨零回归（默认 track 行为不变）。
- task 4：prepare_production_delta(track="assistant") artifact/roles/watermark
  key/prompt_version；track 与显式 roles 冲突 fail closed。
- task 6：detect_confirmation_signal 三态 + 双命中纠正优先；confidence 为证据
  派生（PDA-41 起弃用 LLM 自报），D-03 修饰并入派生（adopted +0.05 /
  corrected -0.2），非硬 gate；user 轨 stats 无 confirmation_* 键。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_knowledge.application.knowledge.build_knowledge_units import (
    ASSISTANT_PROMPT_PATH,
    AssistantExtractionResult,
    ExtractionResult,
)
from personal_knowledge.application.knowledge.build_knowledge_units_prod import (
    ASSISTANT_TRACK,
    USER_TRACK,
    TrackConfig,
    process_run,
)
from personal_knowledge.application.knowledge import build_knowledge_units_prod as prod
from personal_knowledge.application.knowledge.confirmation_signals import (
    detect_confirmation_signal,
)
from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import (
    SCHEMA_SQL,
)
from personal_knowledge.application.knowledge.refresh_knowledge_units import (
    prepare_production_delta,
)

_PROVIDER = dict(
    provider="openai",
    endpoint="https://api.openai.com/v1",
    auth_mode="api_key",
    model="gpt-test",
)

SOLUTION_TEXT = (
    "关键解决方案内容：先备份数据库，再建新表搬数据，最后校验行数守恒。"
    "这套表重建流程适用于所有 SQLite CHECK 约束变更场景。"
)
USER_QUESTION = "如何安全地修改 SQLite 表的 CHECK 约束而不丢数据？"


def _unit(unit_type: str, quote: str, confidence: float = 0.9) -> dict:
    return {
        "unit_type": unit_type,
        "subject": "SQLite 迁移",
        "question": "如何修改 CHECK 约束？",
        "answer": "表重建流程：备份→建新表→搬数据→校验→重命名",
        "confidence": confidence,
        "evidence_quote": quote,
        "lifecycle": "current",
    }


def _llm_payload(units: list[dict]) -> str:
    return json.dumps({"units": units, "abstain": False, "abstain_reason": ""})


# === fixtures ===

def _make_unified_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()


def _seed_run(
    db: Path,
    run_id: str,
    refs: list[str],
    *,
    prompt_version: str = "v1_assistant",
) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO knowledge_build_runs "
        "(run_id, run_type, generated_at, input_hash, prompt_version, status) "
        "VALUES (?, 'incremental', '2026-07-26T00:00:00Z', 'ih', ?, 'pending')",
        (run_id, prompt_version),
    )
    for pos, ref in enumerate(refs):
        con.execute(
            "INSERT INTO knowledge_run_items "
            "(run_id, inventory_id, position, evidence_ref, status, attempt_count, "
            "unit_count, updated_at) "
            "VALUES (?, 'di_test', ?, ?, 'pending', 0, 0, '2026-07-26T00:00:00Z')",
            (run_id, pos, ref),
        )
    con.commit()
    con.close()


def _make_canonical_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE canonical_sessions ("
        "canonical_session_id TEXT PRIMARY KEY, agent TEXT, started_at TEXT, "
        "evidence_eligible INTEGER NOT NULL DEFAULT 1)"
    )
    con.execute(
        "CREATE TABLE canonical_messages ("
        "canonical_message_id TEXT PRIMARY KEY, canonical_session_id TEXT, "
        "source TEXT, ordinal INTEGER, role TEXT, content TEXT, timestamp TEXT)"
    )
    con.execute(
        "INSERT INTO canonical_sessions VALUES ('cs1', 'gemini', '2026-07-20T10:00:00', 1)"
    )
    con.execute(
        "INSERT INTO canonical_sessions VALUES ('cs2', 'gemini', '2026-07-21T10:00:00', 1)"
    )
    con.execute(
        "INSERT INTO canonical_sessions VALUES ('cs3', 'gemini', '2026-07-22T10:00:00', 1)"
    )
    rows = [
        # cs1：user 提问 → assistant 方案 → user 采纳
        ("cm_u1", "cs1", "test", 1, "user", USER_QUESTION, "2026-07-20T10:00:01"),
        ("cm_a1", "cs1", "test", 2, "assistant", SOLUTION_TEXT, "2026-07-20T10:00:02"),
        ("cm_u2", "cs1", "test", 3, "user", "谢谢，解决了！", "2026-07-20T10:00:03"),
        # cs2：assistant → user 纠正
        ("cm_a2", "cs2", "test", 1, "assistant", SOLUTION_TEXT, "2026-07-21T10:00:01"),
        ("cm_u3", "cs2", "test", 2, "user", "不对，应该是 B 方案", "2026-07-21T10:00:02"),
        # cs3：assistant 无后续 user
        ("cm_a3", "cs3", "test", 1, "assistant", SOLUTION_TEXT, "2026-07-22T10:00:01"),
    ]
    con.executemany("INSERT INTO canonical_messages VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _fake_llm(text: str):
    calls: list[dict] = []

    def fake(system_prompt, user_content, model, token_provider, **kwargs):
        calls.append({"user_content": user_content, "kwargs": kwargs})
        return {"text": text, "usage": {}}

    return fake, calls


def _run_assistant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refs: list[str],
    llm_text: str,
    *,
    canonical_rows: list[tuple] | None = None,
) -> tuple[dict, sqlite3.Connection, list[dict]]:
    db = tmp_path / "unified.db"
    canon = tmp_path / "canonical.db"
    _make_unified_db(db)
    _make_canonical_db(canon)
    if canonical_rows:
        con = sqlite3.connect(canon)
        con.executemany(
            "INSERT OR REPLACE INTO canonical_messages VALUES (?,?,?,?,?,?,?)",
            canonical_rows,
        )
        con.commit()
        con.close()
    _seed_run(db, "ir_asstest", refs)
    fake, calls = _fake_llm(llm_text)
    monkeypatch.setattr(prod, "call_llm_with_retry", fake)
    stats = process_run(
        "ir_asstest",
        "gpt-test",
        db_path=db,
        canonical_db=canon,
        workers=1,
        min_request_interval=0.0,
        track=ASSISTANT_TRACK,
    )
    con = sqlite3.connect(db)
    return stats, con, calls


# === task 1：模型与 TrackConfig ===

def test_assistant_result_accepts_new_types() -> None:
    for t in ("solution", "decision_rationale", "technical_conclusion"):
        r = AssistantExtractionResult.model_validate(
            {"units": [_unit(t, "关键解决方案内容")], "abstain": False, "abstain_reason": ""}
        )
        assert r.units[0].unit_type == t


def test_assistant_result_rejects_user_types() -> None:
    for t in ("preference", "habit", "personal_fact", "project_decision",
              "capability", "tool_usage"):
        with pytest.raises(ValidationError):
            AssistantExtractionResult.model_validate(
                {"units": [_unit(t, "关键解决方案内容")], "abstain": False,
                 "abstain_reason": ""}
            )


def test_user_result_rejects_assistant_types() -> None:
    for t in ("solution", "decision_rationale", "technical_conclusion"):
        with pytest.raises(ValidationError):
            ExtractionResult.model_validate(
                {"units": [_unit(t, "关键解决方案内容")], "abstain": False,
                 "abstain_reason": ""}
            )
    ok = ExtractionResult.model_validate(
        {"units": [_unit("preference", "关键解决方案内容")], "abstain": False,
         "abstain_reason": ""}
    )
    assert ok.units[0].unit_type == "preference"


def test_track_config_prefix_assertion() -> None:
    with pytest.raises(AssertionError):
        TrackConfig(
            name="x",
            prompt_path=ASSISTANT_PROMPT_PATH,
            prompt_version="x",
            role_label="x",
            evidence_scope="assistant",
            unit_id_prefix="asst|",  # 4 字符，违反 R2（必须恰 3 字符）
            result_model=AssistantExtractionResult,
            roles=("assistant",),
        )


def test_assistant_track_prefix_contract() -> None:
    assert ASSISTANT_TRACK.unit_id_prefix == "as|"
    assert len(ASSISTANT_TRACK.unit_id_prefix) == 3
    assert ASSISTANT_TRACK.unit_id_prefix.endswith("|")
    assert USER_TRACK.unit_id_prefix == "v1|"


def test_assistant_prompt_path_exists() -> None:
    assert ASSISTANT_PROMPT_PATH.exists()


# === task 2：assistant run 抽取 ===

def test_assistant_run_commits_as_prefixed_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adopted 信号：证据派生 confidence（0.4+0.2+0.15 QA 双证据+0.05 adopted=0.8）；无证据 unit 丢弃并计数。"""
    llm_text = _llm_payload([
        _unit("solution", "关键解决方案内容", confidence=0.98),
        _unit("solution", "完全编造的引文根本不在原文里", confidence=0.5),
    ])
    stats, con, calls = _run_assistant(
        tmp_path, monkeypatch, ["cm_a1"], llm_text
    )

    rows = con.execute(
        "SELECT unit_id, unit_type, evidence_scope, confidence FROM knowledge_units"
    ).fetchall()
    assert len(rows) == 1
    unit_id, unit_type, scope, confidence = rows[0]
    assert unit_id.startswith("as|")
    assert unit_type == "solution"
    assert scope == "assistant"
    # PDA-41：弃用 LLM 自报（0.98），证据派生 0.4+0.2+0.15(QA 双证据)+0.05(adopted)=0.8
    assert confidence == 0.8
    assert stats["units_dropped_no_evidence"] == 1
    assert stats["confirmation_adopted"] == 1
    # QA 对：LLM 输入含前置 user 上下文段，role_label 为 assistant 包装
    assert "用户问题上下文（仅供理解，不作证据）：" in calls[0]["user_content"]
    assert USER_QUESTION in calls[0]["user_content"]
    assert calls[0]["kwargs"]["role_label"] == "助手回答证据（role=assistant）："
    con.close()


def test_assistant_run_corrected_confidence_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """corrected 信号：证据派生 confidence（0.4+0.2 单证据-0.2 corrected=0.4），unit 仍正常 INSERT（非硬 gate）。"""
    llm_text = _llm_payload([_unit("solution", "关键解决方案内容", confidence=0.1)])
    stats, con, _calls = _run_assistant(tmp_path, monkeypatch, ["cm_a2"], llm_text)

    confidence = con.execute("SELECT confidence FROM knowledge_units").fetchone()[0]
    # PDA-41：弃用 LLM 自报（0.1），证据派生 0.4+0.2-0.2(corrected)=0.4
    assert confidence == 0.4
    assert stats["succeeded"] == 1
    assert stats["confirmation_corrected"] == 1
    con.close()


def test_assistant_run_no_followup_none_and_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无后续 user → confirmation_none；>48000 尾部硬截，截断外 quote 丢弃。"""
    head = "解决方案正文段落。" * 5000  # 40000 字
    mid = "关键证据片段abcdefg"
    tail = "尾" * 9000 + "超出截断的独特内容" + "尾" * 200
    long_content = head + mid + tail  # >48000，独特内容落在 49017 处（截断外）
    rows = [("cm_long", "cs3", "test", 9, "assistant", long_content,
             "2026-07-22T10:00:09")]
    llm_text = _llm_payload([
        _unit("solution", "关键证据片段abcdefg", confidence=0.9),
        _unit("solution", "超出截断的独特内容", confidence=0.9),
    ])
    stats, con, _calls = _run_assistant(
        tmp_path, monkeypatch, ["cm_long"], llm_text, canonical_rows=rows
    )

    assert stats["truncated"] == 1
    assert stats["confirmation_none"] == 1
    quotes = [r[0] for r in con.execute("SELECT evidence_quote FROM knowledge_units")]
    assert quotes == ["关键证据片段abcdefg"]
    assert stats["units_dropped_no_evidence"] == 1
    con.close()


def test_assistant_run_role_mismatch_terminal_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run 级单轨：assistant run 混入 user ref → terminal_failed/role_mismatch。"""
    llm_text = _llm_payload([_unit("solution", "关键解决方案内容")])
    stats, con, _calls = _run_assistant(
        tmp_path, monkeypatch, ["cm_u1", "cm_a1"], llm_text
    )

    mismatch = con.execute(
        "SELECT status, last_error_class FROM knowledge_run_items "
        "WHERE evidence_ref='cm_u1'"
    ).fetchone()
    assert mismatch == ("terminal_failed", "role_mismatch")
    assert stats["role_mismatch"] == 1
    ok = con.execute(
        "SELECT status FROM knowledge_run_items WHERE evidence_ref='cm_a1'"
    ).fetchone()
    assert ok == ("succeeded",)
    con.close()


def test_user_track_zero_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 USER_TRACK：v1| 前缀 / scope=user / stats 无 confirmation_*、role_mismatch 键。

    truncated 自双轨对称截断（MESSAGE_MAX_CHARS=48000）起两轨都有，短消息为 0。
    """
    db = tmp_path / "unified.db"
    canon = tmp_path / "canonical.db"
    _make_unified_db(db)
    _make_canonical_db(canon)
    _seed_run(db, "ir_usertest", ["cm_u1"], prompt_version="v1")
    llm_text = _llm_payload([_unit("preference", "如何安全地修改")])
    fake, _calls = _fake_llm(llm_text)
    monkeypatch.setattr(prod, "call_llm_with_retry", fake)

    stats = process_run(
        "ir_usertest", "gpt-test", db_path=db, canonical_db=canon,
        workers=1, min_request_interval=0.0,
    )
    con = sqlite3.connect(db)
    unit_id, scope = con.execute(
        "SELECT unit_id, evidence_scope FROM knowledge_units"
    ).fetchone()
    assert unit_id.startswith("v1|")
    assert scope == "user"
    assert not any(k.startswith("confirmation_") for k in stats)
    assert stats["truncated"] == 0
    assert "role_mismatch" not in stats
    con.close()


# === task 6：detect_confirmation_signal ===

def _detect(canon: Path, session: str, ref: str) -> str:
    return detect_confirmation_signal(
        canon, session_id=session, anchor_message_ref=ref
    )


def test_detect_signal_three_states(tmp_path: Path) -> None:
    canon = tmp_path / "canonical.db"
    _make_canonical_db(canon)
    assert _detect(canon, "cs1", "cm_a1") == "adopted"
    assert _detect(canon, "cs2", "cm_a2") == "corrected"
    assert _detect(canon, "cs3", "cm_a3") == "none"
    # 锚不存在 / 空参数 → none（不炸）
    assert _detect(canon, "cs1", "cm_missing") == "none"
    assert _detect(canon, "", "cm_a1") == "none"


def test_detect_signal_double_hit_corrected_wins(tmp_path: Path) -> None:
    """同条消息双命中 → 纠正优先（保守）。"""
    canon = tmp_path / "canonical.db"
    _make_canonical_db(canon)
    con = sqlite3.connect(canon)
    con.execute(
        "INSERT INTO canonical_messages VALUES "
        "('cm_u4', 'cs3', 'test', 2, 'user', '谢谢，但其实不是这样', '2026-07-22T10:00:02')"
    )
    con.commit()
    con.close()
    assert _detect(canon, "cs3", "cm_a3") == "corrected"


# === task 4：prepare track 接线 ===

def _prepare_canonical(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE canonical_sessions ("
        "canonical_session_id TEXT PRIMARY KEY, agent TEXT, started_at TEXT, "
        "evidence_eligible INTEGER NOT NULL DEFAULT 1)"
    )
    con.execute(
        "CREATE TABLE canonical_messages ("
        "canonical_message_id TEXT PRIMARY KEY, canonical_session_id TEXT, "
        "source TEXT, ordinal INTEGER, role TEXT, content TEXT, timestamp TEXT)"
    )
    con.execute(
        "INSERT INTO canonical_sessions VALUES ('s1', 'gemini', '2026-07-20T10:00:00', 1)"
    )
    con.execute(
        "INSERT INTO canonical_messages VALUES "
        "('m_user', 's1', 'test', 1, 'user', ?, '2026-07-20T10:00:01')",
        (USER_QUESTION + "（补充说明让长度足够通过阈值）",),
    )
    con.execute(
        "INSERT INTO canonical_messages VALUES "
        "('m_asst', 's1', 'test', 2, 'assistant', ?, '2026-07-20T10:00:02')",
        (SOLUTION_TEXT,),
    )
    con.commit()
    con.close()


def _prepare_unified(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_source_watermark "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
    )
    con.execute(
        "INSERT INTO knowledge_source_watermark VALUES "
        "('committed', 'WM_USER_CHECKSUM', '2026-07-15T00:00:00')"
    )
    con.execute(
        "INSERT INTO knowledge_source_watermark VALUES "
        "('committed_assistant', 'WM_ASSISTANT_CHECKSUM', '2026-07-16T00:00:00')"
    )
    con.commit()
    con.close()


def test_prepare_assistant_track_artifact_and_run(tmp_path: Path) -> None:
    """track='assistant'：artifact 带 track；watermark 取 committed_assistant；
    run_items 全部 assistant；manifest prompt_version='v1_assistant'。"""
    db = tmp_path / "unified.db"
    canon = tmp_path / "canonical.db"
    _prepare_unified(db)
    _prepare_canonical(canon)

    artifact = prepare_production_delta(
        db_path=db, canonical_db=canon, track="assistant", **_PROVIDER
    )

    assert artifact["track"] == "assistant"
    assert artifact["no_op"] is False
    # diff 基线取自 assistant watermark key（两 key 值不同，可区分）
    assert artifact["source_before_checksum"] == "WM_ASSISTANT_CHECKSUM"
    assert artifact["roles"] == ["assistant"]

    con = sqlite3.connect(db)
    refs = [
        r[0]
        for r in con.execute(
            "SELECT evidence_ref FROM knowledge_run_items WHERE run_id=?",
            (artifact["fresh_run_id"],),
        )
    ]
    assert refs == ["m_asst"]
    pv = con.execute(
        "SELECT prompt_version FROM knowledge_build_runs WHERE run_id=?",
        (artifact["fresh_run_id"],),
    ).fetchone()[0]
    assert pv == "v1_assistant"
    con.close()
    # 安全字段全 false
    for key in ("active_changed", "watermark_changed", "production_llm_calls",
                "chroma_writes", "canonical_current_writes"):
        assert not artifact[key]


def test_prepare_assistant_track_roles_conflict_fail_closed(tmp_path: Path) -> None:
    """track='assistant' 与显式 roles=['user'] 冲突 → ValueError。"""
    db = tmp_path / "unified.db"
    canon = tmp_path / "canonical.db"
    _prepare_unified(db)
    _prepare_canonical(canon)
    with pytest.raises(ValueError):
        prepare_production_delta(
            db_path=db, canonical_db=canon, track="assistant",
            roles=["user"], **_PROVIDER,
        )


def test_prepare_invalid_track_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "unified.db"
    canon = tmp_path / "canonical.db"
    _prepare_unified(db)
    _prepare_canonical(canon)
    with pytest.raises(ValueError):
        prepare_production_delta(
            db_path=db, canonical_db=canon, track="system", **_PROVIDER
        )


# === QA 联立 v2：穿透短确认 + question-side evidence（PDA-41 deferred）===

def test_qa_context_penetrates_short_confirmations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锚点前的"继续"被穿透，QA 上下文挂到最近实质提问；question-side ref 以
    evidence_type='context' 落盘；confidence 按双证据派生（0.4+0.2+0.15=0.75）。"""
    rows = [
        ("cm_q1", "cs4", "test", 1, "user", "如何配置 Claude Desktop 的 MCP 代理？", "2026-07-23T10:00:01"),
        ("cm_x1", "cs4", "test", 2, "assistant", "先打开设置页看一下。", "2026-07-23T10:00:02"),
        ("cm_c1", "cs4", "test", 3, "user", "继续", "2026-07-23T10:00:03"),
        ("cm_a9", "cs4", "test", 4, "assistant", SOLUTION_TEXT, "2026-07-23T10:00:04"),
    ]
    llm_text = _llm_payload([_unit("solution", "关键解决方案内容")])
    stats, con, calls = _run_assistant(
        tmp_path, monkeypatch, ["cm_a9"], llm_text, canonical_rows=rows
    )
    ctx = calls[0]["user_content"].split("用户问题上下文（仅供理解，不作证据）：\n")[1].split("\n\n---")[0]
    assert "如何配置 Claude Desktop 的 MCP 代理？" in ctx
    assert "继续" not in ctx
    uid, conf = con.execute("SELECT unit_id, confidence FROM knowledge_units").fetchone()
    evs = set(con.execute(
        "SELECT evidence_ref, evidence_type FROM knowledge_unit_evidence WHERE unit_id=?",
        (uid,),
    ).fetchall())
    assert evs == {("cm_a9", "message"), ("cm_q1", "context")}
    assert conf == 0.75
    con.close()


def test_qa_context_keeps_short_substantive_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """短但实质的提问（"怎么配代理？"）不匹配确认套话，不得被穿透跳过。"""
    rows = [
        ("cm_q2", "cs4", "test", 1, "user", "怎么配代理？", "2026-07-23T10:00:01"),
        ("cm_a8", "cs4", "test", 2, "assistant", SOLUTION_TEXT, "2026-07-23T10:00:02"),
    ]
    llm_text = _llm_payload([_unit("solution", "关键解决方案内容")])
    stats, con, calls = _run_assistant(
        tmp_path, monkeypatch, ["cm_a8"], llm_text, canonical_rows=rows
    )
    assert "怎么配代理？" in calls[0]["user_content"]
    con.close()


def test_qa_context_empty_when_only_confirmations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前置 user 全是短确认 → 无 QA 上下文段、无 context 证据行、单证据派生 0.6。"""
    rows = [
        ("cm_c2", "cs4", "test", 1, "user", "好的", "2026-07-23T10:00:01"),
        ("cm_a7", "cs4", "test", 2, "assistant", SOLUTION_TEXT, "2026-07-23T10:00:02"),
    ]
    llm_text = _llm_payload([_unit("solution", "关键解决方案内容")])
    stats, con, calls = _run_assistant(
        tmp_path, monkeypatch, ["cm_a7"], llm_text, canonical_rows=rows
    )
    assert "用户问题上下文" not in calls[0]["user_content"]
    uid, conf = con.execute("SELECT unit_id, confidence FROM knowledge_units").fetchone()
    evs = con.execute(
        "SELECT evidence_ref, evidence_type FROM knowledge_unit_evidence WHERE unit_id=?",
        (uid,),
    ).fetchall()
    assert evs == [("cm_a7", "message")]
    assert conf == 0.6
    con.close()
