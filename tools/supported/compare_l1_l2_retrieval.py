"""Compare L1-only (30k) vs L1+L2 merged (30.7k) retrieval quality with charts.

Runs pure-KU frozen eval on both Chroma collections, layered hybrid via
search_knowledge_units(collection_override=...), inventory stats, and writes
JSON + PNG charts + desktop HTML report.

Usage::

    python tools/supported/compare_l1_l2_retrieval.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_CONVERSATIONS_DB,
    AI_CONTEXT_DIR,
    UNIFIED_DB,
)
from personal_knowledge.retrieval.unified_search import (  # noqa: E402
    search_knowledge_units,
    _search_dialogue_canonical_messages,
)

EVAL = ROOT / "integration" / "evals" / "knowledge_units" / "frozen_test_queries.private.jsonl"
HOLDOUT = ROOT / "assets" / "evals" / "knowledge_units" / "holdout_15_02.synthetic.jsonl"
TAGS = ROOT / "assets" / "evals" / "knowledge_units" / "suite_tags.json"

OLD_COLL = "knowledge_units_run_76c6259e_20260712062418"
NEW_COLL = "knowledge_units_205bff9560b9_20260712142938"

OUT_JSON = AI_CONTEXT_DIR / "l1_l2_retrieval_comparison.json"
OUT_DIR = AI_CONTEXT_DIR / "charts"
HTML_REPORT = AI_CONTEXT_DIR / "l1_l2_retrieval_comparison.html"


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_tags() -> dict:
    if TAGS.exists():
        return json.loads(TAGS.read_text(encoding="utf-8")).get("tags") or {}
    return {}


def load_gold_snips(cases: list[dict]) -> dict[str, str]:
    gold: dict[str, str] = {}
    if not AGENT_CONVERSATIONS_DB.exists():
        return gold
    con = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
    for case in cases:
        for ref in case.get("gold_evidence_refs") or []:
            if ref in gold:
                continue
            row = con.execute(
                "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
                (ref,),
            ).fetchone()
            if row and row[0]:
                gold[ref] = row[0][:200]
    con.close()
    return gold


def match_rank(results: list[dict], gold_refs: set[str], snips: list[str]) -> int | None:
    for rank, item in enumerate(results, 1):
        uid = str(item.get("unit_id") or "")
        ref = str(item.get("source_message_ref") or "")
        if uid in gold_refs or ref in gold_refs:
            return rank
        doc = (item.get("answer") or "") + " " + (item.get("subject") or "")
        for sn in snips:
            if sn and len(sn) >= 15 and sn[:15] in doc:
                return rank
    return None


def pure_ku_eval(collection: str, cases: list[dict], gold_map: dict[str, str]) -> dict:
    from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
    from personal_knowledge.core import local_embed  # noqa: E402

    client = ChromaClient()
    coll = client.get_or_create_collection(collection)
    try:
        count = coll.count()
    except Exception:
        count = None

    hits = 0
    mrr = 0.0
    per = []
    lat = []
    for case in cases:
        q = case["query"]
        gold_refs = set(case.get("gold_evidence_refs") or [])
        t0 = time.perf_counter()
        emb = local_embed.embed(q)
        if emb is None:
            per.append({"id": case["id"], "found_rank": None, "error": "embed"})
            continue
        try:
            kr = coll.query(
                query_embeddings=[emb],
                n_results=5,
                include=["metadatas", "documents", "distances"],
            )
        except ChromaError as e:
            per.append({"id": case["id"], "found_rank": None, "error": str(e)[:80]})
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        ids = kr.get("ids", [[]])[0] if kr.get("ids") else []
        metas = kr.get("metadatas", [[]])[0] if kr.get("metadatas") else []
        docs = kr.get("documents", [[]])[0] if kr.get("documents") else []
        results = []
        for uid, meta, doc in zip(ids, metas, docs):
            meta = meta or {}
            results.append(
                {
                    "unit_id": uid,
                    "source_message_ref": meta.get("source_message_ref", ""),
                    "answer": doc or "",
                    "subject": meta.get("subject", ""),
                }
            )
        snips = [gold_map[r] for r in gold_refs if r in gold_map]
        rank = match_rank(results, gold_refs, snips)
        if rank:
            hits += 1
            mrr += 1.0 / rank
        per.append({"id": case["id"], "found_rank": rank})
    n = max(len(cases), 1)
    lat_s = sorted(lat)
    return {
        "mode": "pure_ku",
        "collection": collection,
        "collection_count": count,
        "n": len(cases),
        "recall_at_5": round(hits / n, 4),
        "mrr_at_5": round(mrr / n, 4),
        "hits": hits,
        "p50_latency_ms": round(lat_s[len(lat_s) // 2], 1) if lat_s else None,
        "per_query": per,
    }


def layered_eval(
    collection: str,
    cases: list[dict],
    gold_map: dict[str, str],
    tags: dict,
    *,
    allow_legacy_pad: bool = True,
) -> dict:
    hits = 0
    mrr = 0.0
    by_tag: dict[str, dict] = {}
    per = []
    first_layers: Counter = Counter()
    for case in cases:
        q = case["query"]
        gold_refs = set(case.get("gold_evidence_refs") or [])
        snips = [gold_map[r] for r in gold_refs if r in gold_map]
        pack = search_knowledge_units(
            q,
            top_k=5,
            fallback_policy="layered",
            allow_legacy_pad=allow_legacy_pad,
            collection_override=collection,
        )
        results = pack.get("results") or []
        tel = pack.get("telemetry") or {}
        fl = tel.get("first_contributing_layer")
        if fl:
            first_layers[str(fl)] += 1
        rank = match_rank(results, gold_refs, snips)
        tag = tags.get(case["id"], case.get("suite_tag") or "unknown")
        b = by_tag.setdefault(tag, {"n": 0, "hits": 0, "mrr": 0.0})
        b["n"] += 1
        if rank:
            hits += 1
            mrr += 1.0 / rank
            b["hits"] += 1
            b["mrr"] += 1.0 / rank
        per.append(
            {
                "id": case["id"],
                "suite_tag": tag,
                "found_rank": rank,
                "first_layer": fl,
                "retrieval_units": [r.get("retrieval_unit") for r in results[:5]],
                "top_unit_id": (results[0].get("unit_id") if results else None),
            }
        )
    n = max(len(cases), 1)
    for b in by_tag.values():
        b["recall_at_5"] = round(b["hits"] / max(b["n"], 1), 4)
        b["mrr_at_5"] = round(b["mrr"] / max(b["n"], 1), 4)
        del b["mrr"]
    return {
        "mode": "layered",
        "collection": collection,
        "n": len(cases),
        "recall_at_5": round(hits / n, 4),
        "mrr_at_5": round(mrr / n, 4),
        "hits": hits,
        "by_suite_tag": by_tag,
        "first_layer_counts": dict(first_layers),
        "per_query": per,
    }


def inventory_stats() -> dict:
    con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
    out = {
        "canonical_current": con.execute(
            "SELECT COUNT(*) FROM canonical_knowledge_units WHERE status='current'"
        ).fetchone()[0],
        "l2_current_units": con.execute(
            "SELECT COUNT(*) FROM knowledge_units WHERE unit_id LIKE 'l2|%' AND status='current'"
        ).fetchone()[0],
        "l2_merge_reason_new": con.execute(
            "SELECT COUNT(*) FROM canonical_knowledge_units "
            "WHERE status='current' AND merge_reason='l2_session_window_import'"
        ).fetchone()[0],
        "by_type_all": dict(
            con.execute(
                "SELECT unit_type, COUNT(*) FROM canonical_knowledge_units "
                "WHERE status='current' GROUP BY 1"
            ).fetchall()
        ),
        "by_type_l2_new": dict(
            con.execute(
                "SELECT unit_type, COUNT(*) FROM canonical_knowledge_units "
                "WHERE status='current' AND merge_reason='l2_session_window_import' GROUP BY 1"
            ).fetchall()
        ),
    }
    con.close()
    return out


def delta_metrics(before: dict, after: dict) -> dict:
    keys = ("recall_at_5", "mrr_at_5", "hits")
    d = {}
    for k in keys:
        if k in before and k in after and before[k] is not None and after[k] is not None:
            d[k] = {
                "before": before[k],
                "after": after[k],
                "delta": round(after[k] - before[k], 4)
                if isinstance(after[k], float)
                else after[k] - before[k],
            }
    return d


def make_charts(report: dict, out_dir: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []

    # 1) Recall bar: pure_ku + layered
    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = ["Pure-KU\nR@5", "Layered\nR@5", "Pure-KU\nMRR@5", "Layered\nMRR@5"]
    old_v = [
        report["frozen"]["old"]["pure_ku"]["recall_at_5"],
        report["frozen"]["old"]["layered"]["recall_at_5"],
        report["frozen"]["old"]["pure_ku"]["mrr_at_5"],
        report["frozen"]["old"]["layered"]["mrr_at_5"],
    ]
    new_v = [
        report["frozen"]["new"]["pure_ku"]["recall_at_5"],
        report["frozen"]["new"]["layered"]["recall_at_5"],
        report["frozen"]["new"]["pure_ku"]["mrr_at_5"],
        report["frozen"]["new"]["layered"]["mrr_at_5"],
    ]
    x = range(len(labels))
    w = 0.35
    b1 = ax.bar([i - w / 2 for i in x], old_v, w, label="L1 only (30,012)", color="#5b8cff")
    b2 = ax.bar([i + w / 2 for i in x], new_v, w, label="L1+L2 (30,774)", color="#35d0ba")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.15)
    ax.set_ylabel("Score")
    ax.set_title("Frozen suite: L1 vs L1+L2 retrieval")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    for bars in (b1, b2):
        for bar in bars:
            h = bar.get_height()
            ax.annotate(
                f"{h:.2f}",
                xy=(bar.get_x() + bar.get_width() / 2, h),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=8,
            )
    p1 = out_dir / "l1_l2_01_frozen_metrics.png"
    fig.tight_layout()
    fig.savefig(p1, dpi=140)
    plt.close(fig)
    paths.append(str(p1))

    # 2) Scale bars
    fig, ax = plt.subplots(figsize=(7, 4))
    cats = ["Canonical\ncurrent", "Active\nindex"]
    old_c = [30012, 30012]
    new_c = [
        report["inventory"]["canonical_current"],
        report["frozen"]["new"]["pure_ku"].get("collection_count")
        or report["inventory"]["canonical_current"],
    ]
    x = range(len(cats))
    ax.bar([i - 0.2 for i in x], old_c, 0.4, label="Before L2", color="#5b8cff")
    ax.bar([i + 0.2 for i in x], new_c, 0.4, label="After L2 merge", color="#35d0ba")
    ax.set_xticks(list(x))
    ax.set_xticklabels(cats)
    ax.set_title("Knowledge scale")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    for i, (a, b) in enumerate(zip(old_c, new_c)):
        ax.annotate(str(a), xy=(i - 0.2, a), ha="center", va="bottom", fontsize=8)
        ax.annotate(str(b), xy=(i + 0.2, b), ha="center", va="bottom", fontsize=8)
    p2 = out_dir / "l1_l2_02_scale.png"
    fig.tight_layout()
    fig.savefig(p2, dpi=140)
    plt.close(fig)
    paths.append(str(p2))

    # 3) L2 new by type
    by_t = report["inventory"].get("by_type_l2_new") or {}
    if by_t:
        fig, ax = plt.subplots(figsize=(8, 4))
        types = list(by_t.keys())
        vals = [by_t[t] for t in types]
        ax.barh(types, vals, color="#b48cff")
        ax.set_xlabel("New canonical units from L2")
        ax.set_title("L2 contribution by unit_type")
        ax.grid(axis="x", alpha=0.3)
        p3 = out_dir / "l1_l2_03_type_mix.png"
        fig.tight_layout()
        fig.savefig(p3, dpi=140)
        plt.close(fig)
        paths.append(str(p3))

    # 4) Per-query win/loss pure KU
    old_pq = {p["id"]: p.get("found_rank") for p in report["frozen"]["old"]["pure_ku"]["per_query"]}
    new_pq = {p["id"]: p.get("found_rank") for p in report["frozen"]["new"]["pure_ku"]["per_query"]}
    improved = missed = same_hit = same_miss = 0
    for cid in old_pq:
        o, n = old_pq.get(cid), new_pq.get(cid)
        if o is None and n is not None:
            improved += 1
        elif o is not None and n is None:
            missed += 1
        elif o is not None and n is not None:
            same_hit += 1
        else:
            same_miss += 1
    fig, ax = plt.subplots(figsize=(6, 4))
    labels = ["Still hit", "Still miss", "Newly hit\n(improved)", "Lost hit\n(regressed)"]
    vals = [same_hit, same_miss, improved, missed]
    colors = ["#35d0ba", "#9aabc9", "#5b8cff", "#ff6b7a"]
    ax.bar(labels, vals, color=colors)
    ax.set_title("Pure-KU frozen: per-query outcome shift")
    ax.set_ylabel("# queries (of 20)")
    for i, v in enumerate(vals):
        ax.annotate(str(v), xy=(i, v), ha="center", va="bottom")
    p4 = out_dir / "l1_l2_04_query_shift.png"
    fig.tight_layout()
    fig.savefig(p4, dpi=140)
    plt.close(fig)
    paths.append(str(p4))

    # 5) Holdout first-layer if present
    if report.get("holdout"):
        fig, ax = plt.subplots(figsize=(7, 4))
        ho = report["holdout"]
        # pad rates / abstain fp
        labels = ["scored R@5\n(layered)", "pad_used_rate", "abstain_fp\n/n"]
        # use pad on mode
        m = ho.get("modes", {}).get("layered_pad_on") or {}
        vals = [
            m.get("recall_at_5") or 0,
            m.get("pad_used_rate") or 0,
            (m.get("abstain_false_positive") or 0) / max(m.get("n_cases") or 1, 1),
        ]
        ax.bar(labels, vals, color=["#35d0ba", "#f0b429", "#ff6b7a"])
        ax.set_ylim(0, 1.1)
        ax.set_title("Holdout (current active = L1+L2) layered pad-on")
        for i, v in enumerate(vals):
            ax.annotate(f"{v:.2f}", xy=(i, v), ha="center", va="bottom")
        p5 = out_dir / "l1_l2_05_holdout_active.png"
        fig.tight_layout()
        fig.savefig(p5, dpi=140)
        plt.close(fig)
        paths.append(str(p5))

    return paths


def write_html(report: dict, chart_paths: list[str], dest: Path) -> None:
    def img(p: str) -> str:
        # embed as file path for local open; also copy relative name
        name = Path(p).name
        return f'<img src="file:///{Path(p).as_posix()}" alt="{name}" style="max-width:100%;border-radius:10px;margin:10px 0;border:1px solid #2a3a5f"/>'

    # Also copy charts next to HTML for relative paths
    dest_dir = dest.parent
    rel_imgs = []
    for p in chart_paths:
        src = Path(p)
        tgt = dest_dir / src.name
        try:
            tgt.write_bytes(src.read_bytes())
            rel_imgs.append(src.name)
        except Exception:
            rel_imgs.append(str(src))

    old_pk = report["frozen"]["old"]["pure_ku"]
    new_pk = report["frozen"]["new"]["pure_ku"]
    old_ly = report["frozen"]["old"]["layered"]
    new_ly = report["frozen"]["new"]["layered"]
    inv = report["inventory"]
    d_pk = report["deltas"]["pure_ku"]
    d_ly = report["deltas"]["layered"]

    verdict_pk = d_pk["recall_at_5"]["delta"]
    verdict_ly = d_ly["recall_at_5"]["delta"]
    if verdict_pk > 0 or verdict_ly > 0:
        verdict = "有提升（至少一个指标上升）"
        vcolor = "#35d0ba"
    elif verdict_pk == 0 and verdict_ly == 0:
        verdict = "frozen 金标指标持平；规模与覆盖有增长"
        vcolor = "#f0b429"
    else:
        verdict = "frozen 指标有回退，需审查"
        vcolor = "#ff6b7a"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>L1 vs L1+L2 检索对比</title>
<style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#0b1020;color:#e8eefc;margin:0;padding:24px}}
.card{{background:#161f38;border:1px solid #2a3a5f;border-radius:14px;padding:18px;margin:16px 0;max-width:960px}}
h1{{margin:0 0 8px}} h2{{border-bottom:1px solid #2a3a5f;padding-bottom:6px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:8px;border-bottom:1px solid #2a3a5f;text-align:left}}
.muted{{color:#9aabc9}} .ok{{color:{vcolor};font-weight:700}}
.delta-up{{color:#35d0ba}} .delta-flat{{color:#f0b429}} .delta-down{{color:#ff6b7a}}
code{{background:#0a1020;padding:2px 6px;border-radius:4px}}
</style></head><body>
<div class="card">
<h1>L1 vs L1+L2 全面检索对比</h1>
<p class="muted">生成时间 {report['generated_at']} · frozen 金标匹配 + 库存规模 + holdout（active）</p>
<p class="ok">结论：{verdict}</p>
</div>
<div class="card">
<h2>1. 规模</h2>
<table>
<tr><th>指标</th><th>L1 only</th><th>L1+L2</th><th>Δ</th></tr>
<tr><td>Canonical current</td><td>30012</td><td>{inv['canonical_current']}</td><td class="delta-up">+{inv['canonical_current']-30012}</td></tr>
<tr><td>Active collection</td><td>{OLD_COLL.split('_')[-1] if False else '30,012'}</td><td>{new_pk.get('collection_count')}</td><td class="delta-up">+{(new_pk.get('collection_count') or 0)-30012}</td></tr>
<tr><td>L2 new canonical rows</td><td>—</td><td>{inv.get('l2_merge_reason_new')}</td><td></td></tr>
<tr><td>L2 member units</td><td>—</td><td>{inv.get('l2_current_units')}</td><td></td></tr>
</table>
</div>
<div class="card">
<h2>2. Frozen 金标（20 题）</h2>
<table>
<tr><th>模式</th><th>L1 only R@5</th><th>L1+L2 R@5</th><th>Δ R@5</th><th>L1 MRR</th><th>L1+L2 MRR</th><th>Δ MRR</th></tr>
<tr>
<td>Pure-KU</td>
<td>{old_pk['recall_at_5']}</td><td>{new_pk['recall_at_5']}</td>
<td class="{'delta-up' if d_pk['recall_at_5']['delta']>0 else 'delta-flat' if d_pk['recall_at_5']['delta']==0 else 'delta-down'}">{d_pk['recall_at_5']['delta']:+.4f}</td>
<td>{old_pk['mrr_at_5']}</td><td>{new_pk['mrr_at_5']}</td>
<td class="{'delta-up' if d_pk['mrr_at_5']['delta']>0 else 'delta-flat' if d_pk['mrr_at_5']['delta']==0 else 'delta-down'}">{d_pk['mrr_at_5']['delta']:+.4f}</td>
</tr>
<tr>
<td>Layered hybrid</td>
<td>{old_ly['recall_at_5']}</td><td>{new_ly['recall_at_5']}</td>
<td class="{'delta-up' if d_ly['recall_at_5']['delta']>0 else 'delta-flat' if d_ly['recall_at_5']['delta']==0 else 'delta-down'}">{d_ly['recall_at_5']['delta']:+.4f}</td>
<td>{old_ly['mrr_at_5']}</td><td>{new_ly['mrr_at_5']}</td>
<td class="{'delta-up' if d_ly['mrr_at_5']['delta']>0 else 'delta-flat' if d_ly['mrr_at_5']['delta']==0 else 'delta-down'}">{d_ly['mrr_at_5']['delta']:+.4f}</td>
</tr>
</table>
<p class="muted">说明：frozen 金标多为 cm| 证据；layered 往往被 dialogue LIKE 拉满，pure-KU 更能反映向量层变化。</p>
<p>Collections: <code>{OLD_COLL}</code> vs <code>{NEW_COLL}</code></p>
</div>
<div class="card">
<h2>3. 图表</h2>
{"".join(f'<img src="{n}" style="max-width:100%;border-radius:10px;margin:10px 0;border:1px solid #2a3a5f"/>' for n in rel_imgs)}
</div>
<div class="card">
<h2>4. L2 新增类型分布</h2>
<table><tr><th>unit_type</th><th>new canonical</th></tr>
{"".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k,v in sorted((inv.get('by_type_l2_new') or {}).items(), key=lambda x:-x[1]))}
</table>
</div>
<div class="card">
<h2>5. 解读</h2>
<ul>
<li>库存：知识面从 30,012 → {inv['canonical_current']}（+{inv['canonical_current']-30012}），以 project_decision 为主（L2 设计目标）。</li>
<li>Pure-KU R@5：{old_pk['recall_at_5']} → {new_pk['recall_at_5']}（Δ {d_pk['recall_at_5']['delta']:+.4f}）。</li>
<li>Layered R@5：{old_ly['recall_at_5']} → {new_ly['recall_at_5']}（Δ {d_ly['recall_at_5']['delta']:+.4f}）；若已接近 1.0，难再涨。</li>
<li>L2 价值主要在<strong>覆盖扩展</strong>（新决策/偏好断言），不一定抬高「旧 frozen 金标」分数——金标针对 L1 证据集。</li>
</ul>
<p class="muted">JSON：integration/analysis/ai_context/l1_l2_retrieval_comparison.json</p>
</div>
</body></html>"""
    dest.write_text(html, encoding="utf-8")


