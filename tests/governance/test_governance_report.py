from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from integration.scripts.governance.render_governance_report import record_preflight, render_preflight_html, sanitized_summary


def test_report_is_aggregate_only_and_has_trend_dimensions() -> None:
    inventory = {
        "nodes": [{"path": "integration/runtime/private_evals/secret-user.json"}],
        "summary": {
            "node_count": 3,
            "files": 2,
            "directories": 1,
            "deepest_depth": 4,
            "excluded_descendants": 2,
            "by_zone": {"var": 3},
            "by_kind": {"generated": 3},
            "by_privacy": {"R4": 3},
            "by_owner": {"evaluation": 3},
            "by_status": {"active": 3},
            "coverage_percent": 100.0,
            "metadata_completeness_percent": 100.0,
            "generated_lineage_completeness_percent": 100.0,
        },
    }
    report = sanitized_summary(inventory)
    payload = json.dumps(report, ensure_ascii=False)
    assert "secret-user" not in payload
    assert "nodes" not in report
    assert report["secret_private_content_scan"] == 0
    assert report["orphan_residuals"] == []
    assert report["metrics"]["by_zone"] == {"var": 3}


def test_tracked_baseline_contains_no_paths_or_private_content() -> None:
    baseline = Path(__file__).resolve().parents[2] / "governance/baselines/inventory_summary.json"
    if not baseline.exists():
        return
    report = json.loads(baseline.read_text(encoding="utf-8"))
    assert "nodes" not in report
    assert report["secret_private_content_scan"] == 0
    assert "path" not in json.dumps(report).lower()


def test_preflight_history_and_html_are_aggregate_only(tmp_path: Path) -> None:
    report = {"ok": True, "policy_id": "policy-v1", "gates": [
        {"gate": "privacy-check", "ok": True, "owner": "governance", "policy": "privacy-v1"}],
        "findings": []}
    registry = tmp_path / "history.sqlite"
    run_id = record_preflight(report, registry)
    page = render_preflight_html(report, run_id)
    assert "privacy-check" in page and "R3/R4 bodies" in page
    assert "secret-user" not in page
    with sqlite3.connect(registry) as db:
        row = db.execute("SELECT policy_id, passed, gate_count FROM governance_runs").fetchone()
    assert row == ("policy-v1", 1, 1)
