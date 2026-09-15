"""Phase 15 Wave 3：从 source_message_ref 回填 knowledge_unit_evidence。

仅 INSERT OR IGNORE 缺失证据行；不修改 knowledge_units 的 question/answer，
不删除任何已有 evidence。默认 --dry-run 只报告。

用法::

    python -m personal_knowledge.application.knowledge.backfill_knowledge_unit_evidence
    python -m personal_knowledge.application.knowledge.backfill_knowledge_unit_evidence --dry-run
    python -m personal_knowledge.application.knowledge.backfill_knowledge_unit_evidence --write
    python -m personal_knowledge.application.knowledge.backfill_knowledge_unit_evidence --write --limit 1000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_CONVERSATIONS_DB,
    AI_CONTEXT_DIR,
    UNIFIED_DB,
)

DEFAULT_REPORT_PATH = AI_CONTEXT_DIR / "phase15_evidence_backfill_dry_run.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _connect_rw(path: Path) -> sqlite3.Connection:
    return connect_rw(path)


def coverage_stats(con: sqlite3.Connection) -> dict[str, Any]:
    """Compute draft evidence coverage from knowledge_units / knowledge_unit_evidence."""
    total = con.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0]
    with_evidence = con.execute(
        "SELECT COUNT(DISTINCT unit_id) FROM knowledge_unit_evidence"
    ).fetchone()[0]
    no_evidence = con.execute(
        """
        SELECT COUNT(*) FROM knowledge_units ku
        WHERE NOT EXISTS (
            SELECT 1 FROM knowledge_unit_evidence e WHERE e.unit_id = ku.unit_id
        )
        """
    ).fetchone()[0]
    no_evidence_with_ref = con.execute(
        """
        SELECT COUNT(*) FROM knowledge_units ku
        WHERE NOT EXISTS (
            SELECT 1 FROM knowledge_unit_evidence e WHERE e.unit_id = ku.unit_id
        )
        AND source_message_ref IS NOT NULL
        AND TRIM(source_message_ref) != ''
        """
    ).fetchone()[0]
    evidence_rows = con.execute(
        "SELECT COUNT(*) FROM knowledge_unit_evidence"
    ).fetchone()[0]
    return {
        "total_units": total,
        "units_with_evidence": with_evidence,
        "units_without_evidence": no_evidence,
        "units_without_evidence_with_ref": no_evidence_with_ref,
        "units_without_evidence_empty_ref": no_evidence - no_evidence_with_ref,
        "evidence_rows": evidence_rows,
        "coverage": round(with_evidence / total, 6) if total else 0.0,
    }


def find_candidates(
    ku_con: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> list[tuple[str, str]]:
    """Units with no evidence row and a non-empty source_message_ref.

    Returns list of (unit_id, source_message_ref).
    """
    sql = """
        SELECT ku.unit_id, ku.source_message_ref
        FROM knowledge_units ku
        WHERE NOT EXISTS (
            SELECT 1 FROM knowledge_unit_evidence e WHERE e.unit_id = ku.unit_id
        )
        AND ku.source_message_ref IS NOT NULL
        AND TRIM(ku.source_message_ref) != ''
        ORDER BY ku.unit_id
    """
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [(r[0], r[1]) for r in ku_con.execute(sql).fetchall()]


def validate_refs(
    candidates: list[tuple[str, str]],
    canon_db: Path | None,
) -> dict[str, Any]:
    """Split candidates into validated vs missing-in-canonical.

    If canon_db is None or missing, treat all as unvalidated (still insertable
    when validate_refs is disabled by caller).
    """
    if not candidates:
        return {
            "validated": [],
            "invalid": [],
            "skipped_no_canon": [],
            "canon_available": False,
            "distinct_refs": 0,
            "valid_refs": 0,
            "invalid_refs": 0,
        }

    distinct = {ref for _, ref in candidates}
    if canon_db is None or not Path(canon_db).exists():
        return {
            "validated": list(candidates),
            "invalid": [],
            "skipped_no_canon": list(candidates),
            "canon_available": False,
            "distinct_refs": len(distinct),
            "valid_refs": 0,
            "invalid_refs": 0,
            "note": "canonical DB unavailable; dry-run lists candidates without ref validation",
        }

    con = _connect_ro(Path(canon_db))
    try:
        valid_set: set[str] = set()
        invalid_set: set[str] = set()
        for ref in distinct:
            ok = con.execute(
                "SELECT 1 FROM canonical_messages WHERE canonical_message_id=? LIMIT 1",
                (ref,),
            ).fetchone()
            if ok:
                valid_set.add(ref)
            else:
                invalid_set.add(ref)
    finally:
        con.close()

    validated = [(uid, ref) for uid, ref in candidates if ref in valid_set]
    invalid = [(uid, ref) for uid, ref in candidates if ref in invalid_set]
    return {
        "validated": validated,
        "invalid": invalid,
        "skipped_no_canon": [],
        "canon_available": True,
        "distinct_refs": len(distinct),
        "valid_refs": len(valid_set),
        "invalid_refs": len(invalid_set),
        "invalid_ref_samples": sorted(invalid_set)[:10],
    }


def insert_evidence(
    ku_con: sqlite3.Connection,
    pairs: list[tuple[str, str]],
) -> int:
    """INSERT OR IGNORE evidence rows. Returns number of rows newly inserted."""
    if not pairs:
        return 0
    before = ku_con.execute("SELECT COUNT(*) FROM knowledge_unit_evidence").fetchone()[0]
    ku_con.executemany(
        "INSERT OR IGNORE INTO knowledge_unit_evidence "
        "(unit_id, evidence_ref, evidence_type) VALUES (?,?,?)",
        [(uid, ref, "message") for uid, ref in pairs],
    )
    after = ku_con.execute("SELECT COUNT(*) FROM knowledge_unit_evidence").fetchone()[0]
    return after - before


def run_backfill(
    *,
    db_path: Path,
    canon_db: Path | None,
    write: bool = False,
    limit: int | None = None,
    require_canon: bool = True,
) -> dict[str, Any]:
    """Run dry-run or write backfill. Never deletes evidence or edits unit text."""
    if not db_path.exists():
        return {
            "ok": False,
            "error": f"unified DB not found: {db_path}",
            "mode": "write" if write else "dry-run",
        }

    # Stats always read-only snapshot first
    ro = _connect_ro(db_path)
    try:
        before = coverage_stats(ro)
        candidates = find_candidates(ro, limit=limit)
    finally:
        ro.close()

    validation = validate_refs(candidates, canon_db)
    if require_canon and not validation["canon_available"]:
        to_insert: list[tuple[str, str]] = []
        insert_policy = "blocked_no_canon"
    elif validation["canon_available"]:
        to_insert = validation["validated"]
        insert_policy = "validated_against_canonical"
    else:
        # explicit override path for tests / offline
        to_insert = list(candidates)
        insert_policy = "no_canon_insert_all_refs"

    missing_with_ref = before["units_without_evidence_with_ref"]
    safe_fill_rate = (
        round(len(to_insert) / missing_with_ref, 6) if missing_with_ref else 0.0
    )
    # When --limit is set, report fill rate relative to candidate batch too
    batch_fill_rate = (
        round(len(to_insert) / len(candidates), 6) if candidates else 0.0
    )

    report: dict[str, Any] = {
        "ok": True,
        "mode": "write" if write else "dry-run",
        "generated_at": _utc_now(),
        "db_path": str(db_path),
        "canon_db": str(canon_db) if canon_db else None,
        "limit": limit,
        "require_canon": require_canon,
        "insert_policy": insert_policy,
        "before": before,
        "candidates_total": len(candidates),
        "candidates_validated": len(to_insert),
        "candidates_invalid_ref": len(validation.get("invalid") or []),
        "validation": {
            "canon_available": validation["canon_available"],
            "distinct_refs": validation["distinct_refs"],
            "valid_refs": validation["valid_refs"],
            "invalid_refs": validation["invalid_refs"],
            "invalid_ref_samples": validation.get("invalid_ref_samples", []),
            "note": validation.get("note"),
        },
        "safe_fill_rate_of_missing_with_ref": safe_fill_rate,
        "batch_fill_rate": batch_fill_rate,
        "would_insert": len(to_insert) if not write else None,
        "inserted": 0,
        "after": None,
        "coverage_delta": None,
        "recommendation": None,
    }

    if not write:
        projected_with = before["units_with_evidence"] + len(to_insert)
        projected_cov = (
            round(projected_with / before["total_units"], 6)
            if before["total_units"]
            else 0.0
        )
        report["projected_after"] = {
            "units_with_evidence": projected_with,
            "coverage": projected_cov,
            "evidence_rows": before["evidence_rows"] + len(to_insert),
        }
        if safe_fill_rate > 0.30 and validation["canon_available"]:
            report["recommendation"] = "safe_to_write"
        elif not validation["canon_available"] and require_canon:
            report["recommendation"] = "blocked_no_canon"
        elif safe_fill_rate <= 0.30:
            report["recommendation"] = "low_fill_rate_dry_run_only"
        else:
            report["recommendation"] = "review"
        return report

    # Write path
    if not to_insert:
        report["inserted"] = 0
        report["after"] = before
        report["coverage_delta"] = 0.0
        report["recommendation"] = "nothing_to_insert"
        return report

    rw = _connect_rw(db_path)
    try:
        inserted = insert_evidence(rw, to_insert)
        rw.commit()
        after = coverage_stats(rw)
    finally:
        rw.close()

    report["inserted"] = inserted
    report["after"] = after
    report["coverage_delta"] = round(after["coverage"] - before["coverage"], 6)
    report["recommendation"] = "write_complete"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill knowledge_unit_evidence from source_message_ref"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report only (default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="INSERT OR IGNORE missing evidence rows",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=UNIFIED_DB,
        help=f"personal_system.sqlite path (default: {UNIFIED_DB})",
    )
    parser.add_argument(
        "--canon-db",
        type=Path,
        default=AGENT_CONVERSATIONS_DB,
        help=f"canonical conversations DB (default: {AGENT_CONVERSATIONS_DB})",
    )
    parser.add_argument(
        "--no-canon",
        action="store_true",
        help="Do not require/validate against canonical_messages (tests/offline)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max candidate units to process",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional JSON report output path",
    )
    args = parser.parse_args(argv)

    write = bool(args.write)
    # argparse default dry-run=True; --write flips mode
    if write:
        dry = False
    else:
        dry = True

    report = run_backfill(
        db_path=args.db,
        canon_db=None if args.no_canon else args.canon_db,
        write=write,
        limit=args.limit,
        require_canon=not args.no_canon,
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))

    out = args.report
    if out is None and dry:
        out = DEFAULT_REPORT_PATH
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n[report] wrote {out}", file=sys.stderr)

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
