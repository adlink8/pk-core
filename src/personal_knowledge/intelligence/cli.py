"""JSON CLI for read-only personal-state intelligence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from personal_knowledge.core.project_paths import KNOWLEDGE_ACTIVE_POINTER, ROOT, UNIFIED_DB

from .schema import checksum
from .service import (
    CHANGE_ALGORITHM_VERSION,
    EXPLANATION_SCHEMA_VERSION,
    INTERFACE_SCHEMA_VERSION,
    PROJECTION_RULE_VERSION,
    IntelligenceService,
)


ACCEPTANCE_SCHEMA_VERSION = "personal_state_acceptance_v1"
_PHASE24_DIR = (
    ROOT
    / ".planning"
    / "phases"
    / "PDA-24-evaluation-closure-and-lifecycle-adoption-close-target-b-c-q"
)
_PHASE24_CHECKPOINTS = tuple(
    _PHASE24_DIR / name
    for name in ("24-02-CHECKPOINT.md", "24-03-CHECKPOINT.md", "24-04-CHECKPOINT.md")
)
_FINGERPRINT_GROUPS = {
    "serving_authority": (
        "serving_authority", "serving_snapshots", "serving_snapshot_members",
        "serving_snapshot_events",
    ),
    "knowledge_units": ("canonical_knowledge_units",),
    "lifecycle": (
        "knowledge_lifecycle_manifests", "knowledge_lifecycle_actions",
        "knowledge_lifecycle_events", "knowledge_unit_corrections",
    ),
    "watermarks": ("source_watermarks", "knowledge_source_watermark"),
    "analysis": (
        "personal_state_runs", "personal_state_publications", "personal_state_assertions", "personal_state_evidence",
        "personal_state_changes", "personal_state_risks",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-state-intelligence")
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    for name in ("current", "history"):
        item = state_sub.add_parser(name)
        _context_args(item)
        item.add_argument("--limit", type=int, default=50)
    explain = state_sub.add_parser("explain")
    _context_args(explain)
    for field in ("assertion-kind", "subject", "domain", "scope", "predicate"):
        explain.add_argument(f"--{field}", required=True)

    changes = sub.add_parser("changes")
    changes_sub = changes.add_subparsers(dest="changes_command", required=True)
    recent = changes_sub.add_parser("recent")
    _context_args(recent)
    recent.add_argument("--window-start")
    recent.add_argument("--limit", type=int, default=50)

    build = sub.add_parser("build", help="Plan a future analysis run; dry-run only in Phase 25")
    build.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    build.add_argument("--write", action="store_true", help="Reserved; rejected in Phase 25")
    build.add_argument("--json", action="store_true")

    acceptance = sub.add_parser("acceptance", help="Run bounded metadata-only acceptance")
    acceptance.add_argument("--dry-run", action="store_true", required=True)
    acceptance.add_argument("--metadata-only", action="store_true", required=True)
    acceptance.add_argument("--json", action="store_true")
    acceptance.add_argument("--limit", type=int, default=10)
    acceptance.add_argument("--active-pointer", type=Path, default=None)
    return parser


def _context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot-id")
    parser.add_argument("--run-id")
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true")


def _invoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "acceptance":
        return run_acceptance(
            args.db or UNIFIED_DB,
            limit=args.limit,
            pointer_path=args.active_pointer or KNOWLEDGE_ACTIVE_POINTER,
        )
    if args.command == "build":
        if args.write or not args.dry_run:
            return {
                "schema_version": "personal_state_interface_v1",
                "operation": "build",
                "ok": False,
                "status": "error",
                "error": {"code": "write_not_available", "detail": "Phase 25 is dry-run only"},
                "privacy": {"metadata_only": True, "private_bodies": 0},
            }
        return {
            "schema_version": "personal_state_interface_v1",
            "operation": "build",
            "ok": True,
            "status": "empty",
            "dry_run": True,
            "written": False,
            "privacy": {"metadata_only": True, "private_bodies": 0},
        }
    service = IntelligenceService(args.db or UNIFIED_DB)
    common = {
        "snapshot_id": args.snapshot_id,
        "run_id": args.run_id,
        "as_of": args.as_of,
    }
    if args.command == "state" and args.state_command == "current":
        return service.invoke("state.current", **common, limit=args.limit)
    if args.command == "state" and args.state_command == "history":
        return service.invoke("state.history", **common, limit=args.limit)
    if args.command == "state" and args.state_command == "explain":
        return service.invoke(
            "state.explain",
            **common,
            assertion_kind=args.assertion_kind,
            subject=args.subject,
            domain=args.domain,
            scope=args.scope,
            predicate=args.predicate,
        )
    if args.command == "changes" and args.changes_command == "recent":
        return service.invoke(
            "changes.recent", **common, window_start=args.window_start, limit=args.limit
        )
    raise AssertionError("unreachable parser state")


def _checkpoint_status(path: Path) -> dict[str, str]:
    if not path.exists():
        return {"checkpoint": path.stem, "status": "missing", "checksum": ""}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    status = "unknown"
    in_frontmatter = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and line.startswith("status:"):
            status = line.split(":", 1)[1].strip()
    return {"checkpoint": path.stem, "status": status, "checksum": digest}


def _table_fingerprint(con: sqlite3.Connection, table: str) -> dict[str, Any]:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return {"table": table, "exists": False, "row_count": 0, "checksum": ""}
    digest = hashlib.sha256()
    count = 0
    cursor = con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
    for row in cursor:
        digest.update(repr(tuple(row)).encode("utf-8", errors="backslashreplace"))
        digest.update(b"\n")
        count += 1
    return {
        "table": table,
        "exists": True,
        "row_count": count,
        "checksum": digest.hexdigest(),
    }


def _fingerprints(db_path: Path, pointer_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        groups = {
            group: [_table_fingerprint(con, table) for table in tables]
            for group, tables in _FINGERPRINT_GROUPS.items()
        }
    finally:
        con.close()
    pointer_checksum = (
        hashlib.sha256(pointer_path.read_bytes()).hexdigest()
        if pointer_path.exists()
        else ""
    )
    return {
        "groups": groups,
        "active_pointer": {
            "exists": pointer_path.exists(),
            "checksum": pointer_checksum,
        },
        "checksum": checksum({"groups": groups, "active_pointer_checksum": pointer_checksum}),
    }


def _phase24_dependency_status(db_path: Path) -> dict[str, Any]:
    from personal_knowledge.core.lifecycle import lifecycle_status
    from personal_knowledge.evaluation.review_packets import status as review_status

    checkpoints = [_checkpoint_status(path) for path in _PHASE24_CHECKPOINTS]
    review = review_status()
    lifecycle = lifecycle_status(db_path)
    unresolved = [
        f"checkpoint:{row['checkpoint']}:{row['status']}"
        for row in checkpoints
        if row["status"] not in {"complete", "completed", "pass", "passed"}
    ]
    unresolved.extend(
        f"review:{name}" for name, passed in review.get("checks", {}).items() if not passed
    )
    unresolved.extend(
        f"lifecycle:{name}"
        for name, passed in lifecycle.get("checks", {}).items()
        if not passed
    )
    return {
        "status": "release_blocked" if unresolved else "release_ready",
        "release_blocked": bool(unresolved),
        "checkpoints": checkpoints,
        "human_review_strict": {
            "ok": bool(review.get("ok")),
            "checks": review.get("checks", {}),
        },
        "lifecycle_strict": {
            "ok": bool(lifecycle.get("ok")),
            "checks": lifecycle.get("checks", {}),
            "applied_manifests": int(lifecycle.get("applied_manifests") or 0),
            "event_count": int(lifecycle.get("event_count") or 0),
        },
        "reason_codes": sorted(unresolved),
    }


def run_acceptance(
    db_path: Path | str,
    *,
    limit: int = 10,
    pointer_path: Path = KNOWLEDGE_ACTIVE_POINTER,
) -> dict[str, Any]:
    """Execute the Phase 25 live acceptance without any write-capable path."""
    path = Path(db_path)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
        return IntelligenceService._error("acceptance", "invalid_limit", str(limit))
    if not path.exists():
        return IntelligenceService._error("acceptance", "database_missing", str(path))

    before = _fingerprints(path, pointer_path)
    service = IntelligenceService(path)
    current = service.invoke("state.current", limit=limit)
    intelligence_ok = True
    intelligence_error: dict[str, Any] | None = None
    if current.get("ok"):
        snapshot = dict(current["snapshot"])
        samples = list(current.get("data", {}).get("items", ()))[:limit]
        candidate_reason = "bounded_committed_run_replay"
        run_context = dict(current["run"])
    else:
        # The active snapshot may legitimately have no committed analysis run.
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT s.snapshot_id,s.manifest_hash FROM serving_authority a "
                "JOIN serving_snapshots s ON s.snapshot_id=a.active_snapshot_id "
                "WHERE a.singleton_id=1"
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return IntelligenceService._error("acceptance", "snapshot_missing", "active")
        snapshot = {"snapshot_id": str(row[0]), "snapshot_hash": str(row[1])}
        samples = []
        analysis_table_flags = [item["exists"] for item in before["groups"]["analysis"]]
        analysis_tables_ready = all(analysis_table_flags)
        error_code = str(current.get("error", {}).get("code") or "no_committed_run")
        schema_unapplied = error_code == "invalid_intelligence_state" and not any(
            analysis_table_flags
        )
        expected_empty = schema_unapplied or (error_code == "run_missing" and analysis_tables_ready)
        candidate_reason = "analysis_schema_unapplied" if schema_unapplied else error_code
        if not expected_empty:
            intelligence_ok = False
            intelligence_error = dict(current.get("error") or {"code": error_code, "detail": ""})
        run_context = {"run_id": None, "run_checksum": None, "producer_version": None}

    dependency = _phase24_dependency_status(path)
    run_plan = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "snapshot": snapshot,
        "selected_run": run_context,
        "rules": {
            "interface": INTERFACE_SCHEMA_VERSION,
            "projection": PROJECTION_RULE_VERSION,
            "changes": CHANGE_ALGORITHM_VERSION,
            "explanation": EXPLANATION_SCHEMA_VERSION,
        },
        "limit": limit,
        "candidate_reason": candidate_reason,
        "candidate_count": len(samples),
        "release_status": dependency["status"],
    }
    run_plan_checksum = checksum(run_plan)
    after = _fingerprints(path, pointer_path)
    unchanged = before == after
    reason_codes = [candidate_reason, *dependency["reason_codes"]]
    if not intelligence_ok:
        reason_codes.append("intelligence_integrity_gate_failed")
    if not unchanged:
        reason_codes.append("fingerprint_changed")
    return {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "operation": "acceptance",
        "ok": unchanged and intelligence_ok,
        "status": (
            "release_blocked"
            if dependency["release_blocked"] or not intelligence_ok or not unchanged
            else "pass"
        ),
        "dry_run": True,
        "metadata_only": True,
        "snapshot": snapshot,
        "run_plan": {
            "run_plan_id": f"psp_{run_plan_checksum[:24]}",
            "checksum": run_plan_checksum,
            **run_plan,
        },
        "candidate": {
            "computed": intelligence_ok,
            "bounded": True,
            "limit": limit,
            "count": len(samples),
            "persisted_rows": 0,
            "reason_codes": sorted(set(reason_codes)),
            "metadata_samples": samples,
        },
        "fingerprints": {
            "before": before,
            "after": after,
            "unchanged": unchanged,
        },
        "mutations": 0 if unchanged else 1,
        "private_bodies": 0,
        "network_calls": 0,
        "paid_calls": 0,
        "intelligence_gate": {
            "ok": intelligence_ok,
            "error": intelligence_error,
        },
        "phase24": dependency,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = _invoke(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
