"""Phase 15 Wave 4: compare hybrid policies on frozen set (gold evidence match)."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "integration" / "scripts"))

from personal_knowledge.retrieval.unified_search import (  # noqa: E402
    search_knowledge_units,
    _search_dialogue_canonical_messages,
)
from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB, AI_CONTEXT_DIR  # noqa: E402

EVAL = ROOT / "assets" / "evals" / "knowledge_units" / "frozen_test_queries.private.jsonl"
TAGS = ROOT / "assets" / "evals" / "knowledge_units" / "suite_tags.json"
OUT = AI_CONTEXT_DIR / "phase15_wave4_hybrid_eval.json"


def load_cases() -> list[dict]:
    return [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_tags() -> dict:
    if TAGS.exists():
        return json.loads(TAGS.read_text(encoding="utf-8")).get("tags") or {}
    return {}


def load_gold(cases: list[dict]) -> dict[str, str]:
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


def match_rank(results: list[dict], gold_refs: set[str], gold_snips: list[str]) -> int | None:
    for rank, item in enumerate(results, 1):
        uid = str(item.get("unit_id") or "")
        ref = str(item.get("source_message_ref") or "")
        if uid in gold_refs or ref in gold_refs:
            return rank
        doc = (item.get("answer") or "") + " " + (item.get("subject") or "")
        for sn in gold_snips:
            if sn and len(sn) >= 15 and sn[:15] in doc:
                return rank
    return None


def eval_policy(cases, gold_map, tags, policy: str) -> dict:
    hits = 0
    mrr = 0.0
    by_tag: dict[str, dict] = {}
    per = []
    for case in cases:
        q = case["query"]
        gold_refs = set(case.get("gold_evidence_refs") or [])
        snips = [gold_map[r] for r in gold_refs if r in gold_map]
        tag = tags.get(case["id"], "unknown")
        if policy == "dialogue_only":
            results = _search_dialogue_canonical_messages(q, top_k=5)
        else:
            pack = search_knowledge_units(q, top_k=5, fallback_policy=policy)
            results = pack.get("results") or []
        rank = match_rank(results, gold_refs, snips)
        if rank:
            hits += 1
            mrr += 1.0 / rank
        bucket = by_tag.setdefault(tag, {"n": 0, "hits": 0, "mrr": 0.0})
        bucket["n"] += 1
        if rank:
            bucket["hits"] += 1
            bucket["mrr"] += 1.0 / rank
        units = [r.get("retrieval_unit") for r in results[:5]]
        per.append(
            {
                "id": case["id"],
                "suite_tag": tag,
                "found_rank": rank,
                "retrieval_units": units,
                "top_collections": [r.get("collection") for r in results[:3]],
            }
        )
    n = max(len(cases), 1)
    for b in by_tag.values():
        b["recall_at_5"] = round(b["hits"] / max(b["n"], 1), 4)
        b["mrr_at_5"] = round(b["mrr"] / max(b["n"], 1), 4)
        del b["mrr"]
    return {
        "policy": policy,
        "n": len(cases),
        "recall_at_5": round(hits / n, 4),
        "mrr_at_5": round(mrr / n, 4),
        "by_suite_tag": by_tag,
        "per_query": per,
    }


def main() -> int:
    cases = load_cases()
    tags = load_tags()
    gold = load_gold(cases)
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_cases": len(cases),
        "gold_loaded": len(gold),
        "modes": {},
    }
    for policy in ("dialogue_only", "legacy", "layered"):
        print(f"[eval] {policy}...")
        report["modes"][policy] = eval_policy(cases, gold, tags, policy)
        m = report["modes"][policy]
        print(f"  R@5={m['recall_at_5']} MRR={m['mrr_at_5']} by_tag={m['by_suite_tag']}")
    AI_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
