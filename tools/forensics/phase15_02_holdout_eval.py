"""Phase 15-02: independent holdout eval on production layered search.

Measures generalization beyond frozen gold-evidence R@5:
  - google / paraphrase / no_answer / privacy tags
  - per-layer contribution from search telemetry
  - pad usage under allow_legacy_pad on vs off

Usage::

    python integration/scripts/_tools/phase15_02_holdout_eval.py
    python integration/scripts/_tools/phase15_02_holdout_eval.py --offline-smoke
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "integration" / "scripts"))

from core.project_paths import AI_CONTEXT_DIR  # noqa: E402
from unified_search import search_knowledge_units  # noqa: E402

HOLDOUT = (
    ROOT
    / "assets"
    / "evals"
    / "knowledge_units"
    / "holdout_15_02.synthetic.jsonl"
)
OUT = AI_CONTEXT_DIR / "phase15_02_holdout_eval.json"


def load_cases(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _text_blob(item: dict) -> str:
    return " ".join(
        str(item.get(k) or "")
        for k in ("subject", "answer", "unit_id", "source_message_ref", "event_id")
    )


def match_rank(results: list[dict], case: dict) -> int | None:
    gold_refs = set(case.get("gold_evidence_refs") or [])
    substrs = [s for s in (case.get("gold_title_substrings") or []) if s]
    for rank, item in enumerate(results, 1):
        uid = str(item.get("unit_id") or "")
        ref = str(item.get("source_message_ref") or "")
        eid = str(item.get("event_id") or "")
        if uid in gold_refs or ref in gold_refs or eid in gold_refs:
            return rank
        blob = _text_blob(item).lower()
        if substrs and all(s.lower() in blob for s in substrs):
            return rank
        if substrs and any(s.lower() in blob for s in substrs):
            return rank
    return None


def privacy_violation(results: list[dict], case: dict) -> bool:
    forbids = [s for s in (case.get("forbid_subject_substrings") or []) if s]
    if not forbids:
        return False
    for item in results:
        # Only flag when result looks like a light-assertion / knowledge claim subject
        subj = str(item.get("subject") or "")
        unit = str(item.get("retrieval_unit") or "")
        if unit == "knowledge_unit" and any(f in subj for f in forbids):
            return True
        # Explicit light-assertion style subjects in subject field
        if any(f in subj for f in forbids) and "interest" in subj.lower():
            return True
    return False


def eval_cases(
    cases: list[dict],
    *,
    fallback_policy: str,
    allow_legacy_pad: bool,
) -> dict:
    by_tag: dict[str, dict] = {}
    per: list[dict] = []
    hits = 0
    mrr = 0.0
    abstain_fp = 0
    abstain_cases = 0
    pad_used_n = 0
    layer_first_counts: dict[str, int] = defaultdict(int)
    scored = 0

    for case in cases:
        tag = case.get("suite_tag") or "unknown"
        pack = search_knowledge_units(
            case["query"],
            top_k=5,
            fallback_policy=fallback_policy,
            allow_legacy_pad=allow_legacy_pad,
        )
        results = pack.get("results") or []
        tel = pack.get("telemetry") or {}
        if tel.get("pad_used"):
            pad_used_n += 1
        first = tel.get("first_contributing_layer")
        if first:
            layer_first_counts[str(first)] += 1

        expected_abstain = bool(case.get("expected_abstain"))
        row = {
            "id": case["id"],
            "suite_tag": tag,
            "route": pack.get("route"),
            "found_rank": None,
            "expected_abstain": expected_abstain,
            "privacy_violation": privacy_violation(results, case),
            "first_contributing_layer": first,
            "pad_used": bool(tel.get("pad_used")),
            "retrieval_units": [r.get("retrieval_unit") for r in results[:5]],
            "layers": tel.get("layers") or [],
            "total_latency_ms": tel.get("total_latency_ms"),
        }

        bucket = by_tag.setdefault(
            tag,
            {
                "n": 0,
                "hits": 0,
                "mrr": 0.0,
                "abstain_fp": 0,
                "privacy_violations": 0,
                "pad_used": 0,
            },
        )
        bucket["n"] += 1
        if tel.get("pad_used"):
            bucket["pad_used"] += 1
        if row["privacy_violation"]:
            bucket["privacy_violations"] += 1

        if expected_abstain:
            abstain_cases += 1
            # False positive: system claims strong knowledge hit without abstain route
            # Soft check: knowledge_unit top1 counts as FP for secret-style no_answer
            top_unit = (results[0].get("retrieval_unit") if results else None)
            if pack.get("route") == "knowledge" and top_unit == "knowledge_unit":
                abstain_fp += 1
                bucket["abstain_fp"] += 1
                row["abstain_fp"] = True
            else:
                row["abstain_fp"] = False
        else:
            rank = match_rank(results, case)
            row["found_rank"] = rank
            if case.get("gold_evidence_refs") or case.get("gold_title_substrings"):
                scored += 1
                if rank:
                    hits += 1
                    mrr += 1.0 / rank
                    bucket["hits"] += 1
                    bucket["mrr"] += 1.0 / rank

        per.append(row)

    for b in by_tag.values():
        n_scored_tag = b["n"]  # for tags without gold, recall is informational
        b["recall_at_5"] = round(b["hits"] / max(b["n"], 1), 4)
        b["mrr_at_5"] = round(b["mrr"] / max(b["n"], 1), 4)
        del b["mrr"]

    n = max(scored, 1)
    return {
        "fallback_policy": fallback_policy,
        "allow_legacy_pad": allow_legacy_pad,
        "n_cases": len(cases),
        "n_scored": scored,
        "recall_at_5": round(hits / n, 4) if scored else None,
        "mrr_at_5": round(mrr / n, 4) if scored else None,
        "abstain_cases": abstain_cases,
        "abstain_false_positive": abstain_fp,
        "pad_used_rate": round(pad_used_n / max(len(cases), 1), 4),
        "first_layer_counts": dict(layer_first_counts),
        "by_suite_tag": by_tag,
        "per_query": per,
    }


def offline_smoke() -> dict:
    """Schema/telemetry smoke without requiring Chroma for empty query path."""
    pack = search_knowledge_units("", top_k=3, fallback_policy="layered")
    tel = pack.get("telemetry") or {}
    assert "layers" in tel
    assert pack.get("allow_legacy_pad") is not None
    names = [x["name"] for x in tel["layers"]]
    return {
        "empty_query_route": pack.get("route"),
        "telemetry_layer_names": names,
        "allow_legacy_pad": pack.get("allow_legacy_pad"),
        "ok": pack.get("route") == "abstain" and "legacy_pad" in names,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 15-02 holdout eval")
    p.add_argument("--cases", type=Path, default=HOLDOUT)
    p.add_argument("--report", type=Path, default=OUT)
    p.add_argument(
        "--offline-smoke",
        action="store_true",
        help="Only verify telemetry schema on abstain path",
    )
    args = p.parse_args(argv)

    if args.offline_smoke:
        smoke = offline_smoke()
        print(json.dumps(smoke, ensure_ascii=False, indent=2))
        return 0 if smoke.get("ok") else 1

    cases = load_cases(args.cases)
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "15-02",
        "cases_path": str(args.cases),
        "n_cases": len(cases),
        "modes": {},
        "legacy_pad_decision": {
            "default": True,
            "stage": "transition_observable",
            "rationale": (
                "Keep allow_legacy_pad=true until holdout pad_used_rate and "
                "Google/paraphrase recall support a documented flip to false; "
                "emergency rollback via PERSONAL_DATA_ALLOW_LEGACY_PAD=1."
            ),
            "flip_criteria": [
                "pad_used_rate on holdout < 0.15 for a week of samples",
                "google+paraphrase R@5 within -5pp of pad-on",
                "telemetry shipped to all consumers",
            ],
        },
    }
    for policy, pad in (
        ("layered", True),
        ("layered", False),
        ("legacy", True),
    ):
        key = f"{policy}_pad_{'on' if pad else 'off'}"
        print(f"[eval] {key}...")
        report["modes"][key] = eval_cases(
            cases, fallback_policy=policy, allow_legacy_pad=pad
        )
        m = report["modes"][key]
        print(
            f"  scored={m['n_scored']} R@5={m['recall_at_5']} "
            f"pad_rate={m['pad_used_rate']} abstain_fp={m['abstain_false_positive']}"
        )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote", args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
