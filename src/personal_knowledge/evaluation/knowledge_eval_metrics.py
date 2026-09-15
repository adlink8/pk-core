"""Strict retrieval metrics + paired bootstrap statistics for Phase 17."""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCORER_VERSION = "knowledge_eval_metrics_v2"
BOOTSTRAP_SEED = 1701
BOOTSTRAP_B = 10_000


@dataclass
class RankedHit:
    id: str
    score: float = 0.0
    source_ref: str = ""
    subject: str = ""
    snippet: str = ""
    layer: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseScore:
    query_id: str
    mode: str
    hit_primary: bool
    hit_diagnostic_snippet: bool
    rank_primary: int | None
    rank_diagnostic: int | None
    expected_abstain: bool
    no_answer_fp: bool
    privacy_hit: bool
    secret_hit: bool
    latency_ms: float
    first_layer: str = ""
    score_retrieval: bool = True
    top_score: float | None = None
    score_margin: float | None = None
    ranked_ids: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def primary_rank(
    ranked: Sequence[RankedHit],
    gold_refs: set[str],
    gold_unit_ids: set[str],
) -> int | None:
    """Primary hit: stable ID / evidence ref match only."""
    for i, h in enumerate(ranked, 1):
        if h.id in gold_unit_ids or h.id in gold_refs:
            return i
        if h.source_ref and h.source_ref in gold_refs:
            return i
        # unit_id stored without prefix variants
        uid = h.meta.get("unit_id") or h.meta.get("canonical_unit_id") or ""
        if uid and (uid in gold_unit_ids or uid in gold_refs):
            return i
    return None


def diagnostic_snippet_rank(
    ranked: Sequence[RankedHit],
    snippets: Sequence[str],
    title_substrings: Sequence[str] | None = None,
) -> int | None:
    """Diagnostic-only substring match; not used for primary claims."""
    snips = [s for s in snippets if s and len(s) >= 15]
    titles = [t for t in (title_substrings or []) if t]
    for i, h in enumerate(ranked, 1):
        doc = f"{h.snippet} {h.subject}".lower()
        for s in snips:
            if s[:15].lower() in doc:
                return i
        for t in titles:
            if t.lower() in doc or t.lower() in (h.subject or "").lower():
                return i
    return None


def score_case(
    query_id: str,
    mode: str,
    ranked: Sequence[RankedHit],
    *,
    gold_refs: Sequence[str] | None = None,
    gold_unit_ids: Sequence[str] | None = None,
    gold_snippets: Sequence[str] | None = None,
    gold_title_substrings: Sequence[str] | None = None,
    expected_abstain: bool = False,
    privacy_sensitive: bool = False,
    secret_ineligible: bool = False,
    forbid_subject_substrings: Sequence[str] | None = None,
    latency_ms: float = 0.0,
    first_layer: str = "",
    score_retrieval: bool = True,
) -> CaseScore:
    refs = set(gold_refs or [])
    units = set(gold_unit_ids or [])
    pr = primary_rank(ranked, refs, units)
    dr = diagnostic_snippet_rank(ranked, gold_snippets or [], gold_title_substrings)
    hit_primary = pr is not None
    # abstain FP: expected abstain but returned non-empty factual ranking
    no_answer_fp = bool(expected_abstain and len(ranked) > 0 and not secret_ineligible)

    privacy_hit = False
    forbid = [s for s in (forbid_subject_substrings or []) if s]
    if forbid:
        for h in ranked:
            subj = f"{h.subject} {h.snippet}"
            if any(f in subj for f in forbid):
                privacy_hit = True
                break
    if privacy_sensitive and privacy_hit:
        pass

    # Secret leakage is a provenance property, not a lexical property. Generic
    # technical content containing words such as "API" must not be treated as
    # proof that a secret-bearing source was retrieved.
    secret_hit = bool(
        secret_ineligible
        and any(
            bool(h.meta.get("source_evidence_ineligible"))
            or str(h.meta.get("evidence_scope") or "").lower()
            in {"secret", "excluded"}
            for h in ranked
        )
    )

    top_score = float(ranked[0].score) if ranked else None
    score_margin = (
        float(ranked[0].score) - float(ranked[1].score)
        if len(ranked) > 1
        else None
    )

    return CaseScore(
        query_id=query_id,
        mode=mode,
        hit_primary=hit_primary,
        hit_diagnostic_snippet=dr is not None,
        rank_primary=pr,
        rank_diagnostic=dr,
        expected_abstain=expected_abstain,
        no_answer_fp=no_answer_fp,
        privacy_hit=privacy_hit,
        secret_hit=secret_hit,
        latency_ms=latency_ms,
        first_layer=first_layer,
        score_retrieval=score_retrieval,
        top_score=top_score,
        score_margin=score_margin,
        ranked_ids=[h.id for h in ranked],
    )


