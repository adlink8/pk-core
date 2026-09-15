"""Phase 17-03: report rendering binds metric keys; N/A not zero."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.evaluation.render_knowledge_eval_report import (  # noqa: E402
    build_chart_specs,
    render_html,
    try_render_pngs,
)


def test_report_chart_specs_bind_run_and_metric() -> None:
    run = {
        "run_id": "abc123",
        "modes": {
            "raw": {"aggregate": {"recall_at": {"5": 0.5}, "mrr_at_5": 0.4}},
            "l1": {"aggregate": {"recall_at": {"5": 0.65}, "mrr_at_5": 0.5}},
        },
        "comparisons": {
            "l1": {
                "delta_pp": 15.0,
                "bootstrap": {"ci_low_pp": 5.0, "ci_high_pp": 25.0},
                "win_loss": {"n_win": 3, "n_loss": 1},
            }
        },
        "gate": {"passed": False, "verdict": "FAIL"},
        "primary_claims": {"summary": "未证明提升"},
        "scorer_version": "v1",
    }
    specs = build_chart_specs(run)
    assert specs
    for s in specs:
        assert s.get("run_id") == "abc123"
        assert s.get("metric_key")


def test_report_na_not_forced_zero(tmp_path: Path) -> None:
    run = {
        "run_id": "r1",
        "modes": {
            "raw": {"aggregate": {"recall_at": {"5": None}}},
            "l1": {"aggregate": {"recall_at": {"5": 0.6}}},
        },
        "comparisons": {},
        "gate": {"verdict": "N/A"},
        "scorer_version": "v1",
    }
    specs = build_chart_specs(run)
    # values may include None — renderer drops them
    html = tmp_path / "report.html"
    render_html(run, [], html)
    text = html.read_text(encoding="utf-8")
    assert "N/A" in text
    assert "Desktop" not in text or "not Desktop" in text
    charts = try_render_pngs(tmp_path, specs)
    # if matplotlib present, only non-None plotted
    assert isinstance(charts, list)
