"""Render offline HTML + PNG evaluation report (project analysis/ only)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import ANALYSIS_DIR  # noqa: E402

EVAL_ROOT = ANALYSIS_DIR / "evaluations"
LATEST_POINTER = EVAL_ROOT / "latest.txt"


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _na(v: Any) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _metric(run: Mapping[str, Any], mode: str, *keys: str) -> Any:
    m = (run.get("modes") or run.get("metrics") or {}).get(mode) or {}
    cur: Any = m
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def build_chart_specs(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    modes = list((run.get("modes") or run.get("metrics") or {}).keys())
    r5 = []
    for m in modes:
        v = _metric(run, m, "recall_at", "5")
        if v is None:
            v = _metric(run, m, "aggregate", "recall_at", "5")
        r5.append(v)
    return [
        {
            "id": "overview_r5",
            "title": "Recall@5 by mode",
            "metric_key": "aggregate.recall_at.5",
            "modes": modes,
            "values": r5,
            "run_id": run.get("run_id"),
        },
        {
            "id": "delta_vs_raw",
            "title": "Delta pp vs Raw (Recall@5)",
            "metric_key": "comparisons.*.delta_pp",
            "modes": list((run.get("comparisons") or {}).keys()),
            "values": [
                ((run.get("comparisons") or {}).get(m) or {}).get("delta_pp")
                for m in (run.get("comparisons") or {})
            ],
            "run_id": run.get("run_id"),
        },
    ]


def try_render_pngs(run_dir: Path, specs: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return paths

    for spec in specs:
        vals = spec.get("values") or []
        labels = spec.get("modes") or []
        # Drop N/A — do not plot as 0
        pairs = [(l, v) for l, v in zip(labels, vals) if v is not None]
        if not pairs:
            continue
        labels, vals = zip(*pairs)
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(labels, vals, color="#4C78A8")
        ax.set_title(spec["title"])
        ax.set_ylabel(spec.get("metric_key", ""))
        fig.tight_layout()
        out = run_dir / f"{spec['id']}.png"
        fig.savefig(out, dpi=120)
        plt.close(fig)
        paths.append(str(out.name))
    return paths


def render_html(run: Mapping[str, Any], chart_files: list[str], out_path: Path) -> None:
    modes = run.get("modes") or run.get("metrics") or {}
    comparisons = run.get("comparisons") or {}
    gate = run.get("gate") or {}
    rows = []
    for mode, payload in modes.items():
        agg = payload.get("aggregate") or payload
        r5 = (agg.get("recall_at") or {}).get("5")
        mrr = agg.get("mrr_at_5")
        rows.append(
            f"<tr><td>{mode}</td><td>{_na(r5)}</td><td>{_na(mrr)}</td>"
            f"<td>{_na(agg.get('privacy_hit'))}</td>"
            f"<td>{_na(agg.get('p95_latency_ms'))}</td></tr>"
        )
    cmp_rows = []
    for mode, c in comparisons.items():
        cmp_rows.append(
            f"<tr><td>{mode}</td><td>{_na(c.get('delta_pp'))}</td>"
            f"<td>{_na((c.get('bootstrap') or {}).get('ci_low_pp'))} .. "
            f"{_na((c.get('bootstrap') or {}).get('ci_high_pp'))}</td>"
            f"<td>{_na((c.get('win_loss') or {}).get('n_win'))}/"
            f"{_na((c.get('win_loss') or {}).get('n_loss'))}</td></tr>"
        )
    imgs = "".join(
        f'<figure><img src="{f}" alt="{f}" style="max-width:100%"/>'
        f"<figcaption>{f} · run {run.get('run_id','')}</figcaption></figure>"
        for f in chart_files
    )
    claims = run.get("primary_claims") or {}
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>Knowledge Eval {run.get('run_id','')}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;max-width:1100px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f5f5f5}}
.muted{{color:#666;font-size:13px}}
.pass{{color:#0a0}} .fail{{color:#a00}}
.kpi{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{border:1px solid #ddd;border-radius:8px;padding:12px;min-width:160px}}
</style></head><body>
<h1>Knowledge Unit Comprehensive Evaluation</h1>
<p class="muted">run_id={run.get('run_id')} · generated={run.get('generated_at') or _utc()} ·
dataset={run.get('dataset_checksum','')[:12]}… · scorer={run.get('scorer_version')}</p>
<div class="kpi">
  <div class="card"><b>Gate</b><br/><span class="{'pass' if gate.get('passed') else 'fail'}">{gate.get('verdict','N/A')}</span></div>
  <div class="card"><b>Primary claim</b><br/>{_na(claims.get('summary','N/A'))}</div>
  <div class="card"><b>Modes</b><br/>{', '.join(modes.keys()) or 'N/A'}</div>
</div>
<h2>Retrieval overview</h2>
<table><thead><tr><th>Mode</th><th>R@5</th><th>MRR@5</th><th>Privacy hit</th><th>p95 ms</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=5>N/A</td></tr>'}</tbody></table>
<h2>Deltas vs Raw</h2>
<table><thead><tr><th>Mode</th><th>Δpp R@5</th><th>95% CI pp</th><th>Win/Loss</th></tr></thead>
<tbody>{''.join(cmp_rows) or '<tr><td colspan=4>N/A</td></tr>'}</tbody></table>
<h2>Charts</h2>
{imgs or '<p class="muted">No charts (matplotlib missing or all N/A)</p>'}
<h2>Notes</h2>
<ul>
<li>N/A is never plotted as 0.</li>
<li>Hybrid gains must be read with layer attribution in JSON artifacts.</li>
<li>Private query text is not embedded in this report.</li>
</ul>
<p class="muted">Artifact dir: project var/reports/analysis/evaluations only (not Desktop).</p>
</body></html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def render_run(run_dir: Path) -> Path:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"missing {summary_path}")
    run = json.loads(summary_path.read_text(encoding="utf-8"))
    specs = build_chart_specs(run)
    charts = try_render_pngs(run_dir, specs)
    # write chart manifest
    (run_dir / "charts_manifest.json").write_text(
        json.dumps(specs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    html_path = run_dir / "report.html"
    render_html(run, charts, html_path)
    return html_path


def resolve_latest() -> Path:
    if LATEST_POINTER.exists():
        rel = LATEST_POINTER.read_text(encoding="utf-8").strip()
        p = EVAL_ROOT / rel if not Path(rel).is_absolute() else Path(rel)
        if p.exists():
            return p
    # fallback: newest dir
    if not EVAL_ROOT.exists():
        raise FileNotFoundError("no evaluations directory")
    dirs = sorted([d for d in EVAL_ROOT.iterdir() if d.is_dir()], reverse=True)
    if not dirs:
        raise FileNotFoundError("no evaluation runs")
    return dirs[0]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render knowledge eval report")
    p.add_argument("--run-dir", type=Path, default=None)
    p.add_argument("--latest", action="store_true")
    args = p.parse_args(argv)
    run_dir = args.run_dir
    if args.latest or run_dir is None:
        run_dir = resolve_latest()
    html = render_run(run_dir)
    print(f"[render] {html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
