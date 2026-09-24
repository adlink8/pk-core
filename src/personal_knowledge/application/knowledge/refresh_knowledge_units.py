"""Phase 14 Plan 06 Task 1: affected-subject incremental refresh + zero-residue reconcile。

用 Phase 13.5 source snapshot/checksum 或 delta watermark 定位受影响 evidence/subjects，
只对这些 subjects 运行 extraction/canonical rebuild。deleted/excluded/deprecated 传播
到 draft/canonical lifecycle 并从 candidate/active surface 移除。

用法::

    python refresh_knowledge_units.py --inspect
    python refresh_knowledge_units.py --source-checksum <hash> --dry-run

Refactoring note (2026-08-08): this module is now the orchestration + CLI layer.
Delta construction lives in ``delta_build.py`` and the journal/watermark ledger
lives in ``journal.py``. All previously public symbols are re-exported here so
external importers are unaffected.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import (  # noqa: E402
    UNIFIED_DB,
    AGENT_CONVERSATIONS_DB,
)
from personal_knowledge.application.knowledge.eligibility import (  # noqa: E402
    compute_eligible_messages,
)
from personal_knowledge.application.knowledge.journal import (  # noqa: E402, re-export
    JOURNAL_SCHEMA,
    acknowledge_dead_refs,
    advance_watermark,
    check_watermark_advance_preconditions,
    commit_incremental_journal,
    ensure_journal_schema,
    get_committed_watermark,
    prepare_incremental_journal,
    rollback_incremental_journal,
)
from personal_knowledge.application.knowledge.delta_build import (  # noqa: E402, re-export
    PROVIDER_AUTH_MODE,
    PROVIDER_ENDPOINT_PATTERNS,
    PROVIDER_MODEL_ALLOWLIST,
    ProviderValidationResult,
    _CANDIDATE_GET_PAGE_SIZE,
    _compute_content_hash,
    _current_eligible_ref_hashes,
    _diff_ref_hashes,
    _empty_delta_result,
    _get_all_collection_ids,
    _load_baseline_inventory_hashes,
    _load_canonical_refs,
    _materialize_delta_run,
    _resolve_active_knowledge_collection,
    build_incremental_candidate,
    compute_affected_subjects,
    compute_cache_key,
    compute_source_checksum,
    execute_run,
    prepare_delta,
    prepare_production_delta,
    validate_provider_model,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RefreshStats:
    """增量刷新统计。"""
    source_changed: bool = False
    affected_evidence_count: int = 0
    affected_subjects: list[str] = field(default_factory=list)
    new_extractions: int = 0
    updated_canonicals: int = 0
    deprecated_count: int = 0
    deleted_count: int = 0
    no_op: bool = True
    pipeline_commands: list[dict] = field(default_factory=list)


def find_affected_evidence(
    db_path: Path = UNIFIED_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
    last_source_checksum: str = "",
) -> dict:
    """定位受影响的 evidence refs 和 subjects。

    比较 canonical store 当前 evidence 与 inventory 的差异。
    """
    if not canonical_db.exists():
        return {"error": "canonical DB not found", "affected_refs": [], "affected_subjects": []}

    current_checksum = compute_source_checksum(canonical_db)

    # 如果 source checksum 相同，no-op
    if last_source_checksum and current_checksum == last_source_checksum:
        return {
            "source_changed": False,
            "current_checksum": current_checksum,
            "affected_refs": [],
            "affected_subjects": [],
            "no_op": True,
        }

    # 比较当前 canonical messages 与 inventory items
    con_unified = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)

    # inventory 中已有的 evidence refs
    inv_refs = {
        r[0] for r in con_unified.execute(
            "SELECT evidence_ref FROM knowledge_inventory_items"
        ).fetchall()
    }

    con_unified.close()

    # 当前 canonical store 的 eligible evidence refs（D-05：与 prepare/inventory
    # 共用同一 eligible 函数；role 过滤不下推到 inspect，由 delta 消费方按轨过滤）
    current_refs = {m.evidence_ref for m in compute_eligible_messages(canonical_db)[0]}

    # 新增的 refs（在 canonical 但不在 inventory）
    new_refs = current_refs - inv_refs
    # 消失的 refs（在 inventory 但不在 canonical）
    deleted_refs = inv_refs - current_refs

    ordered_new_refs = sorted(new_refs)
    ordered_deleted_refs = sorted(deleted_refs)

    # 查受影响的 subjects（从 knowledge_units）。分批查询全部 refs，避免
    # SQLite 参数上限，同时不能让展示 preview 的限制影响执行范围。
    affected_subjects = set()
    if new_refs or deleted_refs:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        # 删除的 refs 对应的 units 的 subjects
        for start in range(0, len(ordered_deleted_refs), 500):
            batch = ordered_deleted_refs[start:start + 500]
            placeholders = ",".join("?" * len(batch))
            for r in con.execute(
                f"SELECT DISTINCT subject FROM knowledge_units "
                f"WHERE source_message_ref IN ({placeholders})",
                tuple(batch),
            ):
                affected_subjects.add(r[0])
        con.close()

    return {
        "source_changed": True,
        "current_checksum": current_checksum,
        "new_refs_count": len(new_refs),
        "deleted_refs_count": len(deleted_refs),
        # Full lists are the execution input. Preview fields are display-only.
        "new_refs": ordered_new_refs,
        "deleted_refs": ordered_deleted_refs,
        "new_refs_preview": ordered_new_refs[:100],
        "deleted_refs_preview": ordered_deleted_refs[:100],
        "preview_limit": 100,
        "preview_truncated": (
            len(ordered_new_refs) > 100 or len(ordered_deleted_refs) > 100
        ),
        "affected_subjects": sorted(affected_subjects),
        "no_op": len(new_refs) == 0 and len(deleted_refs) == 0,
    }


def refresh(
    db_path: Path = UNIFIED_DB,
    canonical_db: Path = AGENT_CONVERSATIONS_DB,
    last_source_checksum: str = "",
    dry_run: bool = True,
) -> tuple[RefreshStats, dict]:
    """执行增量刷新。返回 (stats, detail)。"""
    detail = find_affected_evidence(db_path, canonical_db, last_source_checksum)
    stats = RefreshStats()

    if detail.get("no_op"):
        stats.no_op = True
        return stats, detail

    stats.source_changed = True
    stats.no_op = False
    stats.affected_evidence_count = detail.get("new_refs_count", 0) + detail.get("deleted_refs_count", 0)
    stats.affected_subjects = detail.get("affected_subjects", [])
    stats.new_extractions = detail.get("new_refs_count", 0)
    stats.deleted_count = detail.get("deleted_refs_count", 0)

    if not dry_run and detail.get("deleted_refs"):
        # 传播 deleted → deprecated lifecycle
        con = connect_rw(db_path)
        now = _utc_now()
        deleted_refs = detail["deleted_refs"]
        # 标记全部受影响 units；500 只是单批参数上限，不是执行上限。
        updated = 0
        for start in range(0, len(deleted_refs), 500):
            batch = deleted_refs[start:start + 500]
            placeholders = ",".join("?" * len(batch))
            updated += con.execute(
                f"UPDATE knowledge_units SET lifecycle='deprecated' "
                f"WHERE source_message_ref IN ({placeholders}) AND lifecycle='current'",
                tuple(batch),
            ).rowcount
        con.commit()
        con.close()
        stats.deprecated_count = updated

    if not dry_run and detail.get("new_refs"):
        # 新增 refs → 增量抽取 pipeline 编排
        # 不自动执行 LLM 调用（付费），而是输出 pipeline 命令供人工批准后执行
        stats.pipeline_commands = _build_incremental_pipeline_commands(
            detail["new_refs"], db_path, canonical_db
        )

    return stats, detail


def _build_incremental_pipeline_commands(
    new_refs: list[str],
    db_path: Path,
    canonical_db: Path,
) -> list[dict]:
    """为新增 refs 生成增量抽取→canonical→candidate pipeline 命令。

    不自动执行（LLM 调用需付费），而是返回命令供人工批准。
    命令参数与下游脚本真实 argparse 匹配。
    """
    import sqlite3 as _sql

    # 从 DB 读取真实 inventory_id 和最新 run_id（用于 --resume）
    con = _sql.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    inv_row = con.execute(
        "SELECT inventory_id FROM knowledge_inventory ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    latest_run = con.execute(
        "SELECT run_id FROM knowledge_build_runs WHERE status='validated' ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    con.close()

    inventory_id = inv_row[0] if inv_row else "UNKNOWN"
    run_id = latest_run[0] if latest_run else "UNKNOWN"
    refs_str = ",".join(new_refs[:20])

    commands = []
    # Step 1: 增量抽取 — --resume 使用已有 validated run_id 继续处理 pending items
    # new_refs 对应的 items 会被重置为 pending 以便重新抽取
    commands.append({
        "step": "1_incremental_extraction",
        "description": f"对 {len(new_refs)} 个新增 evidence refs 执行 LLM 抽取",
        "command": (
            f"pk-ku extract --run {run_id} "
            f"--model gemini-3.5-flash-lite --max-items {len(new_refs)}"
        ),
        "requires_approval": True,
        "run_id": run_id,
        "inventory_id": inventory_id,
        "sample_refs": refs_str,
    })
    # Step 2: canonical rebuild — 使用 --run（不是 --extraction-run-id）
    commands.append({
        "step": "2_canonical_rebuild",
        "description": "重建受影响 subjects 的 canonical units",
        "command": f"pk-ku canonical --run {run_id} --write",
        "requires_approval": True,
        "run_id": run_id,
        "depends_on": "1_incremental_extraction",
    })
    # Step 3: candidate build — 构建新 Chroma collection
    commands.append({
        "step": "3_candidate_build",
        "description": "构建增量 candidate 并 reconcile",
        "command": "pk-ku vector --write",
        "requires_approval": True,
        "depends_on": "2_canonical_rebuild",
    })
    # Step 4: eval — 使用新 candidate collection（从 build artifact 读取 collection_name）
    commands.append({
        "step": "4_ab_eval",
        "description": "对增量 candidate 执行 frozen A/B + hybrid eval",
        "command": (
            "python -m personal_knowledge.evaluation.knowledge.evaluate_knowledge_unit_rag "
            "--dataset hybrid --report integration/analysis/ai_context/knowledge_unit_incremental_eval.json"
        ),
        "requires_approval": False,
        "depends_on": "3_candidate_build",
        "note": "先从 build artifact 读 collection_name，再传 --candidate <name>",
    })
    return commands


def run(dry_run: bool, db_path: Path = UNIFIED_DB,
        canonical_db: Path = AGENT_CONVERSATIONS_DB,
        last_checksum: str = "") -> int:
    stats, detail = refresh(db_path, canonical_db, last_checksum, dry_run)

    print("=" * 60)
    print("Phase 14 Plan 06 Task 1: Incremental Refresh")
    print("=" * 60)
    print(f"source_changed:     {stats.source_changed}")
    print(f"current_checksum:   {detail.get('current_checksum', 'n/a')}")
    print(f"no_op:              {stats.no_op}")
    print(f"affected_evidence:  {stats.affected_evidence_count}")
    print(f"new_refs:           {detail.get('new_refs_count', 0)}")
    print(f"deleted_refs:       {detail.get('deleted_refs_count', 0)}")
    print(f"affected_subjects:  {len(stats.affected_subjects)}")
    if stats.affected_subjects:
        for s in stats.affected_subjects[:10]:
            print(f"  {s}")
    print(f"deprecated:         {stats.deprecated_count}")

    if stats.no_op:
        print("\n[no-op] source 未变化，无 LLM/index 写入")
    elif dry_run:
        print("\n[dry-run] 未写入")
    else:
        print(f"\n[done] deprecated {stats.deprecated_count} units")

    if stats.pipeline_commands:
        print(f"\n--- Incremental Pipeline ({len(stats.pipeline_commands)} steps) ---")
        for cmd in stats.pipeline_commands:
            approval_tag = " [需批准]" if cmd.get("requires_approval") else ""
            print(f"  {cmd['step']}: {cmd['description']}{approval_tag}")
            print(f"    $ {cmd['command']}")
        if not dry_run:
            print("\n  注意: pipeline 命令需人工批准后执行（LLM 调用付费）")

    return 0


def run_sandbox_ku08_e2e(
    work_dir: Path,
    *,
    model: str = "gemini-2.5-flash",
) -> dict:
    """Isolated non-empty delta → journal prepare → commit → watermark (no live index)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    unified = work_dir / "unified.sqlite"
    canon_before = work_dir / "canon_before.sqlite"
    canon_after = work_dir / "canon_after.sqlite"
    pointer = work_dir / "knowledge_index_active.txt"
    # Idempotent re-runs: wipe prior sandbox files so CREATE TABLE does not collide.
    for p in (unified, canon_before, canon_after, pointer):
        if p.exists():
            p.unlink()
    pointer.write_text("sandbox_old_index\n", encoding="utf-8")

    # Minimal canon fixtures
    def _mk_canon(path: Path, messages: list[tuple[str, str]]) -> None:
        if path.exists():
            path.unlink()
        con = connect_rw(path)
        con.execute(
            "CREATE TABLE canonical_sessions "
            "(canonical_session_id TEXT PRIMARY KEY, evidence_eligible INTEGER DEFAULT 1)"
        )
        con.execute(
            "CREATE TABLE canonical_messages ("
            "canonical_message_id TEXT PRIMARY KEY, canonical_session_id TEXT, "
            "role TEXT, content TEXT)"
        )
        con.execute("INSERT INTO canonical_sessions VALUES ('cs1', 1)")
        for mid, content in messages:
            con.execute(
                "INSERT INTO canonical_messages VALUES (?,?,?,?)",
                (mid, "cs1", "user", content),
            )
        con.commit()
        con.close()

    base = [
        ("cm|ku08_a", "preference shell powershell " + "x" * 40),
        ("cm|ku08_b", "project uses sqlite fts " + "y" * 40),
    ]
    after = base + [
        ("cm|ku08_new", "new evidence for incremental ku08 " + "z" * 40),
    ]
    _mk_canon(canon_before, base)
    _mk_canon(canon_after, after)

    from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL

    con = connect_rw(unified)
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()

    src_before = compute_source_checksum(canon_before)
    src_after = compute_source_checksum(canon_after)
    delta = prepare_delta(
        unified,
        canon_before,
        canon_after,
        src_before,
        src_after,
        model=model,
    )
    assert not delta.get("no_op"), delta
    journal = prepare_incremental_journal(
        unified,
        delta_inventory_id=delta["delta_inventory_id"],
        fresh_run_id=delta["fresh_run_id"],
        source_before_checksum=src_before,
        source_after_checksum=src_after,
        candidate_collection="sandbox_candidate_ku08",
    )
    committed = commit_incremental_journal(
        unified,
        journal["journal_id"],
        active_pointer_path=pointer,
        promote_collection="sandbox_candidate_ku08",
    )
    wm = get_committed_watermark(unified)
    # second prepare against advanced watermark should no-op when before==after
    noop = prepare_delta(
        unified,
        canon_after,
        canon_after,
        wm,
        compute_source_checksum(canon_after),
        model=model,
    )
    rolled = rollback_incremental_journal(
        unified, journal["journal_id"], active_pointer_path=pointer
    )
    return {
        "ok": True,
        "delta": delta,
        "journal": journal,
        "committed": committed,
        "watermark_after_commit": wm,
        "noop_after_commit": noop,
        "rollback": rolled,
        "pointer_after_commit": pointer.read_text(encoding="utf-8").strip(),
        "live_active_untouched": True,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 07: incremental knowledge pipeline")
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--write", action="store_true")
    p.add_argument("--source-checksum", default="", help="上次 source checksum")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    p.add_argument("--canonical-db", type=Path, default=AGENT_CONVERSATIONS_DB)
    # Task 5A: production delta preflight
    p.add_argument("--prepare", action="store_true", help="生成 immutable production delta artifact")
    p.add_argument(
        "--sandbox-ku08",
        action="store_true",
        help="Isolated non-empty delta→journal→watermark E2E (does not touch live active index)",
    )
    p.add_argument("--provider", default="", help="LLM provider (vertex_google/openai/google_free)")
    p.add_argument("--endpoint", default="", help="LLM endpoint URL")
    p.add_argument("--auth-mode", default="", help="auth mode (gcloud/api_key)")
    p.add_argument("--model", default="", help="model ID (required for --prepare)")
    p.add_argument("--artifact", type=Path, default=None, help="artifact output path")
    # Prepare extract-queue policy (defaults match safe daily incremental)
    p.add_argument(
        "--extract-new-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Queue only change_type=new (default true). Use --no-extract-new-only to include modified.",
    )
    p.add_argument(
        "--extract-since-watermark",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Floor session date at watermark updated_at (default off: the floor excludes "
        "late-synced historical sessions by session date, risking permanent skips). "
        "Use --extract-since-watermark to enable.",
    )
    p.add_argument(
        "--since",
        default="",
        metavar="YYYY-MM-DD",
        help="Explicit session started_at floor; overrides watermark floor when set",
    )
    p.add_argument(
        "--skip-succeeded",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Drop refs already succeeded in any prior run (default true)",
    )
    p.add_argument(
        "--roles",
        default="",
        help="Comma-separated roles to queue, e.g. user or user,assistant (default: all eligible)",
    )
    p.add_argument(
        "--baseline-inventory",
        default="",
        metavar="INVENTORY_ID",
        help="Force before-set inventory_id instead of watermark-era baseline",
    )
    p.add_argument(
        "--max-extract-items",
        type=int,
        default=None,
        metavar="N",
        help="Cap seeded extract queue after filters (newest sessions first)",
    )
    p.add_argument(
        "--track",
        default="user",
        choices=["user", "assistant"],
        help="Extraction track (default user). assistant: watermark key "
        "committed_assistant, roles default [assistant], prompt_version v1_assistant",
    )
    args = p.parse_args(argv)

    if args.sandbox_ku08:
        from personal_knowledge.core.project_paths import AI_CONTEXT_DIR

        work = Path("integration/analysis/ai_context/ku08_sandbox_work")
        report = run_sandbox_ku08_e2e(work, model=args.model or "gemini-2.5-flash")
        out = args.artifact or (AI_CONTEXT_DIR / "phase14_incremental_final_reconcile.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "generated_at": _utc_now(),
            "requirement": "KU-08",
            "mode": "sandbox_isolated",
            "live_active_untouched": True,
            "report": report,
        }
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1

    if args.prepare:
        if not args.model:
            print("[error] --prepare requires --model", file=sys.stderr)
            return 2
        if args.since and len(args.since) < 10:
            print("[error] --since must be YYYY-MM-DD", file=sys.stderr)
            return 2
        if args.max_extract_items is not None and args.max_extract_items < 0:
            print("[error] --max-extract-items must be >= 0", file=sys.stderr)
            return 2
        roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
        artifact_path = args.artifact or Path("integration/analysis/ai_context/knowledge_incremental_delta.json")
        try:
            result = prepare_production_delta(
                db_path=args.db,
                canonical_db=args.canonical_db,
                provider=args.provider,
                endpoint=args.endpoint,
                auth_mode=args.auth_mode,
                model=args.model,
                artifact_path=artifact_path,
                extract_new_only=args.extract_new_only,
                extract_since_watermark=args.extract_since_watermark,
                extract_min_started_at=args.since,
                skip_succeeded=args.skip_succeeded,
                roles=roles,
                baseline_inventory_id_override=args.baseline_inventory,
                max_extract_items=args.max_extract_items,
                track=args.track,
            )
        except ValueError as e:
            print(f"[error] {e}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.write:
        args.dry_run = False
    return run(args.dry_run, args.db, args.canonical_db, args.source_checksum)


if __name__ == "__main__":
    raise SystemExit(main())
