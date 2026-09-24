"""Read-only L2 pilot/full/merge lineage reconciliation.

Explains the 768 (full) + 47 (pilot) = 815 merged L2 units discrepancy and
lists terminal job failures. Never writes to live DBs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import AI_CONTEXT_DIR, UNIFIED_DB  # noqa: E402
from personal_knowledge.evaluation.eval_contracts import content_checksum, dump_json  # noqa: E402

DEFAULT_OUT = AI_CONTEXT_DIR / "l2_lineage_reconcile.json"

KNOWN_L2_RUNS = {
    "2a63b7e98fd3454c1aae3deedcdf038d": "pilot",
    "205bff9560b915508f343aebc0fe4b0b": "full",
}


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def reconcile(db_path: Path = UNIFIED_DB) -> dict:
    if not db_path.exists():
        return {
            "ok": False,
            "error": f"db missing: {db_path}",
            "generated_at": _utc(),
        }

    db_hash_before = _file_sha256(db_path)
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    run_rows = con.execute(
        "SELECT run_id, run_type, generated_at, source_build_id, prompt_version, "
        "status, stats_json, model FROM knowledge_build_runs "
        "WHERE source_build_id='l2_session_window' OR run_id IN ({})".format(
            ",".join("?" * len(KNOWN_L2_RUNS))
        ),
        tuple(KNOWN_L2_RUNS.keys()),
    ).fetchall()

    runs: list[dict] = []
    unit_counts: dict[str, int] = {}
    for r in run_rows:
        rid = r["run_id"]
        n = con.execute(
            "SELECT COUNT(*) AS c FROM knowledge_units WHERE unit_id LIKE 'l2|%' AND run_id=?",
            (rid,),
        ).fetchone()["c"]
        unit_counts[rid] = n
        status_break = {
            row["status"]: row["c"]
            for row in con.execute(
                "SELECT status, COUNT(*) AS c FROM knowledge_units "
                "WHERE unit_id LIKE 'l2|%' AND run_id=? GROUP BY status",
                (rid,),
            )
        }
        job_break = {
            row["status"]: row["c"]
            for row in con.execute(
                "SELECT status, COUNT(*) AS c FROM knowledge_l2_session_jobs "
                "WHERE run_id=? GROUP BY status",
                (rid,),
            )
        }
        runs.append(
            {
                "run_id": rid,
                "label": KNOWN_L2_RUNS.get(rid, "unknown"),
                "run_type": r["run_type"],
                "generated_at": r["generated_at"],
                "source_build_id": r["source_build_id"],
                "prompt_version": r["prompt_version"],
                "status": r["status"],
                "model": r["model"],
                "stats_json": r["stats_json"],
                "l2_unit_count": n,
                "unit_status": status_break,
                "job_status": job_break,
            }
        )

    # Units that appear under multiple runs (should be empty for pure dual-pass)
    multi = con.execute(
        "SELECT unit_id, COUNT(DISTINCT run_id) AS rc "
        "FROM knowledge_units WHERE unit_id LIKE 'l2|%' "
        "GROUP BY unit_id HAVING rc > 1"
    ).fetchall()

    total_l2 = con.execute(
        "SELECT COUNT(*) AS c FROM knowledge_units WHERE unit_id LIKE 'l2|%'"
    ).fetchone()["c"]
    total_l2_current = con.execute(
        "SELECT COUNT(*) AS c FROM knowledge_units WHERE unit_id LIKE 'l2|%' AND status='current'"
    ).fetchone()["c"]

    # Canonical members linked from L2
    l2_member_links = con.execute(
        "SELECT COUNT(*) AS c FROM canonical_unit_members m "
        "JOIN knowledge_units u ON u.unit_id = m.member_unit_id "
        "WHERE u.unit_id LIKE 'l2|%'"
    ).fetchone()["c"]

    # Terminal failures (full run preferred)
    failed_jobs = [
        dict(row)
        for row in con.execute(
            "SELECT run_id, session_id, status, attempt_count, unit_count, "
            "substr(last_error, 1, 200) AS last_error, updated_at "
            "FROM knowledge_l2_session_jobs WHERE status='terminal_failed' "
            "ORDER BY run_id, session_id"
        )
    ]

    # Active index
    active = ""
    try:
        from personal_knowledge.application.knowledge.promote_knowledge_index import read_active

        active = read_active()
    except Exception:
        row = con.execute(
            "SELECT collection_name FROM knowledge_index_versions WHERE status='active' LIMIT 1"
        ).fetchone()
        active = row["collection_name"] if row else ""

    canon_current = con.execute(
        "SELECT COUNT(*) AS c FROM canonical_knowledge_units WHERE status='current'"
    ).fetchone()["c"]

    con.close()
    db_hash_after = _file_sha256(db_path)

    pilot_n = unit_counts.get("2a63b7e98fd3454c1aae3deedcdf038d", 0)
    full_n = unit_counts.get("205bff9560b915508f343aebc0fe4b0b", 0)
    explained = pilot_n + full_n

    discrepancy = {
        "full_run_units": full_n,
        "pilot_run_units": pilot_n,
        "sum_run_units": explained,
        "total_l2_units": total_l2,
        "total_l2_status_current": total_l2_current,
        "merge_report_expected": 815,
        "explanation": (
            f"Full L2 run produced {full_n} units; pilot run produced {pilot_n}; "
            f"sum={explained}. Merge loaded all L2 units with status in (staging,current) "
            f"across runs (= {total_l2_current} current). Difference 768 vs 815 is pilot "
            f"({pilot_n}) + full ({full_n})."
        ),
        "multi_run_unit_ids": [r["unit_id"] for r in multi],
        "classification": {
            "accounted": explained == total_l2 and not multi,
            "pilot_plus_full_equals_total": explained == total_l2,
            "unique_unit_per_run": len(multi) == 0,
        },
    }

    ok = (
        discrepancy["classification"]["accounted"]
        and db_hash_before == db_hash_after
        and total_l2_current == explained
    )

    report = {
        "generated_at": _utc(),
        "ok": ok,
        "db_path": str(db_path).replace("\\", "/"),
        "db_sha256_before": db_hash_before,
        "db_sha256_after": db_hash_after,
        "db_unchanged": db_hash_before == db_hash_after,
        "active_collection": active,
        "canonical_current_count": canon_current,
        "l2_member_links": l2_member_links,
        "runs": runs,
        "discrepancy": discrepancy,
        "terminal_failures": failed_jobs,
        "terminal_failure_count": len(failed_jobs),
        "checksum": content_checksum(
            {
                "unit_counts": unit_counts,
                "total_l2_current": total_l2_current,
                "failed": len(failed_jobs),
            }
        ),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reconcile L2 lineage (read-only)")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--check", action="store_true", help="exit non-zero if not ok")
    p.add_argument("--json-stdout", action="store_true")
    args = p.parse_args(argv)

    report = reconcile(args.db)
    if args.out:
        dump_json(args.out, report)
        print(f"[l2-lineage] wrote {args.out}")
    if args.json_stdout:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        d = report.get("discrepancy") or {}
        print(
            f"[l2-lineage] ok={report.get('ok')} "
            f"full={d.get('full_run_units')} pilot={d.get('pilot_run_units')} "
            f"current={d.get('total_l2_status_current')} "
            f"failures={report.get('terminal_failure_count')} "
            f"db_unchanged={report.get('db_unchanged')}"
        )
        print(d.get("explanation", ""))
    if args.check and not report.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