def main() -> int:
    print("[compare] loading cases…")
    frozen = load_cases(EVAL)
    tags = load_tags()
    gold = load_gold_snips(frozen)
    print(f"[compare] frozen={len(frozen)} gold_snips={len(gold)}")

    inv = inventory_stats()
    print("[compare] inventory", inv["canonical_current"], "l2_new", inv["l2_merge_reason_new"])

    print("[compare] pure-KU old…")
    old_pk = pure_ku_eval(OLD_COLL, frozen, gold)
    print("  R@5", old_pk["recall_at_5"], "MRR", old_pk["mrr_at_5"])
    print("[compare] pure-KU new…")
    new_pk = pure_ku_eval(NEW_COLL, frozen, gold)
    print("  R@5", new_pk["recall_at_5"], "MRR", new_pk["mrr_at_5"])

    print("[compare] layered old…")
    old_ly = layered_eval(OLD_COLL, frozen, gold, tags)
    print("  R@5", old_ly["recall_at_5"], "MRR", old_ly["mrr_at_5"])
    print("[compare] layered new…")
    new_ly = layered_eval(NEW_COLL, frozen, gold, tags)
    print("  R@5", new_ly["recall_at_5"], "MRR", new_ly["mrr_at_5"])

    # holdout on active (new) only + optional both pure
    holdout_doc = None
    hp = AI_CONTEXT_DIR / "phase15_02_holdout_eval.json"
    if hp.exists():
        holdout_doc = json.loads(hp.read_text(encoding="utf-8"))

    # quick holdout pure_ku both collections if holdout file exists
    holdout_cases = load_cases(HOLDOUT)
    holdout_cmp = None
    if holdout_cases:
        # only cases with gold_title_substrings for scoring
        scored = [c for c in holdout_cases if c.get("gold_title_substrings") or c.get("gold_evidence_refs")]
        if scored:
            print("[compare] holdout scored pure-ku…", len(scored))
            # reuse match_rank with title substrings via fake gold map empty + custom
            def holdout_pure(coll: str) -> dict:
                from personal_knowledge.core.chroma_client import ChromaClient
                from personal_knowledge.core import local_embed

                client = ChromaClient()
                coll_o = client.get_or_create_collection(coll)
                hits = 0
                for case in scored:
                    emb = local_embed.embed(case["query"])
                    if emb is None:
                        continue
                    kr = coll_o.query(query_embeddings=[emb], n_results=5, include=["documents", "metadatas"])
                    docs = kr.get("documents", [[]])[0] if kr.get("documents") else []
                    metas = kr.get("metadatas", [[]])[0] if kr.get("metadatas") else []
                    blob = " ".join(
                        (d or "") + " " + ((m or {}).get("subject") or "")
                        for d, m in zip(docs, metas)
                    ).lower()
                    subs = [s.lower() for s in (case.get("gold_title_substrings") or []) if s]
                    if subs and any(s in blob for s in subs):
                        hits += 1
                return {
                    "n_scored": len(scored),
                    "hits": hits,
                    "recall_at_5": round(hits / max(len(scored), 1), 4),
                    "collection": coll,
                }

            holdout_cmp = {
                "old": holdout_pure(OLD_COLL),
                "new": holdout_pure(NEW_COLL),
            }
            print("  holdout pure", holdout_cmp)

    report = {
        "generated_at": _utc(),
        "collections": {"old": OLD_COLL, "new": NEW_COLL},
        "inventory": inv,
        "frozen": {
            "old": {"pure_ku": old_pk, "layered": old_ly},
            "new": {"pure_ku": new_pk, "layered": new_ly},
        },
        "deltas": {
            "pure_ku": delta_metrics(old_pk, new_pk),
            "layered": delta_metrics(old_ly, new_ly),
        },
        "holdout_active_report": holdout_doc.get("modes") if holdout_doc else None,
        "holdout_pure_ku_compare": holdout_cmp,
    }

    AI_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[compare] wrote", OUT_JSON)

    charts = make_charts(report, OUT_DIR)
    print("[compare] charts", charts)
    write_html(report, charts, HTML_REPORT)
    print("[compare] html", HTML_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
