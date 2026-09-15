from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED = {
    "node_count", "files", "directories", "symlinks", "reparse", "deepest_depth",
    "excluded_descendants", "by_zone", "by_kind", "by_privacy", "by_owner",
    "by_status", "coverage_percent", "metadata_completeness_percent",
    "generated_lineage_completeness_percent",
}


def sanitized_summary(inventory: dict[str, Any]) -> dict[str, Any]:
    summary = inventory.get("summary", {})
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": "R2-aggregate-only",
        "secret_private_content_scan": 0,
        "metrics": {key: summary[key] for key in sorted(ALLOWED) if key in summary},
        "orphan_residuals": [],
    }


def record_preflight(report: dict[str, Any], registry: Path) -> int:
    """Persist aggregate gate outcomes only; findings contain no body excerpts."""
    registry.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(registry) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS governance_runs (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, policy_id TEXT NOT NULL,
            passed INTEGER NOT NULL, gate_count INTEGER NOT NULL,
            failed_gate_count INTEGER NOT NULL, finding_count INTEGER NOT NULL,
            p0_count INTEGER NOT NULL, metrics_json TEXT NOT NULL)""")
        gates = report.get("gates", [])
        findings = report.get("findings", [])
        metrics = {
            "gates": {str(row["gate"]): bool(row["ok"]) for row in gates},
            "finding_severity": {level: sum(item.get("severity") == level for item in findings) for level in ("P0", "P1", "P2")},
        }
        cursor = db.execute(
            "INSERT INTO governance_runs(created_at,policy_id,passed,gate_count,failed_gate_count,finding_count,p0_count,metrics_json) VALUES(?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), report.get("policy_id", "unknown"), int(bool(report.get("ok"))),
             len(gates), sum(not row.get("ok") for row in gates), len(findings),
             sum(row.get("severity") == "P0" for row in findings), json.dumps(metrics, sort_keys=True)),
        )
        db.commit()
        return int(cursor.lastrowid)


def render_preflight_html(report: dict[str, Any], run_id: int) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(str(g['gate']))}</td><td>{'PASS' if g['ok'] else 'FAIL'}</td>"
        f"<td>{html.escape(str(g['owner']))}</td><td>{html.escape(str(g['policy']))}</td></tr>"
        for g in report.get("gates", [])
    )
    return ("<!doctype html><meta charset='utf-8'><title>Governance report</title>"
            "<style>body{font-family:system-ui;max-width:1000px;margin:2rem auto}table{border-collapse:collapse;width:100%}"
            "td,th{border:1px solid #ccc;padding:.45rem;text-align:left}.ok{color:#087830}.bad{color:#b00020}</style>"
            f"<h1>Repository governance</h1><p>Run {run_id} · policy {html.escape(str(report.get('policy_id')))}</p>"
            f"<p class={'ok' if report.get('ok') else 'bad'}>{'PASS' if report.get('ok') else 'FAIL'}</p>"
            f"<table><thead><tr><th>Gate</th><th>Status</th><th>Owner</th><th>Policy</th></tr></thead><tbody>{rows}</tbody></table>"
            "<p>Aggregate metadata only. R3/R4 bodies, paths and excerpts are never rendered.</p>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an aggregate-only governance baseline")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--history", type=Path, default=Path("integration/runtime/governance/governance_history.sqlite"))
    args = parser.parse_args(argv)
    if bool(args.inventory) == bool(args.preflight):
        parser.error("provide exactly one of --inventory or --preflight")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.inventory:
        report = sanitized_summary(json.loads(args.inventory.read_text(encoding="utf-8")))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["metrics"], ensure_ascii=False, sort_keys=True))
    else:
        report = json.loads(args.preflight.read_text(encoding="utf-8"))
        run_id = record_preflight(report, args.history)
        args.output.write_text(render_preflight_html(report, run_id), encoding="utf-8")
        print(json.dumps({"run_id": run_id, "ok": report.get("ok"), "gates": len(report.get("gates", []))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
