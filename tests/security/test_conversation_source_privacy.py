"""Phase 62-03: conversation source privacy negatives (D-08).

Forbidden SQLite tables/columns (account, credential, token, auth, secret,
cookie, api_key) must never appear in executed capture trace callbacks,
snapshot manifests, event payloads, reports or logs — even when they share
the same database as the conversation tables. Also covers path escape and
WAL-consistent capture.

本文件留在 security 层：它只断言**隐私边界**，不重跑任何适配器的功能契约。
观察入口一律是公开 seam：

  - capture 走共享夹具 ``support/artifacts.captured_sqlite`` /
    ``captured_directory``（真实 capture seam，不手搓 artifact）；
  - 适配走族级 seam ``registry.detect_family`` / ``registry.adapt_for``，
    不直接调 ``chatgpt.adapt(...)`` 这类模块函数。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import chatgpt
from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.snapshots import CaptureError
from tests.contract.conversation_sources.support import artifacts

# 塞进夹具库的哨兵值：它出现任何地方都算隐私泄漏。
CANARY = artifacts.CANARY

# 观察用的家族与它的 live 允许清单（引用生产常量，夹具与声明不分叉）。
FAMILY = chatgpt.FAMILY
ALLOWED_TABLES = chatgpt.LIVE_ALLOWED_TABLES
ALLOWED_COLUMNS = chatgpt.LIVE_ALLOWED_COLUMNS

_SESSIONS_DDL = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, started_at TEXT, "
    "ended_at TEXT, deleted_at TEXT, file_path TEXT)"
)
_MESSAGES_DDL = (
    "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, ordinal INTEGER, "
    "role TEXT, content TEXT, timestamp TEXT, is_system INTEGER, is_sidechain INTEGER)"
)

# 与允许清单同一份库里的禁表：account / credential / token / auth / secret 各一支。
_FORBIDDEN_DDL = (
    "CREATE TABLE auth_tokens (id TEXT PRIMARY KEY, token_value TEXT)",
    "CREATE TABLE api_credentials (id TEXT PRIMARY KEY, api_key TEXT)",
    "CREATE TABLE user_accounts (id TEXT PRIMARY KEY, email TEXT)",
    "CREATE TABLE sessions_secret_store (id TEXT PRIMARY KEY, secret TEXT)",
)


# ----------------------------------------------------------------- fixtures


def _make_full_db(path: Path) -> None:
    """允许清单内的会话表 + 邻接的凭据/账号表（全部合成、脱敏）。"""
    con = sqlite3.connect(path)
    try:
        con.execute(_SESSIONS_DDL)
        con.execute(_MESSAGES_DDL)
        for ddl in _FORBIDDEN_DDL:
            con.execute(ddl)
        con.execute(
            "INSERT INTO sessions VALUES ('s1','chatgpt','2026-07-01T10:00:00Z',"
            "'2026-07-01T10:00:05Z',NULL,NULL)"
        )
        con.execute(
            "INSERT INTO messages VALUES ('m1','s1',1,'user',?,"
            "'2026-07-01T10:00:01Z',0,0)",
            (artifacts.USER_TEXT,),
        )
        con.execute("INSERT INTO auth_tokens VALUES ('t1', ?)", (CANARY,))
        con.execute("INSERT INTO api_credentials VALUES ('a1', ?)", (CANARY,))
        con.execute("INSERT INTO sessions_secret_store VALUES ('s1', ?)", (CANARY,))
        con.execute("INSERT INTO user_accounts VALUES ('u1','user@example.com')")
        con.commit()
    finally:
        con.close()


def _capture(db: Path, store: Path):
    """经真实 capture seam（共享夹具）抓一份快照，返回 ``(artifact, root)``。"""
    return artifacts.captured_sqlite(
        db,
        store,
        allowed_tables=ALLOWED_TABLES,
        allowed_columns=ALLOWED_COLUMNS,
        family=FAMILY,
        byte_limit=1_000_000,
        count_limit=4,
    )


def _blob_of(artifact, root: Path) -> Path:
    """发布件 blob：内容寻址（``content_hash[:32]``）。"""
    return root / artifact.content_hash[:32]


def _adapt(artifact, root: Path):
    """族级 seam：适配只经 ``registry.adapt_for`` 一个入口。"""
    return registry.adapt_for(
        FAMILY, SourceArtifactSet((artifact,)), artifact_root=root
    )


def _public_surface(result) -> str:
    """一次适配对外可见的全部文本面：事件载荷 + 报告/警告。"""
    return "\n".join(
        (artifacts.event_text(result), " ".join(result.warnings), str(result))
    )


# ------------------------------------------------------- 禁止的表/列不可发布


class TestForbiddenTables:
    def test_forbidden_tables_never_in_artifact(self, tmp_path):
        db = tmp_path / "sessions.db"
        _make_full_db(db)
        artifact, root = _capture(db, tmp_path / "capture")
        # 该快照仍被属主家族认领；registry 是唯一的族级入口。
        assert registry.detect_family(FAMILY, artifact, artifact_root=root) is True
        con = sqlite3.connect(_blob_of(artifact, root))
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
        assert "auth_tokens" not in tables
        assert "api_credentials" not in tables
        assert "user_accounts" not in tables
        assert "sessions_secret_store" not in tables
        assert "sessions" in tables
        assert "messages" in tables
        # 发布件仍可适配，且事件/报告里不得出现哨兵值。
        assert CANARY not in _public_surface(_adapt(artifact, root))

    def test_canary_secret_never_in_filtered_blob(self, tmp_path):
        db = tmp_path / "sessions.db"
        _make_full_db(db)
        artifact, root = _capture(db, tmp_path / "capture")
        assert CANARY.encode() not in _blob_of(artifact, root).read_bytes()

    def test_manifest_reports_exclusions_metadata_only(self, tmp_path):
        db = tmp_path / "sessions.db"
        _make_full_db(db)
        artifact, root = _capture(db, tmp_path / "capture")
        for disposition in artifact.privacy_dispositions:
            assert disposition.startswith("excluded_table:")
        assert "auth_tokens" in " ".join(artifact.privacy_dispositions)
        assert CANARY not in str(artifact)
        # 清单里只有表名，没有值；适配结果同样不得携带哨兵值。
        assert CANARY not in _public_surface(_adapt(artifact, root))

    def test_declaring_forbidden_table_fails_closed(self, tmp_path):
        db = tmp_path / "sessions.db"
        _make_full_db(db)
        with pytest.raises(CaptureError):
            artifacts.captured_sqlite(
                db,
                tmp_path / "capture",
                allowed_tables=("auth_tokens",),
                allowed_columns={"auth_tokens": ("id", "token_value")},
                family=FAMILY,
                byte_limit=1_000_000,
                count_limit=4,
            )

    def test_forbidden_column_never_captured(self, tmp_path):
        db = tmp_path / "sessions.db"
        _make_full_db(db)
        # 名字不带禁词的邻接表里也有凭据列：不在允许清单内的表一律整表排除。
        con = sqlite3.connect(db)
        try:
            con.execute(
                "CREATE TABLE usage_stats (id TEXT PRIMARY KEY, api_key TEXT, n INTEGER)"
            )
            con.execute("INSERT INTO usage_stats VALUES ('u1', ?, 3)", (CANARY,))
            con.commit()
        finally:
            con.close()
        artifact, root = _capture(db, tmp_path / "capture")
        assert CANARY.encode() not in _blob_of(artifact, root).read_bytes()
        assert CANARY not in _public_surface(_adapt(artifact, root))

    def test_undeclared_column_on_allowed_table_is_not_published(self, tmp_path):
        db = tmp_path / "sessions.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE sessions "
            "(id TEXT PRIMARY KEY, agent TEXT, started_at TEXT, ended_at TEXT, "
            "deleted_at TEXT, file_path TEXT, vendor_private TEXT)"
        )
        con.execute(_MESSAGES_DDL)
        con.execute(
            "INSERT INTO sessions VALUES ('s1','chatgpt','2026-07-01T10:00:00Z',"
            "'2026-07-01T10:00:05Z',NULL,NULL,?)",
            (CANARY,),
        )
        con.commit()
        con.close()
        artifact, root = _capture(db, tmp_path / "capture")
        con = sqlite3.connect(_blob_of(artifact, root))
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        finally:
            con.close()
        assert columns == set(ALLOWED_COLUMNS["sessions"])
        assert CANARY.encode() not in _blob_of(artifact, root).read_bytes()
        assert CANARY not in _public_surface(_adapt(artifact, root))

    def test_autoincrement_system_table_is_scrubbed_not_dropped(self, tmp_path):
        db = tmp_path / "sessions.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE sessions "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, started_at TEXT, "
            "ended_at TEXT, deleted_at TEXT, file_path TEXT)"
        )
        con.execute(
            "CREATE TABLE cache_rows (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT)"
        )
        con.execute(_MESSAGES_DDL)
        con.execute("INSERT INTO sessions(agent) VALUES ('chatgpt')")
        con.execute("INSERT INTO cache_rows(body) VALUES (?)", (CANARY,))
        con.commit()
        con.close()
        artifact, root = _capture(db, tmp_path / "capture")
        con = sqlite3.connect(_blob_of(artifact, root))
        try:
            has_sequence = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
            ).fetchone()
            sequence_names = (
                {row[0] for row in con.execute("SELECT name FROM sqlite_sequence")}
                if has_sequence
                else set()
            )
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
        assert sequence_names <= {"sessions"}
        assert "cache_rows" not in tables
        assert CANARY.encode() not in _blob_of(artifact, root).read_bytes()
        assert CANARY not in _public_surface(_adapt(artifact, root))


# ------------------------------------------------------------- 路径与 WAL


class TestPathAndWAL:
    def test_path_escape_rejected(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "conversation.jsonl").write_text(
            '{"type":"session_meta","session_id":"s"}\n', encoding="utf-8"
        )
        # allowlist 相对路径逃出 source root：必须在发布任何 artifact 前 fail closed。
        with pytest.raises(CaptureError):
            artifacts.captured_directory(
                source,
                tmp_path / "dest",
                include_relative=("../../outside.jsonl",),
                byte_limit=1_000_000,
                count_limit=1,
            )

    def test_sqlite_wal_backup_not_loose_copy(self, tmp_path):
        db = tmp_path / "sessions.db"
        con = sqlite3.connect(db)
        try:
            con.execute(_SESSIONS_DDL)
            con.execute(_MESSAGES_DDL)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "INSERT INTO sessions VALUES ('s1','chatgpt',NULL,NULL,NULL,NULL)"
            )
            con.commit()
        finally:
            con.close()
        artifact, root = _capture(db, tmp_path / "capture")
        # 发布件必须是自洽的 filtered 数据库（online backup 的一致读点），
        # 而不是 live .db / -wal / -shm 的松散复制：WAL 里刚落盘的会话必须可见。
        con = sqlite3.connect(_blob_of(artifact, root))
        try:
            rows = con.execute("SELECT id FROM sessions WHERE id='s1'").fetchall()
        finally:
            con.close()
        assert rows == [("s1",)]
        assert CANARY not in _public_surface(_adapt(artifact, root))
