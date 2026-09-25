"""mimo / opencode 的模块原生夹具与选择器。

一个生产模块（``personal_knowledge.adapters.conversation_sources.mimo_opencode``）
服务两家客户端，共享一个 ``ADAPTER_VERSION``，所以夹具集中在这一个 support 文件。

两种原生形状都要覆盖：

* **live** —— ``session`` / ``message`` / ``part``，正文藏在 ``part.data`` 的 JSON
  里（``data`` 字段），不是 ``message.data``；
* **visible** —— ``sessions`` / ``messages`` / ``message_parts``，库旁还摆着一张
  ``api_credentials`` 凭据表，必须经真实 capture allowlist 抓取才可能读到。

夹具是合成库：会话正文是假句子，凭据列里塞 :data:`artifacts.CANARY` 哨兵值 ——
它出现在事件、清单或日志里都算泄漏。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from tests.contract.conversation_sources.support import artifacts

# 本模块服务的家族（共享同一个 ADAPTER_VERSION）。
FAMILIES = ("mimo", "opencode")

# live 形状的合成正文（不复制真实会话）。
LIVE_USER_TEXT = "fixture-user-text"
LIVE_ASSISTANT_TEXT = "fixture-assistant-text"
LIVE_REASONING_TEXT = "fixture-reasoning-plain"
LIVE_SUBTASK_PROMPT = "fixture-subtask-prompt"

# visible 形状的合成正文。
VISIBLE_SESSION_TITLE = "mimo session"
VISIBLE_USER_TEXT = "mimo prompt"
VISIBLE_ASSISTANT_TEXT = "mimo answer"
VISIBLE_REASONING_TEXT = "mimo thinking"


def oversized_text(prefix: str, length: int) -> str:
    """显著超过适配器旧内容上限（工具 50k / 推理 100k）的合成正文。"""
    unit = prefix + "-"
    return (unit * (length // len(unit) + 1))[:length]


BIG_TOOL_INPUT = oversized_text("fixture-mimo-arg", 60_000)
BIG_TOOL_OUTPUT = oversized_text("fixture-mimo-out", 120_000)
BIG_REASONING = oversized_text("fixture-mimo-reasoning", 120_000)
BIG_COMPACTION = oversized_text("fixture-mimo-compaction", 5_000)

VISIBLE_ALLOWED_TABLES: tuple[str, ...] = ("sessions", "messages", "message_parts")
VISIBLE_ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": ("id", "title", "created_at"),
    "messages": ("id", "session_id", "role", "content", "created_at"),
    "message_parts": ("id", "message_id", "part_type", "content", "created_at"),
}


# ------------------------------------------------------------------- live 形状

def _write_live_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                title TEXT,
                time_created TEXT,
                time_updated TEXT,
                time_compacting TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                time_created TEXT,
                time_updated TEXT,
                data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT,
                session_id TEXT,
                time_created TEXT,
                time_updated TEXT,
                data TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("s-chat", None, "chat", "2026-07-01T00:00:00Z", "2026-07-01T00:00:01Z", None),
        )
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("s-empty", None, "empty", "2026-07-01T00:00:02Z", "2026-07-01T00:00:02Z", None),
        )
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("m-user", "s-chat", "2026-07-01T00:00:03Z", "2026-07-01T00:00:03Z",
             json.dumps({"role": "user"})),
        )
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("m-asst", "s-chat", "2026-07-01T00:00:04Z", "2026-07-01T00:00:04Z",
             json.dumps({"role": "assistant"})),
        )
        parts = [
            ("p-user", "m-user", {"type": "text", "text": LIVE_USER_TEXT}),
            ("p-asst", "m-asst", {"type": "text", "text": LIVE_ASSISTANT_TEXT}),
            ("p-reason-enc", "m-asst", {
                "type": "reasoning",
                "text": "",
                "metadata": {"openai": {"reasoningEncryptedContent": "enc-blob"}},
            }),
            ("p-reason-plain", "m-asst", {
                "type": "reasoning",
                "text": LIVE_REASONING_TEXT,
            }),
            ("p-subtask", "m-asst", {"type": "subtask", "prompt": LIVE_SUBTASK_PROMPT}),
            ("p-patch", "m-asst", {"type": "patch", "hash": "h1", "files": ["a.txt"]}),
        ]
        for part_id, message_id, payload in parts:
            con.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (part_id, message_id, "s-chat", "2026-07-01T00:00:05Z",
                 "2026-07-01T00:00:05Z", json.dumps(payload)),
            )
        con.commit()
    finally:
        con.close()


def _sqlite_artifact(
    root: Path, *, digest: str, artifact_id: str, family: str, relative_path: str,
) -> SourceArtifact:
    return SourceArtifact(
        artifact_id=artifact_id,
        family=family,
        source_kind="sqlite",
        content_hash=digest,
        capture_method="fixture",
        relative_path=relative_path,
        byte_size=(root / digest[:32]).stat().st_size,
    )


def live_store(tmp_path: Path, family: str) -> tuple[SourceArtifact, Path]:
    """一份 live 形状合成库 -> ``(artifact, artifact_root)``。"""
    label = f"mimo_opencode.live.{family}"
    root = Path(tmp_path) / family
    root.mkdir(parents=True, exist_ok=True)
    digest = artifacts.blob_name(label)
    blob = root / digest[:32]
    _write_live_db(blob)
    artifact = _sqlite_artifact(
        root, digest=digest, artifact_id=f"art-{label}", family=family,
        relative_path=f"{family}.db",
    )
    return artifact, root


# --------------------------------------------------- 超限正文（live 形状）

def _write_live_oversize_db(path: Path) -> None:
    """live 形状库：工具入参/产出、推理与 compaction 正文全部超旧上限。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
                time_created TEXT, time_updated TEXT, time_compacting TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT, time_created TEXT,
                time_updated TEXT, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created TEXT, time_updated TEXT, data TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("s-big", None, "chat", "2026-07-01T00:00:00Z", "2026-07-01T00:00:01Z", None),
        )
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("m-big", "s-big", "2026-07-01T00:00:03Z", "2026-07-01T00:00:03Z",
             json.dumps({"role": "assistant"})),
        )
        parts = [
            ("p-big-tool", {"type": "tool", "tool": "fixture-tool",
                            "state": {"input": BIG_TOOL_INPUT,
                                      "output": BIG_TOOL_OUTPUT}}),
            ("p-big-reason", {"type": "reasoning", "text": BIG_REASONING}),
            ("p-big-compact", {"type": "compaction", "text": BIG_COMPACTION}),
        ]
        for part_id, payload in parts:
            con.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (part_id, "m-big", "s-big", "2026-07-01T00:00:05Z",
                 "2026-07-01T00:00:05Z", json.dumps(payload)),
            )
        con.commit()
    finally:
        con.close()


