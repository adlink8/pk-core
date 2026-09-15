"""Phase 13.5 Wave 4.1 测试：conversation repository。

覆盖 PLAN Task 4.1：
  - legacy|canonical 显式模式，不静默双计数
  - canonical turn 携带 source_ref + source_session_ref
  - tool output 默认 [tool output omitted]
  - 不读另一个 source 的数据
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.conversation_repository import (  # noqa: E402
    ConversationRepository,
    ConversationTurn,
    ToolEvent,
    TOOL_OUTPUT_OMITTED,
    SOURCE_LEGACY,
    SOURCE_CANONICAL,
)


def _make_legacy_db(dest: Path) -> Path:
    con = sqlite3.connect(str(dest))
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE agent_sessions_meta (
            session_id TEXT, source TEXT, family TEXT, raw_file TEXT,
            line_no INTEGER, timestamp TEXT, cwd TEXT, model TEXT, originator TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE agent_messages (
            session_id TEXT, source TEXT, family TEXT, turn_id TEXT,
            event_index INTEGER, timestamp TEXT, raw_type TEXT, payload_type TEXT,
            role TEXT, text TEXT, raw_file TEXT, line_no INTEGER
        )"""
    )
    cur.execute(
        """CREATE TABLE agent_tool_calls (
            session_id TEXT, call_id TEXT, tool_name TEXT, arguments TEXT, status TEXT
        )"""
    )
    cur.execute(
        "INSERT INTO agent_sessions_meta (session_id, source, timestamp) VALUES "
        "('s1','Codex','2026-01-01'),('s2','Claude','2026-01-02')"
    )
    cur.execute(
        "INSERT INTO agent_messages (session_id, event_index, timestamp, role, text) VALUES "
        "('s1',1,'2026-01-01','user','hello'),"
        "('s1',2,'2026-01-01','assistant','hi there'),"
        "('s2',1,'2026-01-02','user','question')"
    )
    cur.execute(
        "INSERT INTO agent_tool_calls (session_id, call_id, tool_name, status) VALUES "
        "('s1','c1','Read','complete')"
    )
    con.commit()
    con.close()
    return dest


