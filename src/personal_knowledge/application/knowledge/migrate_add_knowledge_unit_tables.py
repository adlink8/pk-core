"""Phase 14 Wave 1.1：knowledge_unit schema 迁移。

在 ``personal_system.sqlite`` 新增 6 张知识单元表：
  - knowledge_build_runs
  - knowledge_units
  - knowledge_unit_evidence
  - canonical_knowledge_units
  - canonical_unit_members
  - knowledge_index_versions

迁移幂等，默认 dry-run/inspect；不修改 memory_items。

用法::

    python migrate_add_knowledge_unit_tables.py --inspect
    python migrate_add_knowledge_unit_tables.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402
from personal_knowledge.core.sqlite import connect_rw  # noqa: E402
from personal_knowledge.application.knowledge.lifecycle_events import LIFECYCLE_SCHEMA_SQL  # noqa: E402

from personal_knowledge.application.knowledge.schema_ddl import (  # noqa: E402
    EXTRACTION_GATES_TABLE_SQL,
    INVENTORY_REGISTRY_TABLE_SQL,
    RUN_ITEMS_TABLE_SQL,
    SCHEMA_SQL,
)


def _foreign_key_target(
    con: sqlite3.Connection, table: str, column: str
) -> str:
    for row in con.execute(f'PRAGMA foreign_key_list("{table}")'):
        if row[3] == column:
            return str(row[2])
    return ""


def inspect_inventory_registry(db_path: Path = UNIFIED_DB) -> dict:
    """Inspect the full/delta inventory parent model without writing."""
    if not db_path.exists():
        return {"db_exists": False, "db_path": str(db_path)}
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "knowledge_inventory",
            "knowledge_delta_inventories",
            "knowledge_run_items",
            "knowledge_extraction_gates",
        }
        missing_tables = sorted(required - tables)
        registry_exists = "knowledge_inventory_registry" in tables
        run_target = (
            _foreign_key_target(con, "knowledge_run_items", "inventory_id")
            if "knowledge_run_items" in tables
            else ""
        )
        gate_target = (
            _foreign_key_target(con, "knowledge_extraction_gates", "inventory_id")
            if "knowledge_extraction_gates" in tables
            else ""
        )
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        by_table: dict[str, int] = {}
        for row in violations:
            by_table[str(row[0])] = by_table.get(str(row[0]), 0) + 1
        counts = {
            "full": con.execute(
                "SELECT COUNT(*) FROM knowledge_inventory"
            ).fetchone()[0]
            if "knowledge_inventory" in tables
            else 0,
            "delta": con.execute(
                "SELECT COUNT(*) FROM knowledge_delta_inventories"
            ).fetchone()[0]
            if "knowledge_delta_inventories" in tables
            else 0,
            "registry": con.execute(
                "SELECT COUNT(*) FROM knowledge_inventory_registry"
            ).fetchone()[0]
            if registry_exists
            else 0,
            "run_items": con.execute(
                "SELECT COUNT(*) FROM knowledge_run_items"
            ).fetchone()[0]
            if "knowledge_run_items" in tables
            else 0,
            "gates": con.execute(
                "SELECT COUNT(*) FROM knowledge_extraction_gates"
            ).fetchone()[0]
            if "knowledge_extraction_gates" in tables
            else 0,
        }
        target = "knowledge_inventory_registry"
        healthy = (
            not missing_tables
            and registry_exists
            and run_target == target
            and gate_target == target
            and not violations
            and counts["registry"] == counts["full"] + counts["delta"]
        )
        return {
            "db_exists": True,
            "db_path": str(db_path),
            "missing_tables": missing_tables,
            "registry_exists": registry_exists,
            "run_items_inventory_fk_target": run_target,
            "gates_inventory_fk_target": gate_target,
            "counts": counts,
            "foreign_key_violations_total": len(violations),
            "foreign_key_violations_by_table": by_table,
            "healthy": healthy,
        }
    finally:
        con.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_sqlite(source: Path, backup_path: Path) -> dict:
    if backup_path.exists():
        raise FileExistsError(f"backup target already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"backup integrity_check: {integrity}")
    finally:
        dst.close()
        src.close()
    return {
        "path": str(backup_path),
        "bytes": backup_path.stat().st_size,
        "sha256": _sha256_file(backup_path),
        "integrity_check": "ok",
    }


def _rebuild_inventory_fk_table(
    con: sqlite3.Connection,
    *,
    table: str,
    create_sql: str,
    columns: tuple[str, ...],
    indexes: tuple[str, ...],
) -> tuple[int, int]:
    legacy = f"{table}_legacy_inventory_fk"
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy,)
    ).fetchone():
        raise RuntimeError(f"stale migration table exists: {legacy}")
    before = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    con.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')
    con.execute(create_sql)
    column_sql = ", ".join(f'"{name}"' for name in columns)
    con.execute(
        f'INSERT INTO "{table}" ({column_sql}) '
        f'SELECT {column_sql} FROM "{legacy}"'
    )
    after = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    if after != before:
        raise RuntimeError(f"row count mismatch rebuilding {table}: {before} != {after}")
    con.execute(f'DROP TABLE "{legacy}"')
    for index_sql in indexes:
        con.execute(index_sql)
    return before, after


def migrate_inventory_registry(
    db_path: Path = UNIFIED_DB,
    *,
    write: bool = False,
    backup_path: Path | None = None,
) -> dict:
    """Repair polymorphic inventory FKs using a unified parent registry."""
    before = inspect_inventory_registry(db_path)
    if not before.get("db_exists"):
        return {"error": f"DB does not exist: {db_path}"}
    if before.get("missing_tables"):
        return {"error": f"required tables missing: {before['missing_tables']}"}
    if before.get("healthy"):
        return {"no_op": True, "before": before, "after": before}
    if not write:
        return {
            "dry_run": True,
            "would_backup": str(backup_path) if backup_path else None,
            "would_create_registry": not before.get("registry_exists"),
            "would_rebuild": ["knowledge_run_items", "knowledge_extraction_gates"],
            "before": before,
        }
    if backup_path is None:
        return {"error": "--backup is required for --write"}

    backup = _backup_sqlite(db_path, backup_path)
    con = sqlite3.connect(str(db_path), timeout=60)
    rebuilt: dict[str, dict[str, int]] = {}
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("BEGIN IMMEDIATE")
        con.execute(INVENTORY_REGISTRY_TABLE_SQL)
        con.execute(
            "INSERT OR IGNORE INTO knowledge_inventory_registry "
            "(inventory_id, inventory_kind, created_at) "
            "SELECT inventory_id, 'full', generated_at FROM knowledge_inventory"
        )
        con.execute(
            "INSERT OR IGNORE INTO knowledge_inventory_registry "
            "(inventory_id, inventory_kind, created_at) "
            "SELECT delta_inventory_id, 'delta', created_at "
            "FROM knowledge_delta_inventories"
        )
        con.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_knowledge_inventory_registry "
            "AFTER INSERT ON knowledge_inventory BEGIN "
            "INSERT OR IGNORE INTO knowledge_inventory_registry "
            "(inventory_id, inventory_kind, created_at) "
            "VALUES (NEW.inventory_id, 'full', NEW.generated_at); END"
        )
        con.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_knowledge_delta_inventory_registry "
            "AFTER INSERT ON knowledge_delta_inventories BEGIN "
            "INSERT OR IGNORE INTO knowledge_inventory_registry "
            "(inventory_id, inventory_kind, created_at) "
            "VALUES (NEW.delta_inventory_id, 'delta', NEW.created_at); END"
        )

        if _foreign_key_target(
            con, "knowledge_run_items", "inventory_id"
        ) != "knowledge_inventory_registry":
            old, new = _rebuild_inventory_fk_table(
                con,
                table="knowledge_run_items",
                create_sql=RUN_ITEMS_TABLE_SQL,
                columns=(
                    "id", "run_id", "inventory_id", "position", "evidence_ref",
                    "status", "attempt_count", "lease_started_at",
                    "last_error_class", "cache_key", "response_hash",
                    "unit_count", "updated_at",
                ),
                indexes=(
                    "CREATE INDEX idx_kri_run ON knowledge_run_items(run_id)",
                    "CREATE INDEX idx_kri_status ON knowledge_run_items(status)",
                ),
            )
            rebuilt["knowledge_run_items"] = {"before": old, "after": new}

        if _foreign_key_target(
            con, "knowledge_extraction_gates", "inventory_id"
        ) != "knowledge_inventory_registry":
            old, new = _rebuild_inventory_fk_table(
                con,
                table="knowledge_extraction_gates",
                create_sql=EXTRACTION_GATES_TABLE_SQL,
                columns=(
                    "gate_id", "run_id", "inventory_id", "gate_status",
                    "gate_json", "evaluated_at",
                ),
                indexes=(
                    "CREATE INDEX idx_keg_run ON knowledge_extraction_gates(run_id)",
                ),
            )
            rebuilt["knowledge_extraction_gates"] = {"before": old, "after": new}

        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            counts: dict[str, int] = {}
            for row in violations:
                counts[str(row[0])] = counts.get(str(row[0]), 0) + 1
            raise sqlite3.IntegrityError(
                f"foreign_key_check still reports {len(violations)}: {counts}"
            )
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("integrity_check failed before commit")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    after = inspect_inventory_registry(db_path)
    if not after.get("healthy"):
        raise RuntimeError(f"post-migration verification failed: {after}")
    return {
        "migrated": True,
        "backup": backup,
        "rebuilt": rebuilt,
        "before": before,
        "after": after,
    }


def inspect(db_path: Path = UNIFIED_DB) -> dict:
    """检查现有表状态。"""
    if not db_path.exists():
        return {"db_exists": False, "tables": []}
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    con.close()

    new_tables = [
        "knowledge_build_runs", "knowledge_units", "knowledge_unit_evidence",
        "canonical_knowledge_units", "canonical_unit_members", "knowledge_index_versions",
        # Plan 02
        "knowledge_inventory", "knowledge_inventory_items",
        "knowledge_inventory_registry",
        "knowledge_run_items", "knowledge_response_cache", "knowledge_extraction_gates",
        # Plan 05
        "rag_runs", "rag_retrieval_items", "rag_feedback",
        # Plan 07
        "knowledge_delta_inventories", "knowledge_delta_items", "knowledge_source_watermark",
        # Phase 23 composite serving authority
        "artifact_registry_entries", "artifact_versions", "source_watermarks",
        "serving_snapshots", "serving_snapshot_members", "serving_authority",
        "serving_snapshot_events",
        # Phase 25 immutable personal-state analysis
        "personal_state_runs", "personal_state_publications", "personal_state_assertions",
        "personal_state_evidence", "personal_state_changes",
        "personal_state_risks",
        # Phase 26 immutable decision-feedback authority
        "decision_runs", "decision_recommendations", "decision_support_refs",
        "decision_confirmations", "decision_actions", "decision_outcomes",
        "decision_effectiveness", "decision_events",
        # Phase 27 immutable proactive-intelligence authority
        "proactive_runs", "proactive_coordination_items", "proactive_candidates",
        "proactive_candidate_support", "proactive_evaluations",
        "proactive_control_events", "proactive_surface_events",
        # Phase 24 governed lifecycle
        "knowledge_lifecycle_manifests", "knowledge_lifecycle_actions",
        "knowledge_lifecycle_events", "knowledge_unit_corrections",
    ]
    return {
        "db_exists": True,
        "db_path": str(db_path),
        "existing_tables": sorted(existing & set(new_tables)),
        "missing_tables": sorted(set(new_tables) - existing),
    }


def plan_serving_bootstrap(db_path: Path = UNIFIED_DB) -> dict:
    """Build a read-only draft description for the current KU serving state."""
    before = db_path.stat().st_mtime_ns if db_path.exists() else None
    if not db_path.exists():
        return {"db_exists": False, "active": False, "missing_proofs": ["unified_db"]}
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "knowledge_index_versions" not in tables:
            return {"db_exists": True, "active": False, "missing_proofs": ["knowledge_index_versions"]}
        rows = con.execute(
            "SELECT version_id, build_id, collection_name, canonical_build_id, unit_count, checksum "
            "FROM knowledge_index_versions WHERE status='active' ORDER BY activated_at DESC"
        ).fetchall()
        missing: list[str] = []
        if len(rows) != 1:
            missing.append("exactly_one_active_knowledge_index")
        row = rows[0] if len(rows) == 1 else None
        if row and not row[5]:
            missing.append("active_collection_checksum")
        return {
            "db_exists": True,
            "active": False,
            "mode": "draft_only",
            "knowledge_index": dict(zip(
                ("version_id", "build_id", "collection_name", "canonical_build_id", "unit_count", "checksum"),
                row,
            )) if row else None,
            "missing_proofs": missing,
        }
    finally:
        con.close()
        after = db_path.stat().st_mtime_ns if db_path.exists() else None
        if before != after:
            raise RuntimeError("read-only bootstrap planning modified the database")


def migrate(db_path: Path = UNIFIED_DB, write: bool = False) -> dict:
    """执行迁移。"""
    info = inspect(db_path)
    if not info["db_exists"]:
        return {"error": f"DB 不存在: {db_path}"}
    if not info["missing_tables"] and write:
        return {"message": "所有表已存在，无需迁移", "tables": info["existing_tables"]}

    if write and {
        "knowledge_run_items",
        "knowledge_extraction_gates",
    }.issubset(set(info["existing_tables"])):
        registry_state = inspect_inventory_registry(db_path)
        if not registry_state.get("healthy"):
            return {
                "error": (
                    "existing inventory consumers require the guarded repair: "
                    "--repair-inventory-fks --write --backup <path>"
                ),
                "inventory_registry": registry_state,
            }

    if not write:
        return {"dry_run": True, "would_create": info["missing_tables"],
                "already_exist": info["existing_tables"]}

    con = connect_rw(db_path)
    try:
        con.executescript(SCHEMA_SQL)
        con.executescript(LIFECYCLE_SCHEMA_SQL)
        con.commit()
    finally:
        con.close()

    result = inspect(db_path)
    return {"migrated": True, "tables": result["existing_tables"]}


def main(argv: list[str] | None = None) -> int:
    import json

    p = argparse.ArgumentParser(description="Phase 14 Wave 1.1: knowledge_unit schema 迁移")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--inspect", action="store_true", help="检查现有表状态")
    g.add_argument("--write", action="store_true", help="执行迁移")
    p.add_argument(
        "--repair-inventory-fks",
        action="store_true",
        help="Repair full/delta inventory parent FKs (guarded, backup required for write)",
    )
    p.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="New backup path required with --repair-inventory-fks --write",
    )
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)

    if args.repair_inventory_fks:
        result = migrate_inventory_registry(
            args.db,
            write=args.write,
            backup_path=args.backup,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if "error" not in result else 1

    if args.inspect or not args.write:
        result = inspect(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = migrate(args.db, write=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
