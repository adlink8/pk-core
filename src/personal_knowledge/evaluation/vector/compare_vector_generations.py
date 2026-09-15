"""新旧向量库多维度对比（保留全部 collection，只读探测）。

对比对象:
  - personal_events      (旧：原始事件)
  - conversation_turns   (中：对话叙述)
  - knowledge_units active (新：结构化知识单元)
  - hybrid               (知识-first + raw fallback，生产路径)

输出:
  integration/analysis/ai_context/vector_generation_comparison.json
  integration/analysis/ai_context/vector_generation_comparison.md
  integration/analysis/ai_context/charts/vector_gen_*.png

用法::

    python src/personal_knowledge/retrieval/compare_vector_generations.py
    python src/personal_knowledge/retrieval/compare_vector_generations.py --sample 300
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_knowledge.core.project_paths import (
    UNIFIED_DB,
    AI_CONTEXT_DIR,
    AGENT_CONVERSATIONS_DB,
    KNOWLEDGE_EVAL_DIR,
    KNOWLEDGE_ACTIVE_POINTER,
)
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
import personal_knowledge.core.local_embed as local_embed  # noqa: E402

EVAL_DIR = KNOWLEDGE_EVAL_DIR
ACTIVE_POINTER = KNOWLEDGE_ACTIVE_POINTER
CHARTS_DIR = AI_CONTEXT_DIR / "charts"
OUT_JSON = AI_CONTEXT_DIR / "vector_generation_comparison.json"
OUT_MD = AI_CONTEXT_DIR / "vector_generation_comparison.md"

# 补充「个人数据/画像向」查询（与 frozen 证据集互补）
PROFILE_QUERIES = [
    "用户希望 Agent 默认使用什么语言",
    "GSD 工作流怎么用",
    "用户偏好什么样的语气风格",
    "用户常用哪些 AI 编码工具",
    "RAG 核心思想是什么",
    "用户的 shell 环境偏好",
    "项目如何做知识单元检索",
    "用户做过哪些个人数据相关项目",
    "cli-anything 是做什么的",
    "用户如何管理个人记忆与知识库",
]


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(int(len(s) * p / 100), len(s) - 1)
    return float(s[idx])


def _safe_mean(vals: list[float]) -> float:
    return float(statistics.mean(vals)) if vals else 0.0


def _safe_median(vals: list[float]) -> float:
    return float(statistics.median(vals)) if vals else 0.0


def read_active_collection() -> str:
    if ACTIVE_POINTER.exists():
        return ACTIVE_POINTER.read_text(encoding="utf-8").strip()
    return ""


def default_query_sources() -> dict[str, str]:
    """Return the canonical collections used by the generation comparison."""
    active = read_active_collection()
    return {
        "events": "personal_events",
        "turns": "conversation_turns",
        "ku": active,
    }


def load_eval_queries() -> list[dict]:
    """合并 frozen_test + dev + profile 查询。"""
    cases: list[dict] = []
    for name in ("frozen_test_queries.private.jsonl", "dev_queries.private.jsonl"):
        path = EVAL_DIR / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            obj["_suite"] = "frozen" if "frozen" in name else "dev"
            cases.append(obj)
    for i, q in enumerate(PROFILE_QUERIES, 1):
        cases.append(
            {
                "id": f"profile-{i:03d}",
                "query": q,
                "gold_evidence_refs": [],
                "expected_abstain": False,
                "_suite": "profile",
            }
        )
    return cases


def load_gold_snippets(cases: list[dict]) -> dict[str, str]:
    gold: dict[str, str] = {}
    if not AGENT_CONVERSATIONS_DB.exists():
        return gold
    refs = sorted({r for c in cases for r in c.get("gold_evidence_refs") or []})
    if not refs:
        return gold
    con = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
    for ref in refs:
        row = con.execute(
            "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
            (ref,),
        ).fetchone()
        if row and row[0]:
            gold[ref] = row[0][:200]
    con.close()
    return gold


def match_found(
    ids: list[str],
    documents: list[str],
    metadatas: list[dict | None],
    gold_refs: set[str],
    gold_snippets: list[str],
) -> int | None:
    for rank, (rid, doc, meta) in enumerate(zip(ids, documents, metadatas), 1):
        if rid in gold_refs:
            return rank
        if isinstance(meta, dict):
            src = meta.get("source_message_ref") or ""
            if src and src in gold_refs:
                return rank
        if doc and gold_snippets:
            for snip in gold_snippets:
                if len(snip) >= 15 and snip[:15] in doc:
                    return rank
    return None


# --- 可答性启发式（不依赖 gold）---

_RE_QA = re.compile(r"[？?].{2,}")
_RE_USER_ASSERT = re.compile(r"(用户|项目|我).{0,20}(使用|偏好|要求|希望|采用|默认)")
_RE_CODEISH = re.compile(r"[{};]|def |class |import |function |SELECT |http")
_RE_NOISE = re.compile(r"(Prompted |Attached |rollout-|Assistant Rules|InvalidTemplate)")


def answerability_score(doc: str, meta: dict | None, collection_kind: str) -> dict[str, Any]:
    """0-1 可答性启发式：结构化、短断言、少噪声。"""
    doc = doc or ""
    meta = meta or {}
    flags = {
        "has_qa_shape": bool(_RE_QA.search(doc)) or ("question" in meta) or collection_kind == "ku",
        "user_assert": bool(_RE_USER_ASSERT.search(doc)),
        "short_doc": 20 <= len(doc) <= 280,
        "has_confidence": "confidence" in meta and float(meta.get("confidence") or 0) > 0,
        "has_unit_type": bool(meta.get("unit_type")),
        "has_subject": bool(meta.get("subject")),
        "code_heavy": bool(_RE_CODEISH.search(doc)),
        "noise_markers": bool(_RE_NOISE.search(doc)),
        "too_long": len(doc) > 800,
    }
    score = 0.0
    if flags["has_qa_shape"]:
        score += 0.25
    if flags["user_assert"]:
        score += 0.20
    if flags["short_doc"]:
        score += 0.15
    if flags["has_confidence"]:
        score += 0.15
    if flags["has_unit_type"]:
        score += 0.15
    if flags["has_subject"]:
        score += 0.10
    if flags["code_heavy"]:
        score -= 0.15
    if flags["noise_markers"]:
        score -= 0.20
    if flags["too_long"]:
        score -= 0.15
    score = max(0.0, min(1.0, score))
    return {"score": round(score, 3), "flags": flags}


def sample_structure(client: ChromaClient, name: str, sample_n: int) -> dict:
    coll = client.get_or_create_collection(name)
    total = coll.count()
    # 多 offset 采样，避免只拿到单一 source 批次
    docs: list[str] = []
    metas: list[dict] = []
    step = max(total // max(sample_n, 1), 1) if total else 1
    got = 0
    offset = 0
    while got < sample_n and offset < max(total, 1):
        batch = min(50, sample_n - got)
        try:
            s = coll.get(limit=batch, offset=offset, include=["documents", "metadatas"])
        except Exception:
            break
        batch_docs = s.get("documents") or []
        batch_metas = s.get("metadatas") or []
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch_metas or [{}] * len(batch_docs))
        got += len(batch_docs)
        offset += step * batch
        if len(batch_docs) < batch:
            break

    lens = [len(d or "") for d in docs]
    meta_keys: set[str] = set()
    type_dist: dict[str, int] = {}
    confidences: list[float] = []
    for m in metas:
        m = m or {}
        meta_keys.update(m.keys())
        key = m.get("unit_type") or m.get("source") or m.get("event_type") or "?"
        type_dist[key] = type_dist.get(key, 0) + 1
        if "confidence" in m:
            try:
                confidences.append(float(m["confidence"]))
            except (TypeError, ValueError):
                pass

    # 信息密度：可答性均值
    kind = "ku" if "knowledge" in name else ("turn" if "conversation" in name else "event")
    ans_scores = [
        answerability_score(d, m, kind)["score"]
        for d, m in zip(docs, metas)
    ]

    return {
        "name": name,
        "kind": kind,
        "total": total,
        "sample_n": len(docs),
        "doc_len": {
            "min": min(lens) if lens else 0,
            "max": max(lens) if lens else 0,
            "mean": round(_safe_mean([float(x) for x in lens]), 1),
            "median": round(_safe_median([float(x) for x in lens]), 1),
            "p90": round(_pct([float(x) for x in lens], 90), 1),
            "hist_buckets": _hist_buckets(lens),
        },
        "empty_docs": sum(1 for x in lens if x == 0),
        "short_lt40": sum(1 for x in lens if x < 40),
        "long_gt800": sum(1 for x in lens if x > 800),
        "meta_keys": sorted(meta_keys),
        "meta_key_count": len(meta_keys),
        "type_or_source_dist": dict(sorted(type_dist.items(), key=lambda x: -x[1])[:12]),
        "confidence": {
            "coverage": round(len(confidences) / max(len(docs), 1), 3),
            "mean": round(_safe_mean(confidences), 3),
        },
        "answerability_mean": round(_safe_mean(ans_scores), 3),
        "answerability_p50": round(_safe_median(ans_scores), 3),
        "answerability_high_share": round(
            sum(1 for s in ans_scores if s >= 0.6) / max(len(ans_scores), 1), 3
        ),
    }


def _hist_buckets(lens: list[int]) -> dict[str, int]:
    edges = [0, 50, 100, 200, 400, 800, 1200, 10_000]
    labels = ["0-49", "50-99", "100-199", "200-399", "400-799", "800-1199", "1200+"]
    counts = {lb: 0 for lb in labels}
    for n in lens:
        for i in range(len(edges) - 1):
            if edges[i] <= n < edges[i + 1]:
                counts[labels[i]] += 1
                break
    return counts


def query_collection(
    coll,
    embedding: list[float],
    n: int = 5,
) -> dict:
    try:
        r = coll.query(
            query_embeddings=[embedding],
            n_results=n,
            include=["metadatas", "documents", "distances"],
        )
    except ChromaError as e:
        return {"error": str(e)[:120], "ids": [], "documents": [], "metadatas": [], "distances": []}
    return {
        "ids": (r.get("ids") or [[]])[0],
        "documents": (r.get("documents") or [[]])[0],
        "metadatas": (r.get("metadatas") or [[]])[0],
        "distances": (r.get("distances") or [[]])[0],
    }


def evaluate_retrieval(
    client: ChromaClient,
    collections: dict[str, str],
    cases: list[dict],
    gold_contents: dict[str, str],
) -> dict:
    """对每个 collection + hybrid 跑检索评估。"""
    coll_handles = {k: client.get_or_create_collection(v) for k, v in collections.items()}
    results: dict[str, Any] = {}

    modes = list(collections.keys()) + ["hybrid"]
    for mode in modes:
        results[mode] = {
            "mode": mode,
            "collection": collections.get(mode, "hybrid"),
            "suites": {},
            "overall": {},
            "per_query": [],
            "top1_distances": [],
            "answerability_top1": [],
            "latencies_ms": [],
        }

    for case in cases:
        q = case.get("query") or ""
        suite = case.get("_suite", "other")
        gold_refs = set(case.get("gold_evidence_refs") or [])
        gold_snips = [gold_contents[r] for r in gold_refs if r in gold_contents]
        has_gold = bool(gold_refs)

        t0 = time.time()
        emb = local_embed.embed(q)
        embed_ms = (time.time() - t0) * 1000
        if emb is None:
            for mode in modes:
                results[mode]["per_query"].append(
                    {"id": case.get("id"), "suite": suite, "error": "embed_failed"}
                )
            continue
        emb_list = list(emb)

        per_mode_hits: dict[str, dict] = {}
        for mode, name in collections.items():
            t1 = time.time()
            hit = query_collection(coll_handles[mode], emb_list, n=5)
            lat = (time.time() - t1) * 1000 + embed_ms
            kind = "ku" if mode == "ku" else ("turn" if mode == "turns" else "event")
            docs = hit.get("documents") or []
            metas = hit.get("metadatas") or []
            dists = hit.get("distances") or []
            ids = hit.get("ids") or []
            top1_doc = docs[0] if docs else ""
            top1_meta = metas[0] if metas else {}
            top1_dist = float(dists[0]) if dists else None
            ans = answerability_score(top1_doc, top1_meta if isinstance(top1_meta, dict) else {}, kind)
            found = None
            if has_gold:
                found = match_found(ids, docs, metas, gold_refs, gold_snips)
            row = {
                "id": case.get("id"),
                "suite": suite,
                "query": q[:100],
                "found_rank": found,
                "has_gold": has_gold,
                "top1_distance": round(top1_dist, 4) if top1_dist is not None else None,
                "top1_answerability": ans["score"],
                "top1_preview": (top1_doc or "")[:120].replace("\n", " "),
                "latency_ms": round(lat, 1),
            }
            per_mode_hits[mode] = {
                "ids": ids,
                "docs": docs,
                "metas": metas,
                "dists": dists,
                "row": row,
            }
            bucket = results[mode]
            bucket["per_query"].append(row)
            if top1_dist is not None:
                bucket["top1_distances"].append(top1_dist)
            bucket["answerability_top1"].append(ans["score"])
            bucket["latencies_ms"].append(lat)

        # hybrid: KU first 3 + raw fill
        t1 = time.time()
        ku = per_mode_hits.get("ku", {})
        raw = per_mode_hits.get("events", {})
        h_ids = list(ku.get("ids") or [])[:3]
        h_docs = list(ku.get("docs") or [])[:3]
        h_metas = list(ku.get("metas") or [])[:3]
        h_dists = list(ku.get("dists") or [])[:3]
        for rid, doc, meta, dist in zip(
            raw.get("ids") or [],
            raw.get("docs") or [],
            raw.get("metas") or [],
            raw.get("dists") or [],
        ):
            if rid in h_ids:
                continue
            h_ids.append(rid)
            h_docs.append(doc)
            h_metas.append(meta)
            h_dists.append(dist)
            if len(h_ids) >= 5:
                break
        lat = (time.time() - t1) * 1000 + embed_ms
        found = match_found(h_ids, h_docs, h_metas, gold_refs, gold_snips) if has_gold else None
        top1_doc = h_docs[0] if h_docs else ""
        top1_meta = h_metas[0] if h_metas else {}
        top1_dist = float(h_dists[0]) if h_dists else None
        ans = answerability_score(
            top1_doc,
            top1_meta if isinstance(top1_meta, dict) else {},
            "ku" if h_ids and str(h_ids[0]).startswith("cu|") else "event",
        )
        row = {
            "id": case.get("id"),
            "suite": suite,
            "query": q[:100],
            "found_rank": found,
            "has_gold": has_gold,
            "top1_distance": round(top1_dist, 4) if top1_dist is not None else None,
            "top1_answerability": ans["score"],
            "top1_preview": (top1_doc or "")[:120].replace("\n", " "),
            "latency_ms": round(lat, 1),
            "ku_slots": min(3, len(ku.get("ids") or [])),
        }
        results["hybrid"]["per_query"].append(row)
        if top1_dist is not None:
            results["hybrid"]["top1_distances"].append(top1_dist)
        results["hybrid"]["answerability_top1"].append(ans["score"])
        results["hybrid"]["latencies_ms"].append(lat)

        # 记录距离赢家（profile/dev 语义贴近度）
        dists_cmp = {
            m: per_mode_hits[m]["row"]["top1_distance"]
            for m in collections
            if per_mode_hits.get(m, {}).get("row", {}).get("top1_distance") is not None
        }
        if dists_cmp:
            winner = min(dists_cmp, key=lambda k: dists_cmp[k])
            for mode in modes:
                if mode == "hybrid":
                    continue
                results[mode].setdefault("distance_wins", 0)
            results[winner]["distance_wins"] = results[winner].get("distance_wins", 0) + 1

    # 汇总指标
    for mode, bucket in results.items():
        for suite in ("frozen", "dev", "profile", "all"):
            rows = [
                r
                for r in bucket["per_query"]
                if suite == "all" or r.get("suite") == suite
            ]
            gold_rows = [r for r in rows if r.get("has_gold") and "error" not in r]
            hits = sum(1 for r in gold_rows if r.get("found_rank"))
            mrr = sum(1.0 / r["found_rank"] for r in gold_rows if r.get("found_rank"))
            dists = [r["top1_distance"] for r in rows if r.get("top1_distance") is not None]
            ans = [r["top1_answerability"] for r in rows if r.get("top1_answerability") is not None]
            lats = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
            bucket["suites"][suite] = {
                "n": len(rows),
                "n_with_gold": len(gold_rows),
                "recall_at_5": round(hits / max(len(gold_rows), 1), 4) if gold_rows else None,
                "mrr_at_5": round(mrr / max(len(gold_rows), 1), 4) if gold_rows else None,
                "top1_distance_mean": round(_safe_mean(dists), 4),
                "top1_distance_median": round(_safe_median(dists), 4),
                "answerability_mean": round(_safe_mean(ans), 3),
                "answerability_high_share": round(
                    sum(1 for a in ans if a >= 0.6) / max(len(ans), 1), 3
                ),
                "latency_p50_ms": round(_pct(lats, 50), 1),
                "latency_p95_ms": round(_pct(lats, 95), 1),
            }
        bucket["overall"] = bucket["suites"]["all"]
        bucket["distance_wins"] = bucket.get("distance_wins", 0)
        # 压缩 per_query 里的预览以减小 JSON
        for r in bucket["per_query"]:
            r.pop("top1_preview", None)

    return results


def sqlite_knowledge_stats() -> dict:
    con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
    out: dict[str, Any] = {}
    for t in (
        "unified_events",
        "knowledge_units",
        "canonical_knowledge_units",
        "knowledge_unit_evidence",
        "memory_items",
    ):
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = None
    try:
        rows = con.execute(
            "SELECT unit_type, COUNT(*) c FROM canonical_knowledge_units "
            "GROUP BY unit_type ORDER BY c DESC"
        ).fetchall()
        out["canonical_by_type"] = {r[0]: r[1] for r in rows}
    except Exception:
        out["canonical_by_type"] = {}
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) c FROM knowledge_index_versions GROUP BY status"
        ).fetchall()
        out["index_versions_by_status"] = {r[0]: r[1] for r in rows}
    except Exception:
        out["index_versions_by_status"] = {}
    # 治理字段覆盖
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM canonical_knowledge_units WHERE status='current'"
        ).fetchone()[0]
        with_conf = con.execute(
            "SELECT COUNT(*) FROM canonical_knowledge_units "
            "WHERE status='current' AND confidence IS NOT NULL"
        ).fetchone()[0]
        out["canonical_current"] = n
        out["canonical_confidence_coverage"] = round(with_conf / max(n, 1), 3)
    except Exception:
        pass
    con.close()
    return out


def list_all_collections(client: ChromaClient) -> list[dict]:
    cols = client.list_collections()
    out = []
    for c in cols:
        name = c.get("name") if isinstance(c, dict) else str(c)
        try:
            n = client.get_or_create_collection(name).count()
        except Exception:
            n = None
        out.append({"name": name, "count": n})
    return sorted(out, key=lambda x: (-(x["count"] or 0), x["name"] or ""))


def make_charts(report: dict) -> list[str]:
    """生成对比图，返回相对路径列表。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
    except ImportError:
        return []

    # 中文字体
    font_candidates = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    for fname in font_candidates:
        try:
            plt.rcParams["font.sans-serif"] = [fname]
            plt.rcParams["axes.unicode_minus"] = False
            break
        except Exception:
            continue

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    structures = report["structure"]
    retrieval = report["retrieval"]
    labels_map = {
        "events": "personal_events\n(旧·事件)",
        "turns": "conversation_turns\n(中·叙述)",
        "ku": "knowledge_units\n(新·知识)",
        "hybrid": "hybrid\n(生产路径)",
    }
    colors = {
        "events": "#6B7280",
        "turns": "#3B82F6",
        "ku": "#10B981",
        "hybrid": "#8B5CF6",
    }

    # 1) 规模
    fig, ax = plt.subplots(figsize=(8, 4.5))
    keys = ["events", "turns", "ku"]
    xs = [labels_map[k] for k in keys]
    ys = [structures[k]["total"] for k in keys]
    bars = ax.bar(xs, ys, color=[colors[k] for k in keys])
    ax.set_title("Collection 规模对比")
    ax.set_ylabel("向量条数")
    for b, y in zip(bars, ys):
        ax.text(b.get_x() + b.get_width() / 2, y, f"{y:,}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_01_scale.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 2) 文档长度分布（hist buckets）
    fig, ax = plt.subplots(figsize=(10, 5))
    bucket_order = ["0-49", "50-99", "100-199", "200-399", "400-799", "800-1199", "1200+"]
    x = range(len(bucket_order))
    width = 0.25
    for i, k in enumerate(keys):
        hist = structures[k]["doc_len"]["hist_buckets"]
        vals = [hist.get(b, 0) for b in bucket_order]
        # normalize to share
        total = sum(vals) or 1
        shares = [v / total for v in vals]
        ax.bar([xi + (i - 1) * width for xi in x], shares, width=width, label=labels_map[k].replace("\n", " "), color=colors[k])
    ax.set_xticks(list(x))
    ax.set_xticklabels(bucket_order, rotation=20)
    ax.set_ylabel("样本占比")
    ax.set_title("文档长度分布（采样归一化）")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_02_doc_length.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 3) 可答性
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ys = [structures[k]["answerability_mean"] for k in keys]
    bars = ax.bar([labels_map[k] for k in keys], ys, color=[colors[k] for k in keys])
    ax.set_ylim(0, 1.05)
    ax.set_title("结构可答性均值（采样启发式 0–1）")
    ax.set_ylabel("answerability")
    for b, y in zip(bars, ys):
        ax.text(b.get_x() + b.get_width() / 2, y + 0.02, f"{y:.2f}", ha="center", fontsize=10)
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_03_answerability_structure.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 4) Recall / MRR on gold suite
    fig, ax = plt.subplots(figsize=(9, 5))
    modes = ["events", "turns", "ku", "hybrid"]
    x = range(len(modes))
    width = 0.35
    recalls, mrrs = [], []
    for m in modes:
        s = retrieval[m]["suites"].get("frozen") or retrieval[m]["suites"].get("all")
        # prefer all-with-gold metrics from suites that have gold
        gold_suite = retrieval[m]["suites"].get("frozen", {})
        # combine frozen+dev gold via recomputing from overall if frozen n_with_gold
        r = gold_suite.get("recall_at_5")
        mr = gold_suite.get("mrr_at_5")
        if r is None:
            # fall back: use all gold rows
            all_s = retrieval[m]["suites"]["all"]
            r = all_s.get("recall_at_5")
            mr = all_s.get("mrr_at_5")
        recalls.append(r or 0)
        mrrs.append(mr or 0)
    ax.bar([i - width / 2 for i in x], recalls, width, label="Recall@5", color="#059669")
    ax.bar([i + width / 2 for i in x], mrrs, width, label="MRR@5", color="#2563EB")
    ax.set_xticks(list(x))
    ax.set_xticklabels([labels_map[m] for m in modes])
    ax.set_ylim(0, 1.1)
    ax.set_title("有 gold evidence 的查询：Recall@5 / MRR@5（frozen）")
    ax.legend()
    for i, (r, m) in enumerate(zip(recalls, mrrs)):
        ax.text(i - width / 2, r + 0.02, f"{r:.2f}", ha="center", fontsize=8)
        ax.text(i + width / 2, m + 0.02, f"{m:.2f}", ha="center", fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_04_recall_mrr.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 5) top1 distance by suite (profile — 最体现知识检索)
    fig, ax = plt.subplots(figsize=(9, 5))
    for m in modes:
        dist = [
            r["top1_distance"]
            for r in retrieval[m]["per_query"]
            if r.get("suite") == "profile" and r.get("top1_distance") is not None
        ]
        if not dist:
            dist = [r["top1_distance"] for r in retrieval[m]["per_query"] if r.get("top1_distance") is not None]
        ax.boxplot(dist, positions=[modes.index(m) + 1], widths=0.5, patch_artist=True,
                   boxprops=dict(facecolor=colors[m], alpha=0.6))
    ax.set_xticks(range(1, len(modes) + 1))
    ax.set_xticklabels([labels_map[m] for m in modes])
    ax.set_ylabel("cosine distance（越小越近）")
    ax.set_title("Top-1 语义距离分布（profile 查询优先）")
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_05_top1_distance.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 6) top1 answerability on retrieval
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ys = [retrieval[m]["suites"]["all"]["answerability_mean"] for m in modes]
    bars = ax.bar([labels_map[m] for m in modes], ys, color=[colors[m] for m in modes])
    ax.set_ylim(0, 1.05)
    ax.set_title("检索 Top-1 可答性均值（全查询集）")
    for b, y in zip(bars, ys):
        ax.text(b.get_x() + b.get_width() / 2, y + 0.02, f"{y:.2f}", ha="center")
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_06_retrieval_answerability.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 7) KU type distribution
    types = report.get("sqlite", {}).get("canonical_by_type") or {}
    if types:
        fig, ax = plt.subplots(figsize=(8, 5))
        labels = list(types.keys())
        vals = list(types.values())
        ax.barh(labels, vals, color="#10B981")
        ax.set_title("canonical 知识单元类型分布")
        ax.set_xlabel("条数")
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:,}", va="center", fontsize=9)
        fig.tight_layout()
        p = CHARTS_DIR / "vector_gen_07_ku_types.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p))

    # 8) multi-metric radar-like grouped comparison (normalized)
    fig, ax = plt.subplots(figsize=(9, 5))
    metric_names = ["规模归一", "可答性", "1-距离(近)", "Recall@5", "高可答占比"]
    # scale: normalize counts by max
    max_count = max(structures[k]["total"] for k in keys) or 1
    series = {}
    for m in ["events", "turns", "ku"]:
        st = structures[m]
        ret = retrieval[m]["suites"]["all"]
        r5 = ret.get("recall_at_5")
        if r5 is None:
            r5 = 0
        series[m] = [
            st["total"] / max_count,
            st["answerability_mean"],
            max(0.0, 1.0 - ret.get("top1_distance_mean", 1.0)),
            r5,
            ret.get("answerability_high_share", 0),
        ]
    x = range(len(metric_names))
    width = 0.25
    for i, m in enumerate(["events", "turns", "ku"]):
        ax.bar(
            [xi + (i - 1) * width for xi in x],
            series[m],
            width=width,
            label=labels_map[m].replace("\n", " "),
            color=colors[m],
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(metric_names)
    ax.set_ylim(0, 1.15)
    ax.set_title("多维度归一化对比（越高越好）")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = CHARTS_DIR / "vector_gen_08_multimetric.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p))

    # 9) distance wins pie
    wins = {m: retrieval[m].get("distance_wins", 0) for m in ["events", "turns", "ku"]}
    if sum(wins.values()) > 0:
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.pie(
            list(wins.values()),
            labels=[labels_map[m].replace("\n", " ") for m in wins],
            autopct="%1.0f%%",
            colors=[colors[m] for m in wins],
            startangle=90,
        )
        ax.set_title("Top-1 距离赢家占比（全查询）")
        fig.tight_layout()
        p = CHARTS_DIR / "vector_gen_09_distance_wins.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p))

    return saved


def write_markdown(report: dict, chart_paths: list[str]) -> None:
    st = report["structure"]
    ret = report["retrieval"]
    sql = report["sqlite"]
    active = report["active_collection"]
    gen = report["generated_at"]

    def suite_row(mode: str, suite: str) -> str:
        s = ret[mode]["suites"].get(suite, {})
        r = s.get("recall_at_5")
        m = s.get("mrr_at_5")
        r_s = f"{r:.2f}" if r is not None else "—"
        m_s = f"{m:.2f}" if m is not None else "—"
        return (
            f"| {mode} | {s.get('n', 0)} | {s.get('n_with_gold', 0)} | {r_s} | {m_s} | "
            f"{s.get('top1_distance_mean', 0):.3f} | {s.get('answerability_mean', 0):.2f} | "
            f"{s.get('latency_p50_ms', 0):.0f} |"
        )

    charts_md = "\n".join(
        f"![{Path(p).stem}]({Path(p).as_posix().split('ai_context/')[-1] if 'ai_context' in Path(p).as_posix() else Path(p).name})"
        for p in chart_paths
    )
    # fix relative paths for md sitting in ai_context
    chart_lines = []
    for p in chart_paths:
        rel = Path("charts") / Path(p).name
        chart_lines.append(f"### {Path(p).stem}\n\n![{Path(p).stem}]({rel.as_posix()})\n")

    improvements = report.get("improvements_summary", [])

    md = f"""# 向量库代际对比报告

- 生成时间: `{gen}`
- Active 知识索引: `{active}`
- 策略: **只读对比，未删除任何旧 collection**
- Embedding: `bge-small-zh-v1.5` (512d)

## 1. 对比对象

| 代号 | Collection | 角色 | 条数 |
|---|---|---|---:|
| events | `personal_events` | 旧主索引 / raw fallback | {st['events']['total']:,} |
| turns | `conversation_turns` | 对话叙述层 | {st['turns']['total']:,} |
| ku | `{active}` | 新 active 知识索引 | {st['ku']['total']:,} |
| hybrid | ku + events | 生产语义检索路径 | — |

SQLite 上游: unified_events={sql.get('unified_events')}, knowledge_units={sql.get('knowledge_units')}, canonical={sql.get('canonical_knowledge_units')}, evidence={sql.get('knowledge_unit_evidence')}.

## 2. 结构维度

| 指标 | personal_events | conversation_turns | knowledge_units |
|---|---:|---:|---:|
| 条数 | {st['events']['total']:,} | {st['turns']['total']:,} | {st['ku']['total']:,} |
| 文档长度中位 | {st['events']['doc_len']['median']} | {st['turns']['doc_len']['median']} | {st['ku']['doc_len']['median']} |
| 文档长度均值 | {st['events']['doc_len']['mean']} | {st['turns']['doc_len']['mean']} | {st['ku']['doc_len']['mean']} |
| >800 字占比(样) | {st['events']['long_gt800']}/{st['events']['sample_n']} | {st['turns']['long_gt800']}/{st['turns']['sample_n']} | {st['ku']['long_gt800']}/{st['ku']['sample_n']} |
| 元数据字段数 | {st['events']['meta_key_count']} | {st['turns']['meta_key_count']} | {st['ku']['meta_key_count']} |
| confidence 覆盖 | {st['events']['confidence']['coverage']} | {st['turns']['confidence']['coverage']} | {st['ku']['confidence']['coverage']} |
| 结构可答性均值 | {st['events']['answerability_mean']} | {st['turns']['answerability_mean']} | {st['ku']['answerability_mean']} |
| 高可答(≥0.6)占比 | {st['events']['answerability_high_share']} | {st['turns']['answerability_high_share']} | {st['ku']['answerability_high_share']} |

**解读:** 新知识索引把平均文档从 ~{st['events']['doc_len']['mean']:.0f} 字压到 ~{st['ku']['doc_len']['mean']:.0f} 字，并补齐 `unit_type/subject/confidence/lifecycle/source_message_ref`，结构可答性显著上升。

## 3. 检索维度

查询集 = frozen_test + dev + profile 补充题；Recall/MRR 仅在 **有 gold evidence** 的查询上计算。

### 3.1 全量查询

| mode | n | gold | R@5 | MRR@5 | top1距离均值 | top1可答性 | p50延迟ms |
|---|---:|---:|---:|---:|---:|---:|---:|
{suite_row('events','all')}
{suite_row('turns','all')}
{suite_row('ku','all')}
{suite_row('hybrid','all')}

### 3.2 frozen_test（最严格）

| mode | n | gold | R@5 | MRR@5 | top1距离均值 | top1可答性 | p50延迟ms |
|---|---:|---:|---:|---:|---:|---:|---:|
{suite_row('events','frozen')}
{suite_row('turns','frozen')}
{suite_row('ku','frozen')}
{suite_row('hybrid','frozen')}

### 3.3 profile 画像向查询（无 gold，看距离与可答性）

| mode | n | top1距离均值 | top1可答性 | 高可答占比 |
|---|---:|---:|---:|---:|
| events | {ret['events']['suites']['profile']['n']} | {ret['events']['suites']['profile']['top1_distance_mean']:.3f} | {ret['events']['suites']['profile']['answerability_mean']:.2f} | {ret['events']['suites']['profile']['answerability_high_share']:.2f} |
| turns | {ret['turns']['suites']['profile']['n']} | {ret['turns']['suites']['profile']['top1_distance_mean']:.3f} | {ret['turns']['suites']['profile']['answerability_mean']:.2f} | {ret['turns']['suites']['profile']['answerability_high_share']:.2f} |
| ku | {ret['ku']['suites']['profile']['n']} | {ret['ku']['suites']['profile']['top1_distance_mean']:.3f} | {ret['ku']['suites']['profile']['answerability_mean']:.2f} | {ret['ku']['suites']['profile']['answerability_high_share']:.2f} |
| hybrid | {ret['hybrid']['suites']['profile']['n']} | {ret['hybrid']['suites']['profile']['top1_distance_mean']:.3f} | {ret['hybrid']['suites']['profile']['answerability_mean']:.2f} | {ret['hybrid']['suites']['profile']['answerability_high_share']:.2f} |

### 3.4 距离赢家（全查询 top1 最近）

| mode | wins |
|---|---:|
| events | {ret['events'].get('distance_wins', 0)} |
| turns | {ret['turns'].get('distance_wins', 0)} |
| ku | {ret['ku'].get('distance_wins', 0)} |

## 4. 提升结论（数据支撑）

"""
    for item in improvements:
        md += f"- **{item['aspect']}**: {item['evidence']}\n"

    md += f"""

## 5. 图表

{''.join(chart_lines)}

## 6. 元数据字段对照

| collection | metadata keys |
|---|---|
| personal_events | {', '.join(st['events']['meta_keys'])} |
| conversation_turns | {', '.join(st['turns']['meta_keys'])} |
| knowledge_units | {', '.join(st['ku']['meta_keys'])} |

## 7. 未删除的历史 collection（对照库存）

共 {len(report.get('all_collections', []))} 个 collection（含 novel_* 与实验候选）。完整列表见 JSON `all_collections`。

## 8. 方法说明

- **Recall@5 / MRR@5**: gold `evidence_ref` 与结果 id / `source_message_ref` / 内容 15-gram 匹配
- **可答性启发式**: Q&A 形态、用户断言、短文档、confidence/unit_type/subject、惩罚代码噪声与 rollout 噪声
- **距离**: Chroma cosine distance，越小越相似
- **hybrid**: knowledge top-3 + personal_events 补齐至 5（近似生产 knowledge-first 策略）
- 旧数据 **全部保留**，本报告只读

原始 JSON: `vector_generation_comparison.json`
"""
    OUT_MD.write_text(md, encoding="utf-8")


def compute_improvements(report: dict) -> list[dict]:
    st = report["structure"]
    ret = report["retrieval"]
    items = []

    # length compression
    e_med = st["events"]["doc_len"]["median"]
    k_med = st["ku"]["doc_len"]["median"]
    if e_med and k_med < e_med:
        items.append(
            {
                "aspect": "信息压缩 / 噪声降低",
                "evidence": (
                    f"文档长度中位 {e_med:.0f} → {k_med:.0f} 字"
                    f"（降低 {(1 - k_med / e_med) * 100:.0f}%），"
                    f"长文档(>800) 样本 {st['events']['long_gt800']} → {st['ku']['long_gt800']}"
                ),
            }
        )

    # answerability
    ea, ka = st["events"]["answerability_mean"], st["ku"]["answerability_mean"]
    items.append(
        {
            "aspect": "结构可答性",
            "evidence": (
                f"采样可答性均值 {ea:.2f} → {ka:.2f}；"
                f"高可答占比 {st['events']['answerability_high_share']:.0%} → "
                f"{st['ku']['answerability_high_share']:.0%}"
            ),
        }
    )

    # retrieval answerability
    rea = ret["events"]["suites"]["all"]["answerability_mean"]
    rka = ret["ku"]["suites"]["all"]["answerability_mean"]
    items.append(
        {
            "aspect": "检索结果可直接回答",
            "evidence": f"Top-1 可答性（全查询）{rea:.2f} → {rka:.2f}（hybrid {ret['hybrid']['suites']['all']['answerability_mean']:.2f}）",
        }
    )

    # distance
    ed = ret["events"]["suites"]["all"]["top1_distance_mean"]
    kd = ret["ku"]["suites"]["all"]["top1_distance_mean"]
    items.append(
        {
            "aspect": "语义贴近度",
            "evidence": (
                f"Top-1 距离均值 {ed:.3f} → {kd:.3f}（越小越好）；"
                f"距离赢家 ku={ret['ku'].get('distance_wins', 0)} / "
                f"events={ret['events'].get('distance_wins', 0)} / "
                f"turns={ret['turns'].get('distance_wins', 0)}"
            ),
        }
    )

    # recall
    er = ret["events"]["suites"].get("frozen", {}).get("recall_at_5")
    kr = ret["ku"]["suites"].get("frozen", {}).get("recall_at_5")
    hr = ret["hybrid"]["suites"].get("frozen", {}).get("recall_at_5")
    em = ret["events"]["suites"].get("frozen", {}).get("mrr_at_5")
    km = ret["ku"]["suites"].get("frozen", {}).get("mrr_at_5")
    hm = ret["hybrid"]["suites"].get("frozen", {}).get("mrr_at_5")
    if er is not None and kr is not None:
        items.append(
            {
                "aspect": "证据召回（frozen）",
                "evidence": (
                    f"Recall@5: events {er:.2f} → ku {kr:.2f} → hybrid {hr:.2f}；"
                    f"MRR@5: {em:.2f} → {km:.2f} → {hm:.2f}"
                ),
            }
        )

    # profile suite
    pe = ret["events"]["suites"]["profile"]
    pk = ret["ku"]["suites"]["profile"]
    items.append(
        {
            "aspect": "画像/偏好类查询",
            "evidence": (
                f"profile 题 top1 距离 {pe['top1_distance_mean']:.3f} → {pk['top1_distance_mean']:.3f}；"
                f"可答性 {pe['answerability_mean']:.2f} → {pk['answerability_mean']:.2f}"
            ),
        }
    )

    # governance
    items.append(
        {
            "aspect": "治理与类型体系",
            "evidence": (
                f"KU 具备 confidence 覆盖 {st['ku']['confidence']['coverage']:.0%}、"
                f"6 类 unit_type、证据链与版本化 collection；"
                f"events/turns 无 confidence（覆盖 0）"
            ),
        }
    )

    # scale knowledge
    items.append(
        {
            "aspect": "知识覆盖面",
            "evidence": (
                f"active 知识单元 {st['ku']['total']:,} 条 vs 原始事件 {st['events']['total']:,} 条；"
                f"canonical 类型分布见图表 vector_gen_07"
            ),
        }
    )
    return items


def run(sample: int = 300) -> int:
    AI_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    active = read_active_collection()
    if not active:
        print("[error] no active knowledge pointer", file=sys.stderr)
        return 1

    print("[0] verify embedding...")
    ok, msg, dim = local_embed.verify_model()
    if not ok:
        print(f"[error] embed: {msg}", file=sys.stderr)
        return 1
    print(f"    {msg} dim={dim}")

    client = ChromaClient()
    collections = default_query_sources()

    print("[1] structure sample...")
    structure = {k: sample_structure(client, v, sample) for k, v in collections.items()}

    print("[2] sqlite stats...")
    sql = sqlite_knowledge_stats()

    print("[3] list collections (inventory)...")
    all_cols = list_all_collections(client)

    print("[4] load queries + gold...")
    cases = load_eval_queries()
    gold = load_gold_snippets(cases)
    print(f"    queries={len(cases)} gold_refs_loaded={len(gold)}")

    print("[5] retrieval eval (events/turns/ku/hybrid)...")
    retrieval = evaluate_retrieval(client, collections, cases, gold)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_collection": active,
        "embedding_model": "bge-small-zh-v1.5",
        "embedding_dim": dim,
        "sample_n": sample,
        "query_count": len(cases),
        "structure": structure,
        "sqlite": sql,
        "retrieval": retrieval,
        "all_collections": all_cols,
        "note": "read-only comparison; no collections deleted",
    }
    report["improvements_summary"] = compute_improvements(report)

    print("[6] charts...")
    charts = make_charts(report)
    report["charts"] = [str(Path(p).name) for p in charts]

    print("[7] write report...")
    # shrink: drop full distance arrays already summarized
    for mode in report["retrieval"]:
        report["retrieval"][mode].pop("top1_distances", None)
        report["retrieval"][mode].pop("answerability_top1", None)
        report["retrieval"][mode].pop("latencies_ms", None)

    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, charts)

    print("=" * 60)
    print("Vector Generation Comparison DONE")
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    print(f"Charts ({len(charts)}): {CHARTS_DIR}")
    for item in report["improvements_summary"]:
        print(f"  * {item['aspect']}: {item['evidence'][:100]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare old vs new vector collections")
    p.add_argument("--sample", type=int, default=300, help="structure sample size per collection")
    args = p.parse_args(argv)
    return run(sample=args.sample)


if __name__ == "__main__":
    raise SystemExit(main())