def live_oversize_store(tmp_path: Path, family: str) -> tuple[SourceArtifact, Path]:
    """超限正文 live 库 -> ``(artifact, artifact_root)``。"""
    label = f"mimo_opencode.live.big.{family}"
    root = Path(tmp_path) / f"{family}-big"
    root.mkdir(parents=True, exist_ok=True)
    digest = artifacts.blob_name(label)
    blob = root / digest[:32]
    _write_live_oversize_db(blob)
    artifact = _sqlite_artifact(
        root, digest=digest, artifact_id=f"art-{label}", family=family,
        relative_path=f"{family}-big.db",
    )
    return artifact, root


# ---------------------------------------------------------------- visible 形状

def _write_visible_db(path: Path, *, canary: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, created_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, created_at TEXT
            );
            CREATE TABLE message_parts (
                id TEXT PRIMARY KEY, message_id TEXT, part_type TEXT,
                content TEXT, created_at TEXT
            );
            CREATE TABLE api_credentials (
                id TEXT PRIMARY KEY, api_key TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            ("s_1", VISIBLE_SESSION_TITLE, "2026-07-01T10:00:00Z"),
        )
        con.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("m1", "s_1", "user", VISIBLE_USER_TEXT, "2026-07-01T10:00:01Z"),
        )
        con.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("m2", "s_1", "assistant", VISIBLE_ASSISTANT_TEXT, "2026-07-01T10:00:02Z"),
        )
        con.execute(
            "INSERT INTO message_parts VALUES (?, ?, ?, ?, ?)",
            ("mp1", "m2", "reasoning", VISIBLE_REASONING_TEXT, "2026-07-01T10:00:03Z"),
        )
        con.execute("INSERT INTO api_credentials VALUES (?, ?)", ("c1", canary))
        con.commit()
    finally:
        con.close()