def _safe_div(n: float, d: float) -> float | None:
    if d <= 0:
        return None
    return n / d


def aggregate_scores(
    scores: Sequence[CaseScore],
    *,
    k_values: Sequence[int] = (1, 5, 10),
) -> dict[str, Any]:
    if not scores:
        return {
            "n": 0,
            "recall_at": {str(k): None for k in k_values},
            "mrr_at_5": None,
            "ndcg_at_5": None,
            "no_answer_fp_rate": None,
            "privacy_hit": 0,
            "secret_hit": 0,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "insufficient_evidence": True,
        }

    n = len(scores)
    # only non-abstain cases contribute to recall/MRR denominators
    ranked_cases = [s for s in scores if not s.expected_abstain and s.score_retrieval]
    den = len(ranked_cases)

    recall: dict[str, float | None] = {}
    for k in k_values:
        if den == 0:
            recall[str(k)] = None
        else:
            hits = sum(
                1
                for s in ranked_cases
                if s.rank_primary is not None and s.rank_primary <= k
            )
            recall[str(k)] = hits / den

    mrr_sum = 0.0
    ndcg_sum = 0.0
    for s in ranked_cases:
        if s.rank_primary is not None and s.rank_primary <= 5:
            mrr_sum += 1.0 / s.rank_primary
            # binary nDCG@5
            ndcg_sum += 1.0 / math.log2(s.rank_primary + 1)
    # ideal DCG for single relevant = 1
    mrr = _safe_div(mrr_sum, den)
    ndcg = _safe_div(ndcg_sum, den)

    abstain_cases = [s for s in scores if s.expected_abstain]
    fp = sum(1 for s in abstain_cases if s.no_answer_fp)
    fp_rate = _safe_div(fp, len(abstain_cases)) if abstain_cases else 0.0

    lats = sorted(s.latency_ms for s in scores)
    privacy_hit = sum(1 for s in scores if s.privacy_hit)
    secret_hit = sum(1 for s in scores if s.secret_hit)

    return {
        "n": n,
        "n_scored_retrieval": den,
        "n_abstain": len(abstain_cases),
        "recall_at": recall,
        "mrr_at_5": mrr,
        "ndcg_at_5": ndcg,
        "no_answer_fp": fp,
        "no_answer_fp_rate": fp_rate,
        "privacy_hit": privacy_hit,
        "secret_hit": secret_hit,
        "p50_latency_ms": _percentile(lats, 50),
        "p95_latency_ms": _percentile(lats, 95),
        "insufficient_evidence": den < 5,
        "scorer_version": SCORER_VERSION,
    }


