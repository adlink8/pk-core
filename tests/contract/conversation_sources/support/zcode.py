"""zcode 家族原生夹具构建器（模块 ``zcode``，ADAPTER_VERSION 1.7.0）。

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

# 切片 1：真实 ``file`` part 顶层没有可读文本，正文只在嵌套槽位
# ``source.text.value``（少数记录另有 ``metadata.preview.text``）。
FILE_SOURCE_TEXT = "fixture attached file body"
FILE_PREVIEW_TEXT = "fixture attachment preview"

# 切片 2：三种「原生确实为空」的记录。
SUMMARY_POINTER = "msg-fixture-summary"
SUMMARY_TEXT = "fixture compaction summary body"
ATTACHMENT_FILE_PART = "part-attach-file"

_LIVE_SCHEMA = """
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
        time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER
    );
"""


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


def _write_live_db(
    path: Path,
    *,
    session_id: str,
    title: str,
    messages: list[tuple[str, str]],
    parts: list[tuple[str, str, dict]],
) -> None:
    """迷你在线形态库：``messages`` 是 ``(message_id, role)``，``parts`` 是 ``(part_id, message_id, data)``。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(_LIVE_SCHEMA)
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, None, title, _LIVE_CREATED, _LIVE_UPDATED, "/tmp/fixture"),
        )
        con.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
            [
                (mid, session_id, _LIVE_CREATED, _LIVE_CREATED,
                 json.dumps({"role": role}), seq)
                for seq, (mid, role) in enumerate(messages, start=1)
            ],
        )
        con.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (pid, mid, session_id, _LIVE_CREATED + 400, _LIVE_CREATED + 400,
                 json.dumps(data), seq)
                for seq, (pid, mid, data) in enumerate(parts, start=1)
            ],
        )
        con.commit()
    finally:
        con.close()


def build_live_file_db(path: Path) -> None:
    """在线形态库：``file`` part 的正文只在 ``source.text.value`` / ``metadata.preview.text``。"""
    _write_live_db(
        path,
        session_id="sess-file",
        title="fixture file session",
        messages=[("msg-file", "user")],
        parts=[
            (
                "part-file-source",
                "msg-file",
                {
                    "type": "file",
                    "filename": "fixture-source.txt",
                    "source": {"type": "text", "text": {"value": FILE_SOURCE_TEXT}},
                },
            ),
            (
                "part-file-both",
                "msg-file",
                {
                    "type": "file",
                    "filename": "fixture-both.txt",
                    "source": {"type": "text", "text": {"value": FILE_SOURCE_TEXT}},
                    "metadata": {"preview": {"text": FILE_PREVIEW_TEXT}},
                },
            ),
            (
                "part-file-preview",
                "msg-file",
                {
                    "type": "file",
                    "filename": "fixture-preview.txt",
                    "metadata": {"preview": {"text": FILE_PREVIEW_TEXT}},
                },
            ),
        ],
    )


def build_live_honest_empty_db(path: Path) -> None:
    """在线形态库：三种「原生确实为空」的记录（正文不可恢复，但必须留下解释）。"""
    _write_live_db(
        path,
        session_id="sess-honest",
        title="fixture honest empty session",
        messages=[
            ("msg-think", "assistant"),
            ("msg-compact-meta", "assistant"),
            ("msg-summary", "assistant"),
            ("msg-attach", "user"),
        ],
        parts=[
            (
                "part-think-empty",
                "msg-think",
                {
                    "type": "reasoning",
                    "text": "",
                    "metadata": {"itemId": "fixture-thinking-item"},
                },
            ),
            (
                "part-compact-meta",
                "msg-compact-meta",
                {
                    "type": "compaction",
                    "auto": True,
                    "trigger": "auto",
                    "phase": "end",
                    "compactReason": "fixture window full",
                    "compactBoundary": {"messageId": SUMMARY_POINTER},
                    "summaryMessageId": SUMMARY_POINTER,
                },
            ),
            ("part-summary-text", "msg-summary", {"type": "text", "text": SUMMARY_TEXT}),
            ("part-attach-text", "msg-attach", {"type": "text", "text": ""}),
            (
                ATTACHMENT_FILE_PART,
                "msg-attach",
                {
                    "type": "file",
                    "filename": "fixture-attach.txt",
                    "source": {"type": "text", "text": {"value": FILE_SOURCE_TEXT}},
                },
            ),
        ],
    )


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


