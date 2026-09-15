"""Wave 10.2: 固定召回评测。

对固定的真实本地 query 集同时评测:
1. `personal_events`
2. `conversation_turns`
3. `search_all` 跨 collection 合并检索

输出:
  integration/analysis/ai_context/vector_retrieval_eval_report.json
  integration/analysis/ai_context/vector_retrieval_eval_report.md

用法:
  python integration\\scripts\\evaluate_vector_retrieval.py
  python integration\\scripts\\evaluate_vector_retrieval.py --write --top-k 5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from personal_knowledge.retrieval.search_vectors import search, search_all, search_conversation_turns


ROOT = Path(__file__).resolve().parents[3]
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
EVAL_SET = AI_DIR / "vector_retrieval_eval_set.json"
OUT_JSON = AI_DIR / "vector_retrieval_eval_report.json"
OUT_MD = AI_DIR / "vector_retrieval_eval_report.md"


def load_eval_set() -> list[dict]:
    if not EVAL_SET.exists():
        raise FileNotFoundError(f"缺少评测集: {EVAL_SET}")
    return json.loads(EVAL_SET.read_text(encoding="utf-8"))


def infer_collection(result: dict) -> str:
    if result.get("session_id") or result.get("event_type") == "conversation_turn":
        return "conversation_turns"
    return "personal_events"


def normalize_result(result: dict) -> dict:
    out = dict(result)
    out["collection"] = infer_collection(result)
    return out


def run_query(mode: str, query: str, top_k: int) -> list[dict]:
    if mode == "personal_events":
        results = search(query, top_k=top_k)
    elif mode == "conversation_turns":
        results = search_conversation_turns(query, top_k=top_k)
    elif mode == "search_all":
        results = search_all(query, top_k=top_k)
    else:
        raise ValueError(f"unknown mode: {mode}")
    return [normalize_result(r) for r in results]


def match_result(sample: dict, result: dict) -> dict:
    expected_sessions = set(sample.get("expected_session_ids") or [])
    expected_turns = set(str(x) for x in (sample.get("expected_turn_ids") or []))
    expected_source = sample.get("expected_source")

    session_match = bool(expected_sessions and result.get("session_id") in expected_sessions)
    turn_match = bool(
        expected_turns
        and str(result.get("turn_id") or "") in expected_turns
        and (not expected_sessions or result.get("session_id") in expected_sessions)
    )
    source_match = bool(expected_source and result.get("source") == expected_source)

    exact_match = turn_match or session_match
    return {
        "exact_match": exact_match,
        "session_match": session_match,
        "turn_match": turn_match,
        "source_match": source_match,
        "collection_match": sample.get("preferred_collection") == result.get("collection"),
    }


def evaluate_mode(samples: list[dict], mode: str, top_k: int) -> dict:
    details = []
    exact_queries = 0
    exact_hits = 0
    exact_rr_sum = 0.0
    source_queries = 0
    source_hits = 0
    preferred_queries = 0
    preferred_top1_hits = 0
    result_source_counter: dict[str, int] = {}
    result_collection_counter: dict[str, int] = {}

    for sample in samples:
        results = run_query(mode, sample["query"], top_k)
        top1 = results[0] if results else None

        if sample.get("preferred_collection"):
            preferred_queries += 1
            if top1 and top1.get("collection") == sample["preferred_collection"]:
                preferred_top1_hits += 1

        if sample.get("expected_source"):
            source_queries += 1

        if sample.get("expected_session_ids") or sample.get("expected_turn_ids"):
            exact_queries += 1

        first_exact_rank = None
        first_source_rank = None
        annotated = []
        for idx, result in enumerate(results, 1):
            marks = match_result(sample, result)
            item = {
                "rank": idx,
                "score": result.get("score"),
                "source": result.get("source"),
                "collection": result.get("collection"),
                "session_id": result.get("session_id", ""),
                "turn_id": result.get("turn_id", ""),
                "title": result.get("title", ""),
                "matches": marks,
            }
            annotated.append(item)
            key_source = result.get("source") or "unknown"
            key_collection = result.get("collection") or "unknown"
            result_source_counter[key_source] = result_source_counter.get(key_source, 0) + 1
            result_collection_counter[key_collection] = result_collection_counter.get(key_collection, 0) + 1
            if first_exact_rank is None and marks["exact_match"]:
                first_exact_rank = idx
            if first_source_rank is None and marks["source_match"]:
                first_source_rank = idx

        if first_exact_rank is not None:
            exact_hits += 1
            exact_rr_sum += 1.0 / first_exact_rank
        if first_source_rank is not None:
            source_hits += 1

        details.append(
            {
                "sample_id": sample["sample_id"],
                "query": sample["query"],
                "expected_source": sample.get("expected_source"),
                "expected_session_ids": sample.get("expected_session_ids", []),
                "preferred_collection": sample.get("preferred_collection"),
                "first_exact_rank": first_exact_rank,
                "first_source_rank": first_source_rank,
                "top1": top1,
                "results": annotated,
            }
        )

    return {
        "mode": mode,
        "top_k": top_k,
        "metrics": {
            "exact_queries": exact_queries,
            "exact_hits": exact_hits,
            "recall_at_k": round(exact_hits / exact_queries, 4) if exact_queries else None,
            "mrr": round(exact_rr_sum / exact_queries, 4) if exact_queries else None,
            "source_queries": source_queries,
            "source_hits": source_hits,
            "source_hit_rate": round(source_hits / source_queries, 4) if source_queries else None,
            "preferred_collection_queries": preferred_queries,
            "preferred_collection_top1_hits": preferred_top1_hits,
            "preferred_collection_top1_rate": (
                round(preferred_top1_hits / preferred_queries, 4) if preferred_queries else None
            ),
        },
        "result_source_mix": dict(sorted(result_source_counter.items())),
        "result_collection_mix": dict(sorted(result_collection_counter.items())),
        "samples": details,
    }


def render_md(report: dict) -> str:
    lines = ["# Vector Retrieval Eval Report", ""]
    lines.append(f"- top_k: {report['top_k']}")
    lines.append(f"- samples: {report['sample_count']}")
    lines.append("")
    lines += [
        "| mode | exact recall@k | MRR | source hit rate | preferred collection top1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode in report["modes"]:
        m = mode["metrics"]
        lines.append(
            f"| {mode['mode']} | {m['recall_at_k']} | {m['mrr']} | "
            f"{m['source_hit_rate']} | {m['preferred_collection_top1_rate']} |"
        )
    lines.append("")

    for mode in report["modes"]:
        lines.append(f"## {mode['mode']}")
        lines.append("")
        lines.append(
            f"- result_source_mix: {json.dumps(mode['result_source_mix'], ensure_ascii=False)}"
        )
        lines.append(
            f"- result_collection_mix: {json.dumps(mode['result_collection_mix'], ensure_ascii=False)}"
        )
        lines.append("")
        failed = [s for s in mode["samples"] if s["first_exact_rank"] is None]
        if failed:
            lines.append("### exact miss")
            lines.append("")
            for sample in failed:
                top1 = sample.get("top1") or {}
                lines.append(
                    f"- {sample['sample_id']}: `{sample['query']}` -> top1 "
                    f"[{top1.get('collection','-')}] {top1.get('source','-')} / {top1.get('title','')}"
                )
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wave 10.2 固定召回评测")
    parser.add_argument("--write", action="store_true", help="把评测结果写入 ai_context")
    parser.add_argument("--top-k", type=int, default=5, help="每种模式返回的 top-k")
    args = parser.parse_args(argv)

    samples = load_eval_set()
    modes = [
        evaluate_mode(samples, "personal_events", args.top_k),
        evaluate_mode(samples, "conversation_turns", args.top_k),
        evaluate_mode(samples, "search_all", args.top_k),
    ]
    report = {
        "top_k": args.top_k,
        "sample_count": len(samples),
        "modes": modes,
    }
    md = render_md(report)
    print(md)

    if args.write:
        OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        OUT_MD.write_text(md, encoding="utf-8")
        print(f"[write] {OUT_JSON.relative_to(ROOT)}")
        print(f"[write] {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