def _make_canonical_db(dest: Path) -> Path:
    con = sqlite3.connect(str(dest))
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE canonical_sessions (
            canonical_session_id TEXT PRIMARY KEY, primary_source TEXT, agent TEXT,
            started_at TEXT, ended_at TEXT, message_count INTEGER,
            user_message_count INTEGER, file_hash TEXT, parent_canonical_id TEXT,
            relationship_type TEXT, cwd TEXT, git_branch TEXT, model TEXT,
            evidence_eligible INTEGER DEFAULT 1, evidence_scope TEXT DEFAULT 'user',
            merged INTEGER DEFAULT 0
        )"""
    )
    cur.execute(
        """CREATE TABLE canonical_messages (
            canonical_message_id TEXT PRIMARY KEY,
            canonical_session_id TEXT, source TEXT, source_message_ref TEXT,
            ordinal INTEGER, role TEXT, content TEXT, content_length INTEGER,
            timestamp TEXT, model TEXT, is_system INTEGER DEFAULT 0,
            is_sidechain INTEGER DEFAULT 0, content_hash TEXT,
            evidence_scope TEXT DEFAULT 'user'
        )"""
    )
    cur.execute(
        """CREATE TABLE canonical_tool_events (
            canonical_tool_id TEXT PRIMARY KEY, canonical_session_id TEXT,
            source TEXT, source_kind TEXT, tool_name TEXT, category TEXT,
            status TEXT, call_index INTEGER, subagent_session_id TEXT,
            content_length INTEGER, timestamp TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE session_source_links (
            link_id TEXT PRIMARY KEY, canonical_session_id TEXT, source TEXT,
            source_session_id TEXT, source_raw_file TEXT, match_method TEXT,
            match_confidence TEXT
        )"""
    )
    cur.execute(
        "INSERT INTO canonical_sessions (canonical_session_id, primary_source, agent, started_at) VALUES "
        "('cs1','agentsview','codex','2026-01-01')"
    )
    cur.execute(
        "INSERT INTO canonical_messages (canonical_message_id, canonical_session_id, source, source_message_ref, ordinal, role, content, timestamp) VALUES "
        "('cm1','cs1','agentsview','av:100',1,'user','canon hello','2026-01-01'),"
        "('cm2','cs1','agentsview','av:101',2,'assistant','canon reply','2026-01-01')"
    )
    cur.execute(
        "INSERT INTO canonical_tool_events (canonical_tool_id, canonical_session_id, source, source_kind, tool_name, status, call_index) VALUES "
        "('cte1','cs1','agentsview','call','Bash','success',0)"
    )
    cur.execute(
        "INSERT INTO session_source_links (link_id, canonical_session_id, source, source_session_id, match_method) VALUES "
        "('l1','cs1','agentsview','agent-x','file_hash'),"
        "('l2','cs1','legacy','rollout-x','file_hash')"
    )
    con.commit()
    con.close()
    return dest


def test_legacy_mode_reads_legacy_only(tmp_path: Path) -> None:
    """legacy 模式只读 legacy DB，不碰 canonical。"""
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    canonical = _make_canonical_db(tmp_path / "canonical.db")

    repo = ConversationRepository(source="legacy", legacy_db=legacy, canonical_db=canonical)
    assert repo.session_count() == 2

    sessions = list(repo.iter_sessions())
    sids = {s["session_id"] for s in sessions}
    assert sids == {"s1", "s2"}


def test_canonical_mode_reads_canonical_only(tmp_path: Path) -> None:
    """canonical 模式只读 canonical DB。"""
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    canonical = _make_canonical_db(tmp_path / "canonical.db")

    repo = ConversationRepository(source="canonical", legacy_db=legacy, canonical_db=canonical)
    assert repo.session_count() == 1

    sessions = list(repo.iter_sessions())
    assert sessions[0]["canonical_session_id"] == "cs1"


def test_invalid_source_rejected(tmp_path: Path) -> None:
    """非法 source 值被拒绝。"""
    import pytest
    with pytest.raises(ValueError):
        ConversationRepository(source="both", legacy_db=tmp_path / "l.db",
                                canonical_db=tmp_path / "c.db")


def test_canonical_turns_carry_source_refs(tmp_path: Path) -> None:
    """canonical turn 携带 source_ref 和 source_session_ref。"""
    canonical = _make_canonical_db(tmp_path / "canonical.db")
    repo = ConversationRepository(source="canonical", canonical_db=canonical)

    turns = list(repo.iter_turns("cs1"))
    assert len(turns) == 2
    assert turns[0].source == "canonical"
    assert turns[0].source_ref == "av:100"
    assert turns[0].source_session_ref == "canonical:cs1"
    assert turns[0].role == "user"


def test_tool_output_omitted(tmp_path: Path) -> None:
    """tool output 默认显示 [tool output omitted]。"""
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    canonical = _make_canonical_db(tmp_path / "canonical.db")

    # legacy
    repo_l = ConversationRepository(source="legacy", legacy_db=legacy, canonical_db=canonical)
    tools_l = list(repo_l.iter_tools("s1"))
    assert len(tools_l) == 1
    assert tools_l[0].output_display == TOOL_OUTPUT_OMITTED
    assert tools_l[0].tool_name == "Read"

    # canonical
    repo_c = ConversationRepository(source="canonical", legacy_db=legacy, canonical_db=canonical)
    tools_c = list(repo_c.iter_tools("cs1"))
    assert len(tools_c) == 1
    assert tools_c[0].output_display == TOOL_OUTPUT_OMITTED
    assert tools_c[0].tool_name == "Bash"


def test_no_silent_double_count(tmp_path: Path) -> None:
    """同一 repo 实例只读一个 source，不双计数。"""
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    canonical = _make_canonical_db(tmp_path / "canonical.db")

    repo = ConversationRepository(source="legacy", legacy_db=legacy, canonical_db=canonical)
    # legacy 只有 s1, s2
    assert repo.session_count() == 2
    # 切到 canonical 需要 new repo（不自动切换）
    assert repo.source == "legacy"


def test_session_source_refs_canonical_only(tmp_path: Path) -> None:
    """session_source_refs 只在 canonical 模式返回 lineage。"""
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    canonical = _make_canonical_db(tmp_path / "canonical.db")

    repo_c = ConversationRepository(source="canonical", legacy_db=legacy, canonical_db=canonical)
    refs = repo_c.session_source_refs("cs1")
    assert len(refs) == 2
    sources = {r["source"] for r in refs}
    assert sources == {"agentsview", "legacy"}

    repo_l = ConversationRepository(source="legacy", legacy_db=legacy, canonical_db=canonical)
    assert repo_l.session_source_refs("s1") == []


def test_user_turn_count(tmp_path: Path) -> None:
    """user_turn_count parity。"""
    legacy = _make_legacy_db(tmp_path / "legacy.db")
    canonical = _make_canonical_db(tmp_path / "canonical.db")

    repo_l = ConversationRepository(source="legacy", legacy_db=legacy, canonical_db=canonical)
    repo_c = ConversationRepository(source="canonical", legacy_db=legacy, canonical_db=canonical)
    assert repo_l.user_turn_count() == 2  # s1:1 + s2:1
    assert repo_c.user_turn_count() == 1  # cs1 ordinal 1


def test_missing_db_raises(tmp_path: Path) -> None:
    """数据库不存在时报 FileNotFoundError。"""
    import pytest
    repo = ConversationRepository(source="legacy", legacy_db=tmp_path / "nope.db")
    with pytest.raises(FileNotFoundError):
        repo.session_count()