def live_file_artifact(
    directory: Path,
    *,
    label: str = "zcode.live.file",
) -> tuple[SourceArtifact, Path]:
    """``file`` part 嵌套正文的在线库 → ``(artifact, artifact_root)``。"""
    directory = Path(directory)
    db = directory / ".build" / f"{label}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    build_live_file_db(db)
    return artifacts.file_artifact(
        directory, label, "db.sqlite", db.read_bytes(),
        family="zcode", source_kind="sqlite",
    )


def live_honest_empty_artifact(
    directory: Path,
    *,
    label: str = "zcode.live.honest",
) -> tuple[SourceArtifact, Path]:
    """三种「原生确实为空」记录的在线库 → ``(artifact, artifact_root)``。"""
    directory = Path(directory)
    db = directory / ".build" / f"{label}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    build_live_honest_empty_db(db)
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


# ------------------------------------- 丢失可见性（在线库）：混存时间戳等

# 混存时间戳基准：session.time_updated 是 epoch 毫秒 int（2030-01-01），
# message.time_updated 是 ISO 字符串（更早但字典序更大）。字典序折叠会
# 选错 ended_at；先 normalize 再比较才能得到 2030。
MIXED_SESSION_UPDATED_MS = 1_893_456_000_000   # -> 2030-01-01T00:00:00Z
MIXED_MESSAGE_UPDATED_ISO = "2023-11-14T22:13:21Z"
EXPECTED_ENDED_AT_ISO = "2030-01-01T00:00:00Z"

# usage 夹具断言值。
LOSS_TOKENS_AGGREGATE_SUMMARY = (
    "input_tokens=10 output_tokens=20 cache_read=1 cache_write=2 total_tokens=33"
)


def build_live_loss_db(path: Path) -> None:
    """在线形态库：混存时间戳、坏 JSON 载荷、usage 裸词收敛、孤儿 part。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(_LIVE_SCHEMA)
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-1", None, "fixture session",
             1_700_000_000_000, MIXED_SESSION_UPDATED_MS, "/tmp/fixture"),
        )
        con.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("msg-user", "sess-1",
                 1_700_000_000_000, MIXED_MESSAGE_UPDATED_ISO,
                 json.dumps({"role": "user"}), 1),
            ],
        )
        parts = [
            # 顶层裸词数字：usage 收敛前会被误报成 USAGE。
            ("part-bare", "msg-user", "sess-1", 1_700_000_000_000, None,
             json.dumps({"type": "text", "text": LIVE_USER_TEXT,
                         "input": 123, "read": 5}), 2),
            # tokens 聚合：裸词在 token 上下文里照常映射。
            ("part-tokens", "msg-user", "sess-1", 1_700_000_000_000, None,
             json.dumps({"type": "text", "text": "fixture tokens body",
                         "tokens": {"input": 10, "output": 20,
                                    "cache": {"read": 1, "write": 2},
                                    "total": 33}}), 3),
            # 坏 JSON 载荷：必须计数进 warnings。
            ("part-bad", "msg-user", "sess-1", 1_700_000_000_000, None,
             "{not json", 4),
            # 孤儿：父消息不在库里（在线形态会落成带解释的 unknown 事件）。
            ("part-orphan", "msg-missing", "sess-1", 1_700_000_000_000, None,
             json.dumps({"type": "text", "text": "fixture orphan body"}), 5),
        ]
        con.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?, ?)", parts
        )
        con.commit()
    finally:
        con.close()


def live_loss_artifact(
    directory: Path,
    *,
    label: str = "zcode.live.loss",
) -> tuple[SourceArtifact, Path]:
    """丢失可见性在线库字节 -> ``(artifact, artifact_root)``。"""
    directory = Path(directory)
    db = directory / ".build" / f"{label}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    build_live_loss_db(db)
    return artifacts.file_artifact(
        directory, label, "db.sqlite", db.read_bytes(),
        family="zcode", source_kind="sqlite",
    )
