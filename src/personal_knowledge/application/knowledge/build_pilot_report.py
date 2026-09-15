"""Phase 14 Plan 03: pilot report 生成。

从 run_id 读取 item/unit/cache 统计，生成 pilot report JSON。

用法::

    python build_pilot_report.py --run <run_id>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, AI_CONTEXT_DIR  # noqa: E402
from personal_knowledge.evaluation.knowledge.evaluate_knowledge_unit_extraction import evaluate_run  # noqa: E402

REPORT_PATH = AI_CONTEXT_DIR / "knowledge_unit_pilot_report.json"


def generate_report(run_id: str, db_path: Path = UNIFIED_DB) -> dict:
    """生成 pilot report。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # run manifest
    run = con.execute(
        "SELECT * FROM knowledge_build_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if not run:
        return {"error": f"run not found: {run_id}"}

    # item stats
    item_stats = {}
    for r in con.execute(
        "SELECT status, COUNT(*) c FROM knowledge_run_items WHERE run_id=? GROUP BY status",
        (run_id,),
    ):
        item_stats[r["status"]] = r["c"]

    # unit stats
    units_total = con.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    by_type = {}
    for r in con.execute(
        "SELECT unit_type, COUNT(*) c FROM knowledge_units WHERE run_id=? GROUP BY unit_type",
        (run_id,),
    ):
        by_type[r["unit_type"]] = r["c"]

    # cache stats
    cache_count = con.execute(
        "SELECT COUNT(*) FROM knowledge_response_cache WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    # terminal errors
    terminal_errors = []
    for r in con.execute(
        "SELECT evidence_ref, last_error_class, attempt_count "
        "FROM knowledge_run_items WHERE run_id=? AND status='terminal_failed'",
        (run_id,),
    ):
        terminal_errors.append(dict(r))

    con.close()

    # gate
    gate = evaluate_run(run_id, db_path)

    report = {
        "run_id": run_id,
        "status": run["status"],
        "model": run["model"],
        "generated_at": run["generated_at"],
        "item_stats": item_stats,
        "units_total": units_total,
        "by_type": by_type,
        "cache_count": cache_count,
        "terminal_errors": terminal_errors,
        "paid_call_count": item_stats.get("succeeded", 0) + item_stats.get("abstained", 0) + item_stats.get("terminal_failed", 0),
        "gate": {
            "status": gate.gate_status,
            "checks": [c.name for c in gate.checks],
            "summary": gate.summary,
        },
        "report_generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return report


def run(run_id: str, db_path: Path = UNIFIED_DB) -> int:
    report = generate_report(run_id, db_path)
    if "error" in report:
        print(f"[error] {report['error']}")
        return 1

    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已保存: {REPORT_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Plan 03: pilot report")
    p.add_argument("--run", required=True, help="run_id")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    args = p.parse_args(argv)
    return run(args.run, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