def _percentile(sorted_vals: Sequence[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(len(sorted_vals) * p / 100)
    idx = min(max(idx, 0), len(sorted_vals) - 1)
    return float(sorted_vals[idx])


def paired_bootstrap_ci(
    baseline: Sequence[CaseScore],
    treatment: Sequence[CaseScore],
    *,
    metric: str = "recall_at_5",
    seed: int = BOOTSTRAP_SEED,
    B: int = BOOTSTRAP_B,
) -> dict[str, Any]:
    """Paired bootstrap on query_id for delta(treatment - baseline)."""
    b_map = {s.query_id: s for s in baseline}
    t_map = {s.query_id: s for s in treatment}
    ids = sorted(set(b_map) & set(t_map))
    # only non-abstain for recall metrics
    ids = [
        q
        for q in ids
        if not b_map[q].expected_abstain
        and not t_map[q].expected_abstain
        and b_map[q].score_retrieval
        and t_map[q].score_retrieval
    ]
    if len(ids) < 5:
        return {
            "metric": metric,
            "n": len(ids),
            "delta": None,
            "ci_low": None,
            "ci_high": None,
            "insufficient_evidence": True,
            "seed": seed,
            "B": B,
        }

    def per_query_hit(s: CaseScore, k: int = 5) -> float:
        if s.rank_primary is not None and s.rank_primary <= k:
            return 1.0
        return 0.0

    def per_query_mrr(s: CaseScore) -> float:
        if s.rank_primary is not None and s.rank_primary <= 5:
            return 1.0 / s.rank_primary
        return 0.0

    def value(s: CaseScore) -> float:
        if metric == "mrr_at_5":
            return per_query_mrr(s)
        # default recall@5
        return per_query_hit(s, 5)

    deltas = [value(t_map[q]) - value(b_map[q]) for q in ids]
    point = sum(deltas) / len(deltas)

    rng = random.Random(seed)
    boots: list[float] = []
    n = len(deltas)
    for _ in range(B):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(sample) / n)
    boots.sort()
    lo = boots[int(0.025 * B)]
    hi = boots[min(int(0.975 * B), B - 1)]
    return {
        "metric": metric,
        "n": n,
        "delta": point,
        "delta_pp": point * 100,
        "ci_low": lo,
        "ci_high": hi,
        "ci_low_pp": lo * 100,
        "ci_high_pp": hi * 100,
        "insufficient_evidence": False,
        "seed": seed,
        "B": B,
    }


def per_scenario(
    scores: Sequence[CaseScore],
    scenario_by_id: Mapping[str, str],
) -> dict[str, Any]:
    buckets: dict[str, list[CaseScore]] = {}
    for s in scores:
        key = scenario_by_id.get(s.query_id, "untagged")
        buckets.setdefault(key, []).append(s)
    return {k: aggregate_scores(v) for k, v in sorted(buckets.items())}


def win_loss(
    baseline: Sequence[CaseScore],
    treatment: Sequence[CaseScore],
) -> dict[str, Any]:
    b_map = {s.query_id: s for s in baseline}
    t_map = {s.query_id: s for s in treatment}
    wins, losses, ties = [], [], []
    for q in sorted(set(b_map) & set(t_map)):
        if b_map[q].expected_abstain or not b_map[q].score_retrieval or not t_map[q].score_retrieval:
            continue
        br = b_map[q].rank_primary
        tr = t_map[q].rank_primary
        b_hit = br is not None and br <= 5
        t_hit = tr is not None and tr <= 5
        if t_hit and not b_hit:
            wins.append(q)
        elif b_hit and not t_hit:
            losses.append(q)
        else:
            ties.append(q)
    return {
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "n_win": len(wins),
        "n_loss": len(losses),
        "n_tie": len(ties),
    }


def compare_modes(
    mode_scores: Mapping[str, Sequence[CaseScore]],
    *,
    baseline: str = "raw",
) -> dict[str, Any]:
    out: dict[str, Any] = {"baseline": baseline, "comparisons": {}}
    if baseline not in mode_scores:
        out["error"] = f"baseline {baseline} missing"
        return out
    base = mode_scores[baseline]
    for mode, scores in mode_scores.items():
        if mode == baseline:
            continue
        agg_b = aggregate_scores(base)
        agg_t = aggregate_scores(scores)
        r5_b = (agg_b.get("recall_at") or {}).get("5")
        r5_t = (agg_t.get("recall_at") or {}).get("5")
        delta = None if r5_b is None or r5_t is None else r5_t - r5_b
        rel = None if not r5_b or r5_t is None else (r5_t - r5_b) / r5_b
        out["comparisons"][mode] = {
            "recall_at_5_baseline": r5_b,
            "recall_at_5_treatment": r5_t,
            "delta": delta,
            "delta_pp": None if delta is None else delta * 100,
            "relative": rel,
            "bootstrap": paired_bootstrap_ci(base, scores, metric="recall_at_5"),
            "bootstrap_mrr": paired_bootstrap_ci(base, scores, metric="mrr_at_5"),
            "win_loss": win_loss(base, scores),
            "aggregate_treatment": agg_t,
        }
    return out
