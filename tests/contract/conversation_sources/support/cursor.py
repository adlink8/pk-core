"""Cursor 家族（生产模块 ``cursor``，ADAPTER_VERSION 1.0.0）原生夹具 builder。

只构造合成 / 脱敏字节，正文全是假句子，不读权威库。sqlite 夹具只声明会话
表；捕获 seam 会把凭据 / account 之类的表挡在允许清单外（privacy 断言依赖
这一点）。测试文件经 registry seam 消费这些产物。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# 支持的 v1 schema：threads + messages。
SQLITE_TABLES = ("threads", "messages")
SQLITE_COLUMNS = {
    "threads": ("id", "title", "created_at"),
    "messages": ("id", "role", "content", "created_at"),
}

# 只有 attribution 表：不匹配任何 probe 版本，必须 fail closed。
ATTRIBUTION_TABLES = ("attribution",)
ATTRIBUTION_COLUMNS = {"attribution": ("id", "name")}

# 真实 transcript 落在 projects/<proj>/agent-transcripts/<id>/<id>.jsonl。
JSONL_RELATIVE_PATH = "projects/fixture/agent-transcripts/abc/abc.jsonl"


def make_cursor_db(path: Path, *, attribution_only: bool = False) -> None:
    """写一个 Cursor 本地库：受支持的 threads/messages，或 attribution-only。"""
    con = sqlite3.connect(path)
    try:
        if attribution_only:
            con.execute("CREATE TABLE attribution (id TEXT PRIMARY KEY, name TEXT)")
            con.execute("INSERT INTO attribution VALUES ('a1', 'attribution-only')")
        else:
            con.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, created_at TEXT);
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at TEXT
                );
                """
            )
            con.execute(
                "INSERT INTO threads VALUES ('t1', 'cursor thread', '2026-07-01T10:00:00Z')"
            )
            con.execute(
                "INSERT INTO messages VALUES ('m1', 'user', 'cursor prompt', '2026-07-01T10:00:01Z')"
            )
            con.execute(
                "INSERT INTO messages VALUES ('m2', 'assistant', 'cursor answer', '2026-07-01T10:00:02Z')"
            )
        con.commit()
    finally:
        con.close()


def write_cursor_transcript(
    directory: Path,
    rows: list[dict],
    *,
    relative_path: str = JSONL_RELATIVE_PATH,
) -> Path:
    """把合成 JSONL rows 写成一个文件并返回源路径，供 ``capture_file`` 抓取。"""
    src = directory / Path(relative_path).name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return src
