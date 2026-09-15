"""Phase 13.5 Wave 5.2：conversation source fallback/rollback。

提供安全切换 conversation repository 默认 source 的能力，并恢复 canonical DB
backup。不修改 AgentView 源库。

支持：
  - ``--to legacy``：把下游默认 source 切回 legacy（agent_data.sqlite）
  - ``--to canonical``：切到 canonical conversation store
  - ``--to-backup <name>``：恢复指定 canonical DB backup
  - ``--list-backups``：列出可用 canonical backup

默认 dry-run，打印将切换的 source/build 和下游影响；``--write`` 执行切换。

source 指针存储在 ``integration/db/conversation_source.txt``（单行 legacy|canonical）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_DB,
    AGENT_CONVERSATIONS_DB,
    DB_DIR,
)
from personal_knowledge.core.conversation_repository import ConversationRepository  # noqa: E402

# source 指针文件
SOURCE_POINTER = DB_DIR / "conversation_source.txt"
# rollback 操作日志
ROLLBACK_LOG = DB_DIR / "conversation_source_rollback_log.jsonl"


@dataclass
class RollbackAction:
    """一次 rollback 操作的描述（dry-run 和实际执行都产出）。"""
    action: str               # switch_source | restore_backup | list_backups
    target_source: str | None # legacy | canonical | None
    target_backup: str | None
    current_source: str
    current_pointer: str
    canonical_db_exists: bool
    backups_available: list[str]
    will_modify: bool         # dry-run=False
    smoke_checks: dict        # source-ref / secret / session-count
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_current_source() -> str:
    """读 source 指针。默认 legacy（未切换前）。"""
    if not SOURCE_POINTER.exists():
        return "legacy"
    return SOURCE_POINTER.read_text(encoding="utf-8").strip() or "legacy"


def write_source_pointer(source: str) -> None:
    """写 source 指针（原子）。"""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SOURCE_POINTER.with_suffix(".tmp")
    tmp.write_text(source, encoding="utf-8")
    os.replace(tmp, SOURCE_POINTER)


def list_canonical_backups() -> list[str]:
    """列出可用的 canonical DB backup 文件名。"""
    backups_dir = AGENT_CONVERSATIONS_DB.parent
    backups = []
    if AGENT_CONVERSATIONS_DB.exists():
        backups.append("current")
    for f in sorted(backups_dir.glob(f"{AGENT_CONVERSATIONS_DB.stem}.backup*.sqlite")):
        backups.append(f.name)
    return backups


def smoke_check(source: str, legacy_db: Path = AGENT_DB,
                canonical_db: Path = AGENT_CONVERSATIONS_DB) -> dict:
    """对指定 source 跑 smoke check：session count + secret eligibility。"""
    checks: dict = {"source": source, "ok": False}
    try:
        repo = ConversationRepository(
            source=source, legacy_db=legacy_db, canonical_db=canonical_db
        )
        checks["session_count"] = repo.session_count()
        if source == "canonical" and canonical_db.exists():
            import sqlite3
            con = sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True)
            secret_searchable = con.execute(
                "SELECT COUNT(*) FROM canonical_messages m "
                "JOIN canonical_sessions s ON m.canonical_session_id=s.canonical_session_id "
                "WHERE s.evidence_eligible=0 AND m.content IS NOT NULL AND m.content != ''"
            ).fetchone()[0]
            con.close()
            checks["secret_searchable"] = secret_searchable
            checks["ok"] = secret_searchable == 0 and checks["session_count"] > 0
        else:
            checks["ok"] = checks["session_count"] > 0
    except Exception as exc:
        checks["error"] = str(exc)
    return checks


def _log_action(action: RollbackAction) -> None:
    """追加 rollback 操作到日志（JSONL）。"""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    action.timestamp = _utc_now()
    with ROLLBACK_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(action.to_dict(), ensure_ascii=False) + "\n")


def switch_source(
    target: str, write: bool = False,
    legacy_db: Path = AGENT_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
) -> RollbackAction:
    """切换默认 source。"""
    current = read_current_source()
    backups = list_canonical_backups()

    # 切到 canonical 需要 canonical DB 存在
    canon_exists = canonical_db.exists()
    if target == "canonical" and not canon_exists:
        smoke = {"error": "canonical DB 不存在，无法切换"}
    else:
        smoke = smoke_check(target, legacy_db, canonical_db)

    action = RollbackAction(
        action="switch_source",
        target_source=target,
        target_backup=None,
        current_source=current,
        current_pointer=str(SOURCE_POINTER),
        canonical_db_exists=canon_exists,
        backups_available=backups,
        will_modify=write,
        smoke_checks=smoke,
    )

    if write and smoke.get("ok"):
        write_source_pointer(target)
        _log_action(action)
    elif write and not smoke.get("ok"):
        action.smoke_checks["blocked"] = "smoke check failed, not switching"
    return action


def restore_backup(
    backup_name: str, write: bool = False,
    legacy_db: Path = AGENT_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
) -> RollbackAction:
    """恢复指定 canonical DB backup。"""
    current = read_current_source()
    backups = list_canonical_backups()

    if backup_name == "current":
        target_path = canonical_db
    else:
        target_path = canonical_db.parent / backup_name

    action = RollbackAction(
        action="restore_backup",
        target_source=None,
        target_backup=backup_name,
        current_source=current,
        current_pointer=str(SOURCE_POINTER),
        canonical_db_exists=canonical_db.exists(),
        backups_available=backups,
        will_modify=write,
        smoke_checks={},
    )

    if not target_path.exists():
        action.smoke_checks = {"error": f"backup 不存在: {backup_name}"}
        return action

    if write:
        # 备份当前 canonical DB
        if canonical_db.exists() and backup_name != "current":
            current_backup = canonical_db.parent / (
                f"{canonical_db.stem}.backup.{datetime.now().strftime('%Y%m%d%H%M%S')}.sqlite"
            )
            shutil.copy2(canonical_db, current_backup)
        # 恢复目标 backup
        if backup_name != "current":
            shutil.copy2(target_path, canonical_db)
        action.smoke_checks = smoke_check("canonical", legacy_db, canonical_db)
        _log_action(action)

    return action


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Phase 13.5 Wave 5.2: conversation source rollback"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--to", choices=["legacy", "canonical"], help="切换默认 source")
    g.add_argument("--to-backup", metavar="NAME", help="恢复指定 canonical backup")
    g.add_argument("--list-backups", action="store_true", help="列出可用 backup")
    p.add_argument("--write", action="store_true", help="实际执行（默认 dry-run）")
    p.add_argument("--legacy-db", type=Path, default=AGENT_DB)
    p.add_argument("--canonical-db", type=Path, default=AGENT_CONVERSATIONS_DB)
    args = p.parse_args(argv)

    if args.list_backups:
        backups = list_canonical_backups()
        print("可用 canonical backups:")
        for b in backups:
            print(f"  {b}")
        return 0

    if args.to:
        action = switch_source(
            args.to, write=args.write,
            legacy_db=args.legacy_db, canonical_db=args.canonical_db,
        )
    elif args.to_backup:
        action = restore_backup(
            args.to_backup, write=args.write,
            legacy_db=args.legacy_db, canonical_db=args.canonical_db,
        )
    else:
        # 无参数：显示当前状态
        current = read_current_source()
        backups = list_canonical_backups()
        print(f"当前 source: {current}")
        print(f"canonical DB: {args.canonical_db} (exists={args.canonical_db.exists()})")
        print(f"可用 backups: {backups}")
        print("\n用法: --to legacy|canonical | --to-backup <name> | --list-backups")
        return 0

    # 打印 action
    print("=" * 60)
    print(f"Rollback Action: {action.action}")
    print("=" * 60)
    print(f"current source: {action.current_source}")
    if action.target_source:
        print(f"target source:  {action.target_source}")
    if action.target_backup:
        print(f"target backup:  {action.target_backup}")
    print(f"will modify:    {action.will_modify}")
    print(f"canonical DB:   exists={action.canonical_db_exists}")
    print(f"backups:        {action.backups_available}")
    print(f"smoke checks:   {json.dumps(action.smoke_checks, ensure_ascii=False)}")
    if action.will_modify:
        print("[done] 已执行")
    else:
        print("[dry-run] 未修改，加 --write 执行")
    return 0 if action.smoke_checks.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
