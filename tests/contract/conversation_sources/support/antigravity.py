"""Antigravity（family ``antigravity``）专用夹具构造器。

live 库把正文放在 ``steps.step_payload`` 的二进制 protobuf ``Step`` 消息里，
磁盘上不带 ``.proto`` schema；这里用 ``protobuf_wire.encode_field`` 包装出最小
字段构造器，字段编号与 ``antigravity._decode_live_step`` 的字段表一一对应：

    step_type 14   f19/f2 用户句；f19/f7|f9|f11 附件（f1 = URI，f11/f2/f1 正文）
    step_type 15   f20/f1 助手句（f20/f8 是镜像，跳过）；f20/f3 推理；
                   f20/f7 工具调用；f5/f9 token 计数
    step_type 132  f5/f4 调用标识；f140/f1 执行注解；f140/f2/f1 工具结果
    step_type 17   f24/f3 执行错误
    step_type 23   f30/f5 压缩正文；f30/f4 会话标题
    step_type 101  f114/f2/f1 标题、f114/f2/f2 正文

约定：夹具 blob 写在 ``<root>/<content_hash[:32]>``，因为适配器就是按这个
路径读回字节。所有正文都是合成句子，不复制真实会话；legacy 夹具库里塞
:data:`support.artifacts.CANARY`，用来断言凭据不抵达事件。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.adapters.conversation_sources.protobuf_wire import encode_field

from tests.contract.conversation_sources.support.artifacts import CANARY

# live 夹具的 content_hash：[:32] 同时是 blob 文件名（见模块 docstring）。
LIVE_HASH = "a" * 64

# step 载荷里的唯一时间源：f5/f1 = {f1: epoch 秒, f2: 纳秒}。
EPOCH = 1788678250
NANOS = 928526700

# trajectory_meta 只有标识，没有时间列。
TRAJECTORY = "00000000-0000-4000-8000-000000000001"
CASCADE = "cascade-synthetic"

# 声明的长度超过缓冲区：wire format 不可信，只能按引用保留，不许解码。
TRUNCATED_PAYLOAD = bytes([0x08, 0x0E, 0x20, 0x03, 0x2A, 0x98, 0x01, 0x0A, 0x22])

LIVE_SCHEMA = """
CREATE TABLE trajectory_meta (
    trajectory_id text, cascade_id text, trajectory_type integer, source integer,
    PRIMARY KEY (trajectory_id)
);
CREATE TABLE steps (
    idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0,
    has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob,
    error_details blob, permissions blob, task_details blob, render_info blob,
    step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx)
);
CREATE TABLE parent_references (idx integer, data blob, PRIMARY KEY (idx));
"""

# 漂移 schema：trajectories 没有 created_at 列。
LEGACY_SCHEMA_WITHOUT_CREATED_AT = """
CREATE TABLE trajectories (id text, name text, PRIMARY KEY (id));
CREATE TABLE steps (
    id integer, trajectory_id text, seq integer, kind text, content text,
    metadata text, created_at text
);
CREATE TABLE subtrajectories (
    id integer, step_id integer, parent_trajectory_id text, content text,
    created_at text
);
"""

# legacy 层级库的抓取白名单（trajectories/steps/subtrajectories）。
LEGACY_ALLOWED_TABLES: tuple[str, ...] = (
    "trajectories", "steps", "subtrajectories",
)
LEGACY_ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "trajectories": ("id", "name", "created_at"),
    "steps": ("id", "trajectory_id", "seq", "kind", "content", "created_at"),
    "subtrajectories": (
        "id", "step_id", "parent_trajectory_id", "content", "created_at",
    ),
}

_LEGACY_HIERARCHY_SCHEMA = """
CREATE TABLE trajectories (
    id TEXT PRIMARY KEY, name TEXT, created_at TEXT
);
CREATE TABLE steps (
    id TEXT PRIMARY KEY, trajectory_id TEXT, seq INTEGER,
    kind TEXT, content TEXT, created_at TEXT
);
CREATE TABLE subtrajectories (
    id TEXT PRIMARY KEY, step_id TEXT, parent_trajectory_id TEXT,
    content TEXT, created_at TEXT
);
CREATE TABLE session_tokens (
    id TEXT PRIMARY KEY, session_secret TEXT
);
"""


# ------------------------------------------------------------------- encoders

def vi(number: int, value: int) -> bytes:
    """一个 varint（wire type 0）字段。"""
    return encode_field(number, 0, value)


def ld(number: int, payload: bytes) -> bytes:
    """一个 length-delimited（wire type 2）字段。"""
    return encode_field(number, 2, payload)


def st(number: int, text: str) -> bytes:
    """一个 UTF-8 文本（wire type 2）字段。"""
    return encode_field(number, 2, text.encode("utf-8"))


def meta(epoch: int = EPOCH, nanos: int = NANOS) -> bytes:
    """Field 5 的 step 元数据：f5/f1 = {f1: epoch 秒, f2: 纳秒}。"""
    return ld(1, vi(1, epoch) + vi(2, nanos)) + vi(3, 2) + st(12, "step-uuid")


def step(step_type: int, *bodies: bytes, epoch: int = EPOCH, nanos: int = NANOS) -> bytes:
    """一个最小 ``Step`` 消息：f1 类型 + f4 状态 + f5 元数据 + 正文。"""
    return vi(1, step_type) + vi(4, 3) + ld(5, meta(epoch, nanos)) + b"".join(bodies)


def user_step(text: str, *, epoch: int = EPOCH) -> bytes:
    return step(14, ld(19, st(2, text)), epoch=epoch)


def user_step_with_attachment(text: str, uri: str) -> bytes:
    return step(14, ld(19, ld(11, st(1, uri) + ld(2, st(1, text)))))


def user_step_without_body() -> bytes:
    """只有元数据、没有任何正文的 user step（不许被静默丢弃）。"""
    return step(14)


def assistant_reply_step(text: str, *, epoch: int = EPOCH) -> bytes:
    """只有 f20/f1 助手句的最小 assistant step。"""
    return step(15, ld(20, st(1, text)), epoch=epoch)


def assistant_step(
    reply: str, thinking: str, call_id: str, tool: str, args: str,
    *, epoch: int = EPOCH,
) -> bytes:
    f20 = (
        st(1, reply)
        + st(3, thinking)
        + ld(7, st(1, call_id) + st(2, tool) + st(3, args))
        + st(8, reply)  # f1 的镜像：只允许产出一条助手事件
    )
    return step(15, ld(20, f20), epoch=epoch)


def tool_execution_step(
    call_id: str, tool: str, args: str, result: str | None = None, *,
    notes: dict | None = None, raw_result: bytes | None = None,
) -> bytes:
    """一个 step_type=132 载荷：f140/f1 注解，f140/f2/f1 结果文本。"""
    f5 = ld(4, st(1, call_id) + st(2, tool) + st(3, args))
    body = b"".join(ld(1, st(1, key) + st(2, value)) for key, value in (notes or {}).items())
    if raw_result is not None:
        body += ld(2, ld(1, raw_result))
    elif result is not None:
        body += ld(2, st(1, result))
    return vi(1, 132) + vi(4, 3) + ld(5, f5) + ld(140, body)


def error_step() -> bytes:
    f24 = ld(
        3,
        st(1, "Agent execution terminated due to error.")
        + st(2, "FAILED_PRECONDITION (code 400): region blocked")
        + vi(7, 400),
    )
    return step(17, ld(24, f24))


def compaction_step(summary: str) -> bytes:
    return step(23, ld(30, st(5, f"<summary>{summary}</summary>")))


def subagent_step(title: str, body: str) -> bytes:
    return step(101, ld(114, ld(2, st(1, title) + st(2, body)) + st(3, "agent_message")))


# -------------------------------------------------------------------- harness

def iso_utc(epoch: int) -> str:
    """epoch 秒 -> ``YYYY-MM-DDTHH:MM:SSZ``。"""
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


EXPECTED_ISO = iso_utc(EPOCH)


def live_db(
    root: Path,
    rows: list[tuple[int, bytes]],
    *,
    status: int = 3,
    step_format: int = 0,
    trajectory_id: str = TRAJECTORY,
    cascade_id: str = CASCADE,
) -> Path:
    """写一份 live 形状的 sqlite，返回 db 路径（文件名 = ``LIVE_HASH[:32]``）。

    ``rows`` 是 ``(step_type, step_payload)`` 列表，按给定顺序写入 ``idx``。
    """
    db = Path(root) / LIVE_HASH[:32]
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(LIVE_SCHEMA)
        con.execute(
            "INSERT INTO trajectory_meta VALUES (?,?,?,?)",
            (trajectory_id, cascade_id, 4, 1),
        )
        for idx, (step_type, payload) in enumerate(rows):
            con.execute(
                "INSERT INTO steps (idx, step_type, status, step_payload, step_format) "
                "VALUES (?,?,?,?,?)",
                (idx, step_type, status, payload, step_format),
            )
        con.commit()
    finally:
        con.close()
    return db


def live_artifact(db: Path) -> SourceArtifact:
    """指向 :func:`live_db` 写出的 blob（``content_hash[:32]`` == 文件名）。"""
    return SourceArtifact(
        artifact_id="art-antigravity-live",
        family="antigravity",
        source_kind="sqlite",
        content_hash=LIVE_HASH,
        capture_method="fixture",
        relative_path=db.name,
        byte_size=db.stat().st_size,
    )


def legacy_db_without_created_at(root: Path) -> Path:
    """写一份缺 ``created_at`` 列的 legacy 库，返回 db 路径。

    文件名为 ``legacy.db``，所以 :func:`legacy_artifact` 用 ``db.name`` 当
    ``content_hash`` 即可让 ``content_hash[:32]`` 解析回同一文件。
    """
    db = Path(root) / "legacy.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(LEGACY_SCHEMA_WITHOUT_CREATED_AT)
        con.execute("INSERT INTO trajectories VALUES ('t1','session one')")
        con.execute(
            "INSERT INTO steps VALUES "
            "(1,'t1',0,'user','合成 legacy 用户句','meta','2026-09-16T03:37:12Z')"
        )
        con.commit()
    finally:
        con.close()
    return db


def legacy_artifact(db: Path) -> SourceArtifact:
    return SourceArtifact(
        artifact_id=db.name,
        family="antigravity",
        source_kind="sqlite",
        content_hash=db.name,
        capture_method="sqlite",
        relative_path=db.name,
        byte_size=db.stat().st_size,
    )


def legacy_hierarchy_db(
    path: Path, *, long_non_message: str | None = None,
    long_subtrajectory: str | None = None,
) -> None:
    """写一份 legacy 层级库（trajectories/steps/subtrajectories + canary 凭据表）。

    ``long_non_message`` 额外插入一个 ``kind=tool`` 步骤，``long_subtrajectory``
    替换子轨迹正文：两者都用来验证非消息正文不被上限截断。
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_LEGACY_HIERARCHY_SCHEMA)
        con.execute(
            "INSERT INTO trajectories VALUES (?, ?, ?)",
            ("t_1", "antigravity run", "2026-07-01T10:00:00Z"),
        )
        con.execute(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
            ("st1", "t_1", 1, "user", "antigravity prompt", "2026-07-01T10:00:01Z"),
        )
        con.execute(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
            ("st2", "t_1", 2, "assistant", "antigravity answer", "2026-07-01T10:00:02Z"),
        )
        if long_non_message is not None:
            con.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                ("st3", "t_1", 3, "tool", long_non_message, "2026-07-01T10:00:04Z"),
            )
        con.execute(
            "INSERT INTO subtrajectories VALUES (?, ?, ?, ?, ?)",
            ("sub1", "st2", "t_1", long_subtrajectory or "sub task",
             "2026-07-01T10:00:03Z"),
        )
        con.execute("INSERT INTO session_tokens VALUES (?, ?)", ("tok_1", CANARY))
        con.commit()
    finally:
        con.close()
