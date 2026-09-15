"""Phase 09 Wave 5: controlled promotion dry-run.

Default mode is dry-run. This script reads `memory_promotion_report.json` and
shows what would be applied. It refuses to apply human-review-required records
and does not create a real `memory_conversation_links` write path in Wave 5.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
REPORT_JSON = ROOT / "integration" / "analysis" / "ai_context" / "memory_promotion_report.json"
LONG_TERM_TABLES = ("memory_items", "memory_links", "memory_relations")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def load_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing promotion report: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "reviews" not in data:
        raise ValueError("promotion report must contain a reviews array")
    return data


def long_term_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in LONG_TERM_TABLES}


def eligible_reviews(report: dict[str, Any], *, approved_only: bool) -> list[dict[str, Any]]:
    reviews = report.get("reviews") or []
    if approved_only:
        return [
            item
            for item in reviews
            if item.get("promotion_status") == "approved"
            and not item.get("human_review_required")
            and item.get("auto_approval_eligible", item.get("promotion_status") == "approved")
        ]
    return [
        item
        for item in reviews
        if not item.get("human_review_required")
        and item.get("auto_approval_eligible", item.get("promotion_status") == "approved")
    ]


def planned_action(review: dict[str, Any]) -> dict[str, Any]:
    target = review.get("merge_or_replace_target") or {}
    action = target.get("action") or "none"
    if action in {"merge", "replace"}:
        operation = action
        target_memory_id = target.get("memory_id")
    else:
        operation = "insert_memory_item"
        target_memory_id = None
    return {
        "promotion_id": review.get("promotion_id"),
        "operation": operation,
        "target_memory_id": target_memory_id,
        "memory_type": review.get("memory_type"),
        "canonical_claim": review.get("canonical_claim"),
        "final_score": review.get("final_score"),
        "auto_approval_eligible": review.get("auto_approval_eligible"),
        "would_write_tables": ["memory_items", "memory_links", "memory_relations"],
        "would_create_memory_conversation_links": False,
    }


def build_preview(
    report: dict[str, Any],
    before_counts: dict[str, int],
    *,
    report_path: Path,
    dry_run: bool,
    write: bool,
    approved_only: bool,
) -> dict[str, Any]:
    eligible = eligible_reviews(report, approved_only=approved_only)
    actions = [planned_action(item) for item in eligible]
    blocked = [
        item["promotion_id"]
        for item in report.get("reviews", [])
        if item.get("promotion_status") == "approved" and item.get("human_review_required")
    ]
    return {
        "mode": "write" if write else "dry-run",
        "dry_run": dry_run,
        "approved_only": approved_only,
        "report_path": rel(report_path),
        "report_status_distribution": report.get("status_distribution", {}),
        "eligible_count": len(eligible),
        "blocked_human_review_required_approved_count": len(blocked),
        "blocked_human_review_required_approved_ids": blocked,
        "actions": actions,
        "before_counts": before_counts,
        "after_counts": dict(before_counts),
        "long_term_tables_changed": False,
        "note": (
            "No eligible approved candidates; nothing to apply."
            if not actions
            else "Wave 4 write path is controlled and must be explicitly reviewed before real long-term writes."
        ),
    }


def run(
    *,
    db_path: Path = DB_PATH,
    report_path: Path = REPORT_JSON,
    dry_run: bool = True,
    write: bool = False,
    approved_only: bool = False,
) -> dict[str, Any]:
    if write and not approved_only:
        raise RuntimeError("--write requires --approved-only")
    report = load_report(report_path)
    with closing(sqlite3.connect(db_path)) as con:
        before = long_term_counts(con)
        preview = build_preview(
            report,
            before,
            report_path=report_path,
            dry_run=dry_run,
            write=write,
            approved_only=approved_only,
        )
        if write and preview["actions"]:
            raise RuntimeError(
                "Wave 4 refuses real long-term writes in this execution; review and implement a later approved apply path."
            )
        after = long_term_counts(con)
        preview["after_counts"] = after
        preview["long_term_tables_changed"] = before != after
    return preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run controlled memory promotions.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show planned actions only; default")
    mode.add_argument("--write", action="store_true", help="write eligible approved promotions")
    parser.add_argument("--approved-only", action="store_true", help="only consider approved non-review records")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--report-json", type=Path, default=REPORT_JSON)
    args = parser.parse_args(argv)

    preview = run(
        db_path=args.db,
        report_path=args.report_json,
        dry_run=not args.write,
        write=args.write,
        approved_only=args.approved_only,
    )
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
