"""Phase 14 Plan 06 Task 2: preview-first memory lifecycle migration/sync。

为 memory_items 增加 lifecycle 字段（status/version/last_seen/canonical_unit_id），
但默认只 dry-run link proposal。写入前展示影响清单并要求显式 `--write`；
只标记 deprecated，不物理删除。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LifecycleProposal:
    """单个 lifecycle 变更提案。"""
    memory_id: str
    action: str  # create / update / deprecate / conflict
    canonical_unit_id: str
    reason: str
    current_status: str
    proposed_status: str


@dataclass
class LifecyclePreview:
    """lifecycle preview 报告。"""
    preview_hash: str = ""
    source_canonical_build: str = ""
    generated_at: str = ""
    create_count: int = 0
    update_count: int = 0
    deprecate_count: int = 0
    conflict_count: int = 0
    proposals: list[dict] = field(default_factory=list)


def migrate(db_path: Path = UNIFIED_DB, write: bool = False) -> dict:
    """幂等 migration：为 memory_items 增加 lifecycle 字段。"""
    con = sqlite3.connect(str(db_path))
    result = {"migrated": False, "added_columns": []}

    # 检查现有列
    cols = {c[1] for c in con.execute("PRAGMA table_info(memory_items)")}
    needed = {
        "ku_status": "TEXT DEFAULT 'pending'",
        "ku_version": "INTEGER DEFAULT 0",
        "ku_last_seen": "TEXT",
        "canonical_unit_id": "TEXT",
    }
    if write:
        for col, defn in needed.items():
            if col not in cols:
                con.execute(f"ALTER TABLE memory_items ADD COLUMN {col} {defn}")
                result["added_columns"].append(col)
        con.commit()
        result["migrated"] = True
    else:
        result["would_add"] = [c for c in needed if c not in cols]

    con.close()
    return result


def build_preview(db_path: Path = UNIFIED_DB) -> LifecyclePreview:
    """构建 lifecycle preview（只读）。"""
    preview = LifecyclePreview(
        generated_at=_utc_now(),
        source_canonical_build="canonical-v1",
    )

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # 检查 memory_items 是否有 lifecycle 列
    cols = {c[1] for c in con.execute("PRAGMA table_info(memory_items)")}
    if "canonical_unit_id" not in cols:
        con.close()
        preview.preview_hash = hashlib.sha256(
            f"no_migration|{preview.generated_at}".encode()
        ).hexdigest()[:32]
        return preview

    # 匹配 memory_items 与 canonical_knowledge_units
    # 按 subject 匹配
    memories = con.execute(
        "SELECT memory_id, subject, description FROM memory_items LIMIT 1000"
    ).fetchall()

    canonicals = con.execute(
        "SELECT canonical_unit_id, subject FROM canonical_knowledge_units "
        "WHERE status='current'"
    ).fetchall()

    # subject → canonical_unit_id 映射
    subject_to_cu: dict[str, str] = {}
    for cu in canonicals:
        subject_to_cu.setdefault(cu["subject"].lower().strip(), cu["canonical_unit_id"])

    for mem in memories:
        mem_subject = (mem["subject"] or "").lower().strip()
        cu_id = subject_to_cu.get(mem_subject)

        if cu_id:
            # 有匹配 → link/update
            existing = con.execute(
                "SELECT canonical_unit_id FROM memory_items WHERE memory_id=?",
                (mem["memory_id"],),
            ).fetchone()
            if existing and existing["canonical_unit_id"]:
                action = "update"
                preview.update_count += 1
            else:
                action = "create"
                preview.create_count += 1
            preview.proposals.append({
                "memory_id": str(mem["memory_id"]),
                "action": action,
                "canonical_unit_id": cu_id,
                "reason": f"subject match: {mem_subject}",
            })
        else:
            # 无匹配 → 可能 deprecate
            preview.deprecate_count += 1
            preview.proposals.append({
                "memory_id": str(mem["memory_id"]),
                "action": "deprecate",
                "canonical_unit_id": "",
                "reason": "no canonical unit found for subject",
            })

    con.close()

    # preview hash
    payload = json.dumps(preview.proposals[:100], sort_keys=True, ensure_ascii=False)
    preview.preview_hash = hashlib.sha256(payload.encode()).hexdigest()[:32]

    return preview


def apply_write(db_path: Path, preview_hash: str) -> dict:
    """应用获批的 preview。deprecate/supersede 不物理删除。"""
    preview = build_preview(db_path)
    if preview.preview_hash != preview_hash:
        return {"error": "preview hash mismatch"}

    con = sqlite3.connect(str(db_path))
    applied = 0
    for prop in preview.proposals:
        if prop["action"] in ("create", "update"):
            con.execute(
                "UPDATE memory_items SET canonical_unit_id=?, ku_status='linked' WHERE memory_id=?",
                (prop["canonical_unit_id"], prop["memory_id"]),
            )
            applied += 1
        elif prop["action"] == "deprecate":
            con.execute(
                "UPDATE memory_items SET ku_status='deprecated' WHERE memory_id=?",
                (prop["memory_id"],),
            )
            applied += 1
    con.commit()
    con.close()
    return {"applied": applied, "hash": preview_hash}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 06: memory lifecycle")
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--migrate", action="store_true", help="执行 migration")
    p.add_argument("--preview", action="store_true", help="生成 preview")
    p.add_argument("--write", action="store_true", help="应用 preview")
    p.add_argument("--expected-hash", default="", help="preview hash")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)

    if args.migrate:
        result = migrate(args.db, write=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.preview or args.inspect:
        preview = build_preview(args.db)
        print(json.dumps(asdict(preview), ensure_ascii=False, indent=2))
        return 0

    if args.write:
        if not args.expected_hash:
            print("[error] --write 需要 --expected-hash", file=sys.stderr)
            return 2
        result = apply_write(args.db, args.expected_hash)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if "error" not in result else 1

    # 默认 preview
    preview = build_preview(args.db)
    print(json.dumps(asdict(preview), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
