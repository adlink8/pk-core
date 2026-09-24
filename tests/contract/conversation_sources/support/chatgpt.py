"""chatgpt 家族原生 sqlite 夹具 builder（AgentsView pathless 兼容，合成、脱敏）。

本机没有 ChatGPT 的原生文件：该适配器只读 **AgentsView 形态的 sqlite**
（pathless 兼容通道）。夹具只构造最小合成行；可选的哨兵凭据表用来断言隐私
边界 —— 凭据值既不得被捕获进快照，也不得抵达事件、清单或日志。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import chatgpt as chatgpt_adapter

from tests.contract.conversation_sources.support import artifacts

# 合成最小行（正文为脱敏占位句）。
PATHLESS_SESSION_ID = "chat-1"
PATHLESS_USER_TEXT = "chatgpt prompt"

FLAT_SESSION_ID = "chat-fixture"
FLAT_FILLED_TEXT = "fixture-chatgpt-text"

_SESSION_COLUMNS = "(id, agent, started_at, ended_at, deleted_at, file_path)"
_MESSAGE_COLUMNS = (
    "(id, session_id, ordinal, role, content, timestamp, is_system, is_sidechain)"
)

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, agent TEXT, started_at TEXT,
    ended_at TEXT, deleted_at TEXT, file_path TEXT
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY, session_id TEXT, ordinal INTEGER,
    role TEXT, content TEXT, timestamp TEXT,
    is_system INTEGER, is_sidechain INTEGER
);
"""


def make_agentsview_db(
    path: Path,
    *,
    sessions: tuple = (),
    messages: tuple = (),
    canary: str | None = None,
) -> None:
    """建一份 AgentsView 形态的 sqlite：``sessions`` / ``messages``（+ 哨兵表）。

    ``canary`` 给出时额外建一张不在允许清单内的凭据表并塞入哨兵值：捕获阶段
    必须整表排除它，适配结果里不得出现该值。
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_SCHEMA)
        if canary is not None:
            con.execute(
                "CREATE TABLE credentials (id TEXT PRIMARY KEY, token TEXT)"
            )
            con.execute(
                "INSERT INTO credentials VALUES (?, ?)", ("cred-1", canary)
            )
        con.executemany(
            f"INSERT INTO sessions {_SESSION_COLUMNS} VALUES (?,?,?,?,?,?)",
            sessions,
        )
        con.executemany(
            f"INSERT INTO messages {_MESSAGE_COLUMNS} VALUES (?,?,?,?,?,?,?,?)",
            messages,
        )
        con.commit()
    finally:
        con.close()


def captured_agentsview(db: Path, store: Path, *, count_limit: int = 2):
    """经真实 capture seam 抓一份快照，返回 ``(artifact, artifact_root)``。

    允许清单直接引用生产模块的公开常量（``LIVE_ALLOWED_TABLES`` /
    ``LIVE_ALLOWED_COLUMNS``），保证夹具与适配器的声明不会分叉。
    """
    return artifacts.captured_sqlite(
        db,
        store,
        allowed_tables=chatgpt_adapter.LIVE_ALLOWED_TABLES,
        allowed_columns=chatgpt_adapter.LIVE_ALLOWED_COLUMNS,
        family=chatgpt_adapter.FAMILY,
        byte_limit=1_000_000,
        count_limit=count_limit,
    )
