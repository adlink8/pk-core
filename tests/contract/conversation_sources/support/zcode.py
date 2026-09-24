"""zcode 家族原生夹具构建器（模块 ``zcode``，ADAPTER_VERSION 1.5.0）。

适配器认两种原生库形态：

* **在线形态** ``session`` / ``message`` / ``part``：时间戳是 epoch 毫秒，消息
  角色藏在 ``message.data.role``，part 内容藏在 ``data`` JSON 里；``type=timeline``
  的分隔记录（model_change / context_compaction / goal_verification /
  session_fork）必须各自留下一条带解释的事件。
* **可捕获形态** ``conversation_traces`` / ``conversation_parts``：走 allowlist 的
  :func:`capture_sqlite`；兄弟凭据表（``auth_tokens`` / ``accounts``）不得进入
  产物，只能留下 ``excluded_table:`` disposition。

正文全是合成句子，不含任何真实会话；可捕获形态里塞入 :data:`artifacts.CANARY`
与一个假邮箱，供 canary 断言使用。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from tests.contract.conversation_sources.support import artifacts

# 可捕获形态的 allowlist（与生产 ``zcode.ALLOWED_TABLES`` 一致）。
ALLOWED_TABLES: tuple[str, ...] = ("conversation_traces", "conversation_parts")
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "conversation_traces": ("trace_id", "title", "created_at"),
    "conversation_parts": ("part_id", "trace_id", "turn_id", "part_type",
                           "role", "content", "created_at"),
}

# 在线形态的时间戳基准（epoch 毫秒）。
_LIVE_CREATED = 1_700_000_000_000
_LIVE_UPDATED = 1_700_000_000_500

# 在线形态的合成正文常量：断言侧引用同一份，避免魔法字符串漂移。
LIVE_USER_TEXT = "fixture user text"
LIVE_ASSISTANT_TEXT = "fixture assistant text"


def oversized_text(prefix: str, length: int) -> str:
    """超限合成正文：显著超过适配器旧的内容上限。"""
    unit = prefix + "-"
    return (unit * (length // len(unit) + 1))[:length]


BIG_TOOL_INPUT = oversized_text("fixture-tool-arg", 60_000)
BIG_TOOL_OUTPUT = oversized_text("fixture-tool-out", 120_000)
BIG_REASONING = oversized_text("fixture-reasoning", 120_000)
BIG_COMPACTION = oversized_text("fixture-compaction", 5_000)


# ------------------------------------------------------------------- builders

def build_live_db(path: Path) -> None:
    """写出在线形态库：session / message / part，正文全是合成句子。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                title TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                directory TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT,
                sequence INTEGER
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT,
                session_id TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT,
                sequence INTEGER
            );
            """
        )
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-1", None, "fixture session", _LIVE_CREATED, _LIVE_UPDATED,
             "/tmp/fixture"),
        )
        messages = [
            ("msg-user", "sess-1", {"role": "user"}, 1),
            ("msg-assistant", "sess-1", {"role": "assistant"}, 2),
            ("msg-timeline", "sess-1", {"role": "assistant"}, 3),
        ]
        con.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
            [
                (mid, sid, _LIVE_CREATED, _LIVE_CREATED, json.dumps(data), seq)
                for mid, sid, data, seq in messages
            ],
        )
        parts = [
            ("part-user", "msg-user", {"type": "text", "text": LIVE_USER_TEXT}),
            ("part-assistant", "msg-assistant",
             {"type": "text", "text": LIVE_ASSISTANT_TEXT}),
            (
                "part-model",
                "msg-timeline",
                {
                    "type": "timeline",
                    "timelineType": "model_change",
                    "display": "separator",
                    "status": "completed",
                    "toModel": {"modelID": "fixture-model", "label": "Fixture Model"},
                },
            ),
            (
                "part-compact",
                "msg-timeline",
                {
                    "type": "timeline",
                    "timelineType": "context_compaction",
                    "display": "separator",
                    "status": "completed",
                    "compactReason": "user_requested",
                    "reason": "fixture compact note",
                },
            ),
            (
                "part-goal",
                "msg-timeline",
                {
                    "type": "timeline",
                    "timelineType": "goal_verification",
                    "display": "separator",
                    "status": "completed",
                    "verification": {"passed": True, "reason": "fixture goal note"},
                },
            ),
            (
                "part-fork",
                "msg-timeline",
                {
                    "type": "timeline",
                    "timelineType": "session_fork",
                    "display": "separator",
                    "status": "completed",
                    "parentSessionId": "parent-session",
                    "targetMessageId": "target-msg",
                    "restoredFileCount": 1,
                },
            ),
        ]
        con.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (pid, mid, "sess-1", _LIVE_CREATED + 400, _LIVE_CREATED + 400,
                 json.dumps(data), index)
                for index, (pid, mid, data) in enumerate(parts, start=1)
            ],
        )
        con.commit()
    finally:
        con.close()


def build_live_oversize_db(path: Path) -> None:
    """在线形态库：超限的工具入参/产出、reasoning 与 compaction 正文。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER, directory TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                time_updated INTEGER, data TEXT, sequence INTEGER
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT,
                sequence INTEGER
            );
            """
        )
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-big", None, "fixture oversize session", _LIVE_CREATED,
             _LIVE_UPDATED, "/tmp/fixture"),
        )
        messages = [("msg-big", "sess-big", {"role": "assistant"}, 1)]
        con.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
            [
                (mid, sid, _LIVE_CREATED, _LIVE_CREATED, json.dumps(data), seq)
                for mid, sid, data, seq in messages
            ],
        )
        parts = [
            ("part-big-tool", "msg-big",
             {"type": "tool", "tool": "fixture-tool",
              "state": {"input": BIG_TOOL_INPUT, "output": BIG_TOOL_OUTPUT}}),
            ("part-big-reasoning", "msg-big",
             {"type": "reasoning", "text": BIG_REASONING}),
            ("part-big-compact", "msg-big",
             {"type": "compaction", "text": BIG_COMPACTION}),
        ]
        con.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (pid, mid, "sess-big", _LIVE_CREATED + 400, _LIVE_CREATED + 400,
                 json.dumps(data), index)
                for index, (pid, mid, data) in enumerate(parts, start=1)
            ],
        )
        con.commit()
    finally:
        con.close()


def build_store_db(path: Path) -> None:
    """写出可捕获形态库，并附上兄弟凭据表（哨兵值 + 假邮箱）。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE conversation_traces (
                trace_id TEXT PRIMARY KEY, title TEXT, created_at TEXT
            );
            CREATE TABLE conversation_parts (
                part_id TEXT PRIMARY KEY, trace_id TEXT, turn_id TEXT,
                part_type TEXT, role TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE auth_tokens (
                token_id TEXT PRIMARY KEY, token_value TEXT
            );
            CREATE TABLE accounts (
                account_id TEXT PRIMARY KEY, email TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO conversation_traces VALUES (?, ?, ?)",
            ("tr_1", "zcode session", "2026-07-01T10:00:00Z"),
        )
        rows = [
            ("p1", "tr_1", "tn_1", "text", "user", "zcode prompt", "2026-07-01T10:00:01Z"),
            ("p2", "tr_1", "tn_1", "reasoning", "assistant", "thinking", "2026-07-01T10:00:02Z"),
            ("p3", "tr_1", "tn_1", "text", "assistant", "zcode answer", "2026-07-01T10:00:03Z"),
            ("p4", "tr_1", "tn_1", "tool", "assistant", "bash ls", "2026-07-01T10:00:04Z"),
            ("p5", "tr_1", "tn_1", "compaction", "assistant", "compacted", "2026-07-01T10:00:05Z"),
        ]
        con.executemany(
            "INSERT INTO conversation_parts VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        con.execute("INSERT INTO auth_tokens VALUES (?, ?)", ("tok_1", artifacts.CANARY))
        con.execute("INSERT INTO accounts VALUES (?, ?)", ("acc_1", "user@example.com"))
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ artifacts

def live_artifact(
    directory: Path,
    *,
    label: str = "zcode.live",
) -> tuple[SourceArtifact, Path]:
    """合成在线库字节 → 内容寻址 blob → ``(artifact, artifact_root)``。

    在线形态不走 capture seam（它没有 allowlist 捕获入口），只把库字节写成
    内容寻址 blob，产物 ``source_kind`` 仍是 ``sqlite``。
    """
    directory = Path(directory)
    db = directory / ".build" / f"{label}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    build_live_db(db)
    return artifacts.file_artifact(
        directory, label, "db.sqlite", db.read_bytes(),
        family="zcode", source_kind="sqlite",
    )


def live_oversize_artifact(
    directory: Path,
    *,
    label: str = "zcode.live.big",
) -> tuple[SourceArtifact, Path]:
    """超限正文的在线库字节 → ``(artifact, artifact_root)``。"""
    directory = Path(directory)
    db = directory / ".build" / f"{label}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    build_live_oversize_db(db)
    return artifacts.file_artifact(
        directory, label, "db.sqlite", db.read_bytes(),
        family="zcode", source_kind="sqlite",
    )


def store_artifact(
    directory: Path,
    *,
    db_name: str = "zcode.db",
) -> tuple[SourceArtifact, Path]:
    """合成可捕获库 → 真实 capture seam → ``(artifact, artifact_root)``。"""
    directory = Path(directory)
    db = directory / db_name
    build_store_db(db)
    return artifacts.captured_sqlite(
        db,
        directory,
        allowed_tables=ALLOWED_TABLES,
        allowed_columns=ALLOWED_COLUMNS,
        byte_limit=1_000_000,
        count_limit=8,
    )