def captured_store(
    tmp_path: Path, *, label: str = "mimo_opencode.visible",
) -> tuple[SourceArtifact, Path]:
    """经真实 capture seam 抓一份 visible 形状合成库 -> ``(artifact, artifact_root)``。

    allowlist 只放会话表，相邻的 ``api_credentials`` 因此不进 artifact：
    凭据值永远到不了事件。
    """
    db = Path(tmp_path) / f"{label}.source.db"
    _write_visible_db(db, canary=artifacts.CANARY)
    return artifacts.captured_sqlite(
        db, Path(tmp_path) / label,
        allowed_tables=VISIBLE_ALLOWED_TABLES,
        allowed_columns=VISIBLE_ALLOWED_COLUMNS,
        byte_limit=1_000_000,
        count_limit=8,
    )


# ------------------------------------------------------------------- 选择器

def event_named(result, native_id: str):
    """native_event_id 恰好等于 ``native_id`` 的事件。"""
    return next(
        event for event in result.events
        if event.provenance.native_event_id == native_id
    )


# ------------------------------------------- 丢失可见性（live 形状专用夹具）

# 系统注入的脚手架标题：必须被滤掉，不许成为 session title。
LOSS_PLACEHOLDER_TITLE = "<INSTRUCTIONS> agents.md scaffold for the agent"

# usage 夹具断言值：usage 字典与 tokens 聚合各自映射出的规范串。
LOSS_USAGE_DICT_SUMMARY = "input_tokens=5 output_tokens=6"
LOSS_TOKENS_AGGREGATE_SUMMARY = (
    "input_tokens=10 output_tokens=20 cache_read=1 cache_write=2 total_tokens=33"
)


def _write_live_loss_db(path: Path) -> None:
    """live 形状库：孤儿 part、坏 JSON 载荷、usage 裸词收敛、占位标题。

    覆盖四类「以前静默或误报」的形态：

    * ``p-orphan`` 指向不存在的 message —— 必须计数进 warnings；
    * ``m-bad`` 的 ``data`` 不是合法 JSON —— 必须计数进 warnings；
    * ``p-bare`` 顶层裸词 ``input`` / ``read`` 数字 —— 不得伪造 USAGE 事件；
    * ``m-ok`` 的 ``usage`` 字典与 ``p-tokens`` 的 ``tokens`` 聚合 ——
      裸词在 token 上下文里必须照常映射成规范 usage。
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
                time_created TEXT, time_updated TEXT, time_compacting TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT,
                time_created TEXT, time_updated TEXT, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created TEXT, time_updated TEXT, data TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            ("s-loss", None, LOSS_PLACEHOLDER_TITLE,
             "2026-07-01T00:00:00Z", "2026-07-01T00:00:01Z", None),
        )
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("m-ok", "s-loss", "2026-07-01T00:00:03Z", "2026-07-01T00:00:03Z",
             json.dumps({
                 "role": "user",
                 "input": 9,  # 顶层裸词，不在 token 上下文：不得计数
                 "usage": {"input": 5, "output": 6},
             })),
        )
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("m-bad", "s-loss", "2026-07-01T00:00:04Z", "2026-07-01T00:00:04Z",
             "{not json"),
        )
        parts = [
            # 孤儿：父消息不在 artifact 里。
            ("p-orphan", "m-missing", "s-loss",
             {"type": "text", "text": "fixture orphan body"}),
            # 顶层裸词数字：usage 收敛前会被误报成 USAGE。
            ("p-bare", "m-ok", "s-loss",
             {"type": "text", "text": "fixture bare body",
              "input": 123, "read": 5}),
            # tokens 聚合：裸词在 token 上下文里照常映射。
            ("p-tokens", "m-bad", "s-loss",
             {"type": "text", "text": "fixture tokens body",
              "tokens": {"input": 10, "output": 20,
                         "cache": {"read": 1, "write": 2}, "total": 33}}),
        ]
        for part_id, message_id, session_id, payload in parts:
            con.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (part_id, message_id, session_id, "2026-07-01T00:00:05Z",
                 "2026-07-01T00:00:05Z", json.dumps(payload)),
            )
        con.commit()
    finally:
        con.close()


def live_loss_store(tmp_path: Path, family: str) -> tuple[SourceArtifact, Path]:
    """丢失可见性 live 库 -> ``(artifact, artifact_root)``。"""
    label = f"mimo_opencode.live.loss.{family}"
    root = Path(tmp_path) / f"{family}-loss"
    root.mkdir(parents=True, exist_ok=True)
    digest = artifacts.blob_name(label)
    blob = root / digest[:32]
    _write_live_loss_db(blob)
    artifact = _sqlite_artifact(
        root, digest=digest, artifact_id=f"art-{label}", family=family,
        relative_path=f"{family}-loss.db",
    )
    return artifact, root
