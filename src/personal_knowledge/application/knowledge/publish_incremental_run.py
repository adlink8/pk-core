"""Publish an incremental extraction/canonical run into current (additive).

Unlike StagingPublisher.promote (full backfill), this does NOT demote other runs'
current units. Only flips this run_id's staging rows → current, and marks the
build run validated.

Usage::

    python -m personal_knowledge.application.knowledge.publish_incremental_run --run ir_… --dry-run
    python -m personal_knowledge.application.knowledge.publish_incremental_run --run ir_… --write
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish_incremental_run(
    run_id: str,
    db_path: Path = UNIFIED_DB,
    *,
    write: bool = False,
) -> dict:
    if not run_id:
        raise ValueError("run_id required")

    con = connect_rw(db_path)
    con.row_factory = sqlite3.Row
    run = con.execute(
        "SELECT run_id, run_type, status FROM knowledge_build_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not run:
        con.close()
        raise ValueError(f"run not found: {run_id}")

    run_type = run["run_type"] or ""
    if run_type not in ("incremental",) and not run_id.startswith("ir_"):
        con.close()
        raise ValueError(
            f"refuse publish: run_type={run_type!r} run_id={run_id!r} "
            "(expected incremental / ir_*). Full backfill must not use this path."
        )

    units_staging = con.execute(
        "SELECT COUNT(*) AS c FROM knowledge_units WHERE run_id=? AND status='staging'",
        (run_id,),
    ).fetchone()["c"]
    units_current = con.execute(
        "SELECT COUNT(*) AS c FROM knowledge_units WHERE run_id=? AND status='current'",
        (run_id,),
    ).fetchone()["c"]
    canon_staging = con.execute(
        "SELECT COUNT(*) AS c FROM canonical_knowledge_units "
        "WHERE run_id=? AND status='staging'",
        (run_id,),
    ).fetchone()["c"]
    canon_current = con.execute(
        "SELECT COUNT(*) AS c FROM canonical_knowledge_units "
        "WHERE run_id=? AND status='current'",
        (run_id,),
    ).fetchone()["c"]

    report = {
        "run_id": run_id,
        "run_type": run_type,
        "run_status_before": run["status"],
        "write": write,
        "units_staging": units_staging,
        "units_current_before": units_current,
        "canonical_staging": canon_staging,
        "canonical_current_before": canon_current,
        "demoted_other_runs": 0,
        "candidate_excluded": 0,
        "active_pointer_touched": False,
        "published_at": _utc_now() if write else None,
    }

    if not write:
        report["no_op"] = units_staging == 0 and canon_staging == 0
        con.close()
        return report

    # Additive only — never demote other runs
    assert_foreign_key_integrity(con)
    report["candidate_excluded"] = con.execute(
        "SELECT COUNT(*) FROM knowledge_units "
        "WHERE run_id=? AND status='staging' AND lifecycle='candidate'",
        (run_id,),
    ).fetchone()[0]
    con.execute(
        "UPDATE knowledge_units SET status='current' "
        "WHERE run_id=? AND status='staging' AND lifecycle<>'candidate'",
        (run_id,),
    )
    units_promoted = con.execute("SELECT changes()").fetchone()[0]
    con.execute(
        "UPDATE canonical_knowledge_units SET status='current' "
        "WHERE run_id=? AND status='staging'",
        (run_id,),
    )
    canon_promoted = con.execute("SELECT changes()").fetchone()[0]
    con.execute(
        "UPDATE knowledge_build_runs SET status='validated' WHERE run_id=?",
        (run_id,),
    )
    con.commit()

    report["units_promoted"] = units_promoted
    report["canonical_promoted"] = canon_promoted
    report["run_status_after"] = "validated"
    report["units_current_after"] = con.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE run_id=? AND status='current'",
        (run_id,),
    ).fetchone()[0]
    report["canonical_current_after"] = con.execute(
        "SELECT COUNT(*) FROM canonical_knowledge_units "
        "WHERE run_id=? AND status='current'",
        (run_id,),
    ).fetchone()[0]
    report["canonical_current_total"] = con.execute(
        "SELECT COUNT(*) FROM canonical_knowledge_units WHERE status='current'"
    ).fetchone()[0]
    con.close()
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Additive publish: incremental run staging → current (no demote)"
    )
    p.add_argument("--run", required=True, help="incremental run_id (ir_*)")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--write", action="store_true")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    write = bool(args.write)
    try:
        report = publish_incremental_run(args.run, args.db, write=write)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if write and report.get("units_promoted", 0) == 0 and report.get("canonical_promoted", 0) == 0:
        print("[warn] nothing promoted (already current or empty staging)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
