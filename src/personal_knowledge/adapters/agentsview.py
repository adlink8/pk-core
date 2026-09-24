"""Phase 13.5 Wave 1.1：AgentView 会话源 adapter（只读）。

设计约束（来自 CONTEXT.md D-02/D-04，不可违反）：

- 源库 ``C:\\Users\\<user>\\.agentsview\\sessions.db`` 是 AgentView daemon 正在
  写入的 WAL SQLite。本 adapter 连接时强制 ``mode=ro`` + ``PRAGMA query_only=ON``，
  绝不迁移、建索引、VACUUM 或写入源库。
- 长导入前用 SQLite backup API 取得一致快照；禁止直接复制
  ``.db/.db-wal/.db-shm`` 拼装快照（跨事务不一致）。
- schema 缺表、关键列缺失或 ``integrity_check != ok`` 时 pre-flight abort，
  只返回 blocked manifest，不创建正式 normalized DB。
- 所有方法纯读取，本模块不写任何 normalized store（那是 import 脚本的职责）。

对外只暴露：
  - :class:`SourceProbe`        — 只读 schema/计数探测结果
  - :class:`SnapshotManifest`   — 快照元数据（供 inventory/normalized 复用）
  - :func:`probe_source`        — 连接 + integrity + schema gate
  - :func:`backup_snapshot`     — 用 backup API 把一致快照写到临时文件
  - :class:`AgentViewAdapter`   — 高层封装

所有公开函数的 ``source_db`` 参数默认指向 ``core.project_paths.AGENTSVIEW_DB``，
但允许传入显式路径（测试用临时 fixture）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENTSVIEW_DB,
    VAR_TMP,
)

# 受支持的最小 schema：adapter/normalized 需要这些表存在。
# 缺表即 pre-flight abort。FTS/config 内部表不在其中。
REQUIRED_TABLES: tuple[str, ...] = (
    "sessions",
    "messages",
    "tool_calls",
    "tool_result_events",
    "usage_events",
    "secret_findings",
    "excluded_sessions",
)

# 关键列：缺这些列说明源 schema 已偏离 adapter 假设，必须 abort。
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "id", "agent", "started_at", "ended_at", "message_count",
        "user_message_count", "file_hash", "parent_session_id",
        "relationship_type", "source_session_id", "deleted_at",
        "secret_leak_count",
    ),
    "messages": (
        "id", "session_id", "ordinal", "role", "content", "timestamp",
        "is_system", "is_sidechain", "model",
    ),
    "tool_calls": (
        "id", "session_id", "tool_name", "category", "call_index",
        "subagent_session_id",
    ),
    "tool_result_events": (
        "id", "session_id", "status", "subagent_session_id", "event_index",
    ),
    "usage_events": ("id", "session_id", "model", "occurred_at"),
    "secret_findings": ("id", "session_id", "rule_name"),
    "excluded_sessions": ("id", "created_at"),
}


@dataclass(frozen=True)
class SourceProbe:
    """源库只读探测结果。

    所有字段来自一次只读连接；不含任何 message content、邮箱或 secret match。
    """

    source_path: str
    integrity_check: str
    user_version: int
    journal_mode: str
    table_count: int
    required_tables_present: list[str]
    required_tables_missing: list[str]
    missing_columns: dict[str, list[str]]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """schema gate：integrity ok + 无缺表 + 无缺列。"""
        return (
            self.integrity_check == "ok"
            and not self.required_tables_missing
            and not self.missing_columns
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotManifest:
    """一次 backup 快照的元数据。

    记录源 schema/version、输入计数、代码版本和配置 hash，供 inventory 和
    normalized 生成复用。不含任何正文。
    """

    run_id: str
    source_path: str
    snapshot_path: str
    source_user_version: int
    source_journal_mode: str
    source_integrity: str
    schema_hash: str
    counts: dict[str, int]
    config_hash: str
    code_version: str

    def to_dict(self) -> dict:
        return asdict(self)


def _read_only_uri(path: Path) -> str:
    """构造只读 SQLite URI。"""
    return f"file:{path.as_posix()}?mode=ro"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    """以 mode=ro + query_only 打开源库。失败抛 sqlite3.Error。"""
    con = sqlite3.connect(_read_only_uri(path), uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def probe_source(source_db: Path = AGENTSVIEW_DB) -> SourceProbe:
    """只读探测源库：integrity、schema gate、表计数。

    不创建任何文件，不产生任何写入。返回 :class:`SourceProbe`；
    schema gate 不过时 ``probe.ok`` 为 False。
    """
    con = _connect_read_only(source_db)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        user_version = con.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]

        all_tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        table_count = len(all_tables)

        present = [t for t in REQUIRED_TABLES if t in all_tables]
        missing = [t for t in REQUIRED_TABLES if t not in all_tables]

        missing_cols: dict[str, list[str]] = {}
        for tbl, cols in REQUIRED_COLUMNS.items():
            if tbl in all_tables:
                actual = {c[1] for c in con.execute(f"PRAGMA table_info({tbl})")}
                absent = [c for c in cols if c not in actual]
                if absent:
                    missing_cols[tbl] = absent

        counts: dict[str, int] = {}
        for t in present:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                counts[t] = -1
    finally:
        con.close()

    return SourceProbe(
        source_path=str(source_db),
        integrity_check=integrity,
        user_version=user_version,
        journal_mode=journal_mode,
        table_count=table_count,
        required_tables_present=present,
        required_tables_missing=missing,
        missing_columns=missing_cols,
        counts=counts,
    )


def _schema_hash(con: sqlite3.Connection) -> str:
    """对源库 schema DDL 做稳定 hash，用于检测 schema 漂移。"""
    ddl_rows = con.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE '%\\_fts%' ESCAPE '\\' "
        "ORDER BY name"
    ).fetchall()
    payload = "\n;;;".join(sql or "" for _name, sql in ddl_rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _config_hash(extra: dict | None = None) -> str:
    """对 adapter 配置（required tables/columns）做 hash。"""
    payload = json.dumps(
        {
            "required_tables": list(REQUIRED_TABLES),
            "required_columns": {
                k: list(v) for k, v in REQUIRED_COLUMNS.items()
            },
            "extra": extra or {},
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _run_id(source_path: str, schema_hash: str) -> str:
    """稳定的 run_id：源路径 + schema hash。"""
    return hashlib.sha256(
        f"{source_path}|{schema_hash}".encode("utf-8")
    ).hexdigest()[:16]


def backup_snapshot(
    source_db: Path = AGENTSVIEW_DB,
    dest_dir: Path | None = None,
    probe: SourceProbe | None = None,
) -> tuple[SnapshotManifest, Path]:
    """用 SQLite backup API 把源库一致快照写到临时文件。

    返回 ``(manifest, snapshot_path)``。快照文件由调用方负责清理
    （import 脚本在成功或失败后都会删除自己创建的临时文件）。

    若 ``probe`` 为 None，内部先 :func:`probe_source`；schema gate 不过则
    抛 :class:`SchemaGateError`，不创建快照。
    """
    if probe is None:
        probe = probe_source(source_db)
    if not probe.ok:
        raise SchemaGateError(probe)

    if dest_dir is None:
        snapshot_temp = VAR_TMP / "agentsview-snapshots"
        snapshot_temp.mkdir(parents=True, exist_ok=True)
        dest_dir = Path(tempfile.mkdtemp(
            prefix="agentsview_snap_", dir=snapshot_temp,
        ))
    dest_dir.mkdir(parents=True, exist_ok=True)

    schema_h = _config_hash()  # 配置 hash（静态）
    run_id = _run_id(str(source_db), probe.counts.get("sessions", 0).__str__() + schema_h)
    snapshot_path = dest_dir / f"agentsview_snapshot_{run_id}.sqlite"

    src = _connect_read_only(source_db)
    dst = sqlite3.connect(str(snapshot_path))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    manifest = SnapshotManifest(
        run_id=run_id,
        source_path=str(source_db),
        snapshot_path=str(snapshot_path),
        source_user_version=probe.user_version,
        source_journal_mode=probe.journal_mode,
        source_integrity=probe.integrity_check,
        schema_hash=_schema_hash_from_snapshot(snapshot_path),
        counts=dict(probe.counts),
        config_hash=schema_h,
        code_version="13.5.w1",
    )
    return manifest, snapshot_path


def _schema_hash_from_snapshot(snapshot_path: Path) -> str:
    """从已写入的快照文件读 schema hash（与 _schema_hash 同算法）。"""
    con = sqlite3.connect(str(snapshot_path))
    try:
        return _schema_hash(con)
    finally:
        con.close()


class SchemaGateError(RuntimeError):
    """schema gate 未通过（缺表/缺列/integrity 失败）。"""

    def __init__(self, probe: SourceProbe) -> None:
        self.probe = probe
        parts = []
        if probe.integrity_check != "ok":
            parts.append(f"integrity_check={probe.integrity_check}")
        if probe.required_tables_missing:
            parts.append(f"missing_tables={probe.required_tables_missing}")
        if probe.missing_columns:
            parts.append(f"missing_columns={probe.missing_columns}")
        super().__init__("AgentView schema gate failed: " + "; ".join(parts))


class AgentViewAdapter:
    """高层封装：probe + backup + manifest，供 import 脚本调用。

    典型用法::

        adapter = AgentViewAdapter()
        if not adapter.probe().ok:
            # 写 blocked report，不创建 normalized DB
            ...
        manifest, snap = adapter.snapshot()   # backup API
        try:
            ...  # 所有转换只读 snap
        finally:
            snap.unlink(missing_ok=True)       # 清理临时文件
    """

    def __init__(self, source_db: Path = AGENTSVIEW_DB) -> None:
        self.source_db = source_db

    def probe(self) -> SourceProbe:
        return probe_source(self.source_db)

    def snapshot(
        self, dest_dir: Path | None = None, probe: SourceProbe | None = None
    ) -> tuple[SnapshotManifest, Path]:
        return backup_snapshot(self.source_db, dest_dir=dest_dir, probe=probe)


if __name__ == "__main__":
    # 手动冒烟：python agentsview.py probe | snapshot
    import argparse

    p = argparse.ArgumentParser(description="AgentView 只读 source adapter 冒烟")
    p.add_argument("action", choices=["probe", "snapshot"])
    p.add_argument("--source", type=Path, default=AGENTSVIEW_DB)
    args = p.parse_args()

    adapter = AgentViewAdapter(args.source)
    if args.action == "probe":
        probe = adapter.probe()
        print(json.dumps(probe.to_dict(), ensure_ascii=False, indent=2))
        print("GATE:", "OK" if probe.ok else "BLOCKED")
    else:
        manifest, snap = adapter.snapshot()
        print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
        print("snapshot:", snap)
