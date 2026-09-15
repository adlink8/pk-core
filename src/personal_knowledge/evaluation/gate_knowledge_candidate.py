"""Fail-closed promotion gate for knowledge index candidates."""

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

from personal_knowledge.core.project_paths import DB_DIR, ROOT  # noqa: E402
from personal_knowledge.evaluation.eval_contracts import content_checksum, dump_json  # noqa: E402


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_policy(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text)
        except Exception:
            # minimal YAML subset: JSON-compatible YAML preferred
            return json.loads(text)
    return json.loads(text)


def read_active_pointer() -> str:
    p = DB_DIR / "knowledge_index_active.txt"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def evaluate_gate(
    summary: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    candidate_collection: str = "",
    candidate_checksum: str = "",
    require_answer: bool = True,
) -> dict[str, Any]:
    """Return PASS/FAIL verdict with reasons. Never mutates active."""
    hard = policy.get("hard_gates") or {}
    quality = policy.get("quality_gates") or {}
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    dataset_audit = (summary.get("stage_details") or {}).get("dataset_audit") or {}
    dataset_ok = bool(dataset_audit.get("ok"))
    checks.append(
        {
            "name": "dataset_audit",
            "passed": dataset_ok,
            "value": {
                "real_gold_cases": dataset_audit.get("real_gold_cases"),
                "real_cross_turn_gold_cases": dataset_audit.get("real_cross_turn_gold_cases"),
            },
        }
    )
    if not dataset_ok:
        reasons.append("dataset audit failed: " + "; ".join(dataset_audit.get("errors") or ["missing audit evidence"]))

    if policy.get("require_review_evidence", policy.get("require_human_review_evidence", False)):
        human_review = (summary.get("stage_details") or {}).get("human_review") or {}
        human_ok = bool(human_review.get("ok")) and bool(
            human_review.get("binding_checksum")
        )
        checks.append(
            {
                "name": "review_evidence",
                "passed": human_ok,
                "value": {
                    "checks": human_review.get("checks") or {},
                    "binding_checksum": human_review.get("binding_checksum"),
                },
            }
        )
        if not human_ok:
            reasons.append("review evidence incomplete or unbound")

    modes = summary.get("modes") or summary.get("metrics") or {}
    if not modes:
        reasons.append("no mode metrics in eval run")

    def agg(mode: str) -> dict:
        payload = modes.get(mode) or {}
        return payload.get("aggregate") or payload

    # Hard safety gates apply to publishable candidate modes. Raw and L1 may
    # remain as diagnostic baselines without allowing their legacy behavior to
    # veto an otherwise safe candidate.
    candidate_safety_modes = set(policy.get("candidate_safety_modes") or modes)
    for mode, payload in modes.items():
        if mode not in candidate_safety_modes:
            continue
        a = payload.get("aggregate") or payload
        ph = int(a.get("privacy_hit") or 0)
        sh = int(a.get("secret_hit") or 0)
        checks.append(
            {
                "name": f"privacy_secret:{mode}",
                "passed": ph == 0 and sh == 0,
                "value": {"privacy_hit": ph, "secret_hit": sh},
            }
        )
        if ph > 0 or sh > 0:
            reasons.append(f"{mode}: privacy/secret hit > 0")

        max_fp = hard.get("no_answer_fp_max")
        fp_rate = a.get("no_answer_fp_rate")
        if max_fp is not None:
            ok = fp_rate is not None and float(fp_rate) <= float(max_fp)
            checks.append(
                {
                    "name": f"no_answer_fp:{mode}",
                    "passed": ok,
                    "value": fp_rate,
                    "threshold": max_fp,
                }
            )
            if not ok:
                reasons.append(f"{mode}: no-answer FP {fp_rate} exceeds {max_fp}")

    # Hard: all primary modes present for full claim
    required_modes = policy.get("required_modes") or [
        "raw",
        "l1",
        "l1_l2",
        "hybrid",
    ]
    for m in required_modes:
        ok = m in modes and not (modes[m] or {}).get("blocked")
        checks.append({"name": f"mode_present:{m}", "passed": ok, "value": m in modes})
        if not ok:
            reasons.append(f"missing or blocked mode: {m}")

    # Answer eval required
    answer = summary.get("answer") or {}
    if require_answer and policy.get("require_answer_eval", True):
        ok = bool(answer) and not answer.get("skipped")
        checks.append({"name": "answer_eval_present", "passed": ok, "value": bool(answer)})
        if not ok:
            reasons.append("answer eval missing (fail-closed)")
        else:
            answer_modes = answer.get("modes") or {}
            min_citation = hard.get("citation_precision_min")
            if min_citation is not None:
                for mode in required_modes:
                    payload = answer_modes.get(mode) or {}
                    if payload.get("blocked"):
                        continue
                    value = (payload.get("aggregate") or {}).get("citation_precision")
                    passed_citation = value is not None and float(value) >= float(min_citation)
                    checks.append(
                        {
                            "name": f"citation_precision:{mode}",
                            "passed": passed_citation,
                            "value": value,
                            "threshold": min_citation,
                        }
                    )
                    if not passed_citation:
                        reasons.append(
                            f"{mode}: citation precision {value} below {min_citation}"
                        )

    # Lineage/reconcile must be clean before any publish decision.
    if "reconcile_missing" in hard or "reconcile_orphan" in hard:
        lineage = (summary.get("stage_details") or {}).get("lineage") or {}
        discrepancy = lineage.get("discrepancy") or {}
        total = int(discrepancy.get("total_l2_status_current") or 0)
        links = int(lineage.get("l2_member_links") or 0)
        missing = max(total - links, 0)
        orphan = max(links - total, 0)
        lineage_ok = bool(lineage.get("ok"))
        missing_limit = int(hard.get("reconcile_missing") or 0)
        orphan_limit = int(hard.get("reconcile_orphan") or 0)
        reconcile_ok = lineage_ok and missing <= missing_limit and orphan <= orphan_limit
        checks.append(
            {
                "name": "reconcile_integrity",
                "passed": reconcile_ok,
                "value": {"missing": missing, "orphan": orphan, "lineage_ok": lineage_ok},
            }
        )
        if not reconcile_ok:
            reasons.append(
                f"reconcile failed: lineage_ok={lineage_ok} missing={missing} orphan={orphan}"
            )

    # Scorer errors
    if summary.get("scorer_error"):
        reasons.append(f"scorer_error: {summary.get('scorer_error')}")
        checks.append({"name": "scorer_error", "passed": False, "value": summary.get("scorer_error")})

    # Candidate checksum match
    expected_ck = candidate_checksum or summary.get("candidate_checksum") or ""
    summary_ck = summary.get("candidate_checksum") or ""
    if candidate_collection and summary.get("candidate_collection"):
        if candidate_collection != summary.get("candidate_collection"):
            reasons.append("candidate collection mismatch")
            checks.append({"name": "candidate_collection_match", "passed": False})
    if expected_ck and summary_ck and expected_ck != summary_ck:
        reasons.append("candidate checksum mismatch")
        checks.append(
            {
                "name": "candidate_checksum_match",
                "passed": False,
                "value": {"expected": expected_ck, "got": summary_ck},
            }
        )

    # Quality: pure-KU vs raw primary claim
    comparisons = summary.get("comparisons") or {}
    # prefer l1_l2 vs raw
    primary = comparisons.get("l1_l2") or comparisons.get("l1") or {}
    delta = primary.get("delta")
    boot = primary.get("bootstrap") or {}
    min_pp = float((quality.get("ku_vs_raw_recall5_pp") or {}).get("min_delta_pp", 10))
    if delta is None:
        reasons.append("primary KU vs Raw delta missing → 未证明提升")
        checks.append({"name": "primary_claim_delta", "passed": False, "value": None})
        claim = "未证明提升"
    else:
        delta_pp = delta * 100
        ci_low = boot.get("ci_low")
        ok = delta_pp >= min_pp and (ci_low is None or ci_low > 0)
        if boot.get("insufficient_evidence"):
            ok = False
            reasons.append("insufficient_evidence for primary claim")
        if not ok:
            reasons.append(
                f"primary claim failed: delta_pp={delta_pp:.2f} min={min_pp} "
                f"ci_low={ci_low}"
            )
        checks.append(
            {
                "name": "primary_claim_ku_vs_raw",
                "passed": ok,
                "value": {"delta_pp": delta_pp, "ci_low": ci_low, "min_pp": min_pp},
            }
        )
        claim = "已证明提升" if ok else "未证明提升"

    # Pure-KU regression vs L1 baseline: hybrid must not mask
    l1 = agg("l1")
    l1l2 = agg("l1_l2")
    r_l1 = (l1.get("recall_at") or {}).get("5")
    r_l2 = (l1l2.get("recall_at") or {}).get("5")
    max_reg_pp = float(
        (quality.get("frozen_regression_pp") or {}).get("max_drop_pp", 2)
    )
    if r_l1 is not None and r_l2 is not None:
        drop_pp = (r_l1 - r_l2) * 100
        ok = drop_pp <= max_reg_pp
        checks.append(
            {
                "name": "pure_ku_regression_l1_to_l1l2",
                "passed": ok,
                "value": {"drop_pp": drop_pp, "max_drop_pp": max_reg_pp},
            }
        )
        if not ok:
            reasons.append(
                f"pure-KU regression L1→L1+L2 drop {drop_pp:.1f}pp > {max_reg_pp}pp "
                f"(Hybrid must not mask this)"
            )

    # Ranking non-inferiority uses the paired MRR bootstrap declared by AI-SPEC.
    mrr_policy = quality.get("mrr_at_5_non_inferior_pp") or {}
    if mrr_policy:
        mrr_boot = primary.get("bootstrap_mrr") or {}
        ci_low = mrr_boot.get("ci_low")
        max_drop_pp = float(mrr_policy.get("max_drop_pp", 2))
        ok = (
            ci_low is not None
            and not mrr_boot.get("insufficient_evidence")
            and float(ci_low) * 100 >= -max_drop_pp
        )
        checks.append(
            {
                "name": "mrr_at_5_non_inferior",
                "passed": ok,
                "value": {"ci_low_pp": None if ci_low is None else float(ci_low) * 100},
                "threshold": -max_drop_pp,
            }
        )
        if not ok:
            reasons.append(f"MRR@5 non-inferiority failed: ci_low={ci_low}")

    # Cross-turn contribution is evaluated against L1, never against Raw.
    cross_policy = quality.get("cross_turn_l2_vs_l1_pp") or {}
    if cross_policy:
        cross = (
            (summary.get("scenario_comparisons") or {})
            .get("cross_turn_l1_baseline", {})
            .get("l1_l2", {})
        )
        boot = cross.get("bootstrap") or {}
        delta = cross.get("delta")
        n = int(boot.get("n") or 0)
        min_cases = int(cross_policy.get("min_cases", 30))
        min_delta = float(cross_policy.get("min_delta_pp", 10))
        ci_low = boot.get("ci_low")
        ok = (
            delta is not None
            and float(delta) * 100 >= min_delta
            and n >= min_cases
            and ci_low is not None
            and float(ci_low) > 0
        )
        checks.append(
            {
                "name": "cross_turn_l2_vs_l1",
                "passed": ok,
                "value": {"delta_pp": None if delta is None else float(delta) * 100, "n": n, "ci_low": ci_low},
            }
        )
        if not ok:
            reasons.append(
                f"cross-turn L2 claim failed: delta={delta} n={n} ci_low={ci_low}"
            )

    latency_policy = quality.get("p95_latency_vs_active") or {}
    if latency_policy:
        baseline_latency = l1.get("p95_latency_ms")
        candidate_latency = l1l2.get("p95_latency_ms")
        max_ratio = float(latency_policy.get("max_increase_ratio", 0.25))
        ratio = None
        if baseline_latency is not None and float(baseline_latency) > 0 and candidate_latency is not None:
            ratio = (float(candidate_latency) - float(baseline_latency)) / float(baseline_latency)
        ok = ratio is not None and ratio <= max_ratio
        checks.append(
            {
                "name": "p95_latency_vs_l1_baseline",
                "passed": ok,
                "value": ratio,
                "threshold": max_ratio,
            }
        )
        if not ok:
            reasons.append(f"p95 latency comparison failed: increase_ratio={ratio}")

    grounded_policy = quality.get("grounded_l2_human_precision") or {}
    if grounded_policy:
        extraction = (summary.get("stage_details") or {}).get("extraction_quality") or {}
        human = ((extraction.get("metrics") or {}).get("human") or [])
        metric = next((m for m in human if m.get("name") == "grounded_precision_human"), {})
        value = metric.get("value")
        denominator = int(metric.get("denominator") or 0)
        min_value = float(grounded_policy.get("min", 0.90))
        min_sample = int(grounded_policy.get("min_sample", 50))
        ok = value is not None and float(value) >= min_value and denominator >= min_sample
        checks.append(
            {
                "name": "grounded_l2_human_precision",
                "passed": ok,
                "value": {"precision": value, "sample": denominator},
            }
        )
        if not ok:
            reasons.append(
                f"grounded L2 human precision unavailable/below gate: precision={value} sample={denominator}"
            )

    # Hybrid must not be used as pure-KU score
    hybrid = agg("hybrid")
    h_r = (hybrid.get("recall_at") or {}).get("5")
    if h_r is not None and r_l2 is not None and h_r > (r_l2 or 0) + 1e-9:
        checks.append(
            {
                "name": "hybrid_vs_pure_attribution",
                "passed": True,
                "value": {"hybrid_r5": h_r, "pure_r5": r_l2},
                "notes": "hybrid higher than pure-KU — report layer attribution; do not claim pure-KU",
            }
        )

    passed = not reasons and all(c.get("passed", True) for c in checks if "passed" in c)
    # recompute: any failed check => fail
    if any(c.get("passed") is False for c in checks):
        passed = False
    if reasons:
        passed = False

    verdict = "PASS" if passed else "FAIL"
    active_before = read_active_pointer()
    return {
        "generated_at": _utc(),
        "verdict": verdict,
        "passed": passed,
        "claim": claim,
        "reasons": reasons,
        "checks": checks,
        "policy_version": policy.get("version", "v1"),
        "active_collection_before": active_before,
        "active_collection_after": active_before,  # gate never changes active
        "candidate_collection": candidate_collection or summary.get("candidate_collection"),
        "checksum": content_checksum({"verdict": verdict, "checks": checks}),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Gate knowledge candidate from eval summary")
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument(
        "--policy",
        type=Path,
        default=ROOT
        / "assets"
        / "evals"
        / "knowledge_units"
        / "eval_policy_v1.yaml",
    )
    p.add_argument("--candidate", default="")
    p.add_argument("--candidate-checksum", default="")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--no-require-answer", action="store_true")
    args = p.parse_args(argv)

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    policy = load_policy(args.policy) if args.policy.exists() else {"version": "v1"}
    gate = evaluate_gate(
        summary,
        policy,
        candidate_collection=args.candidate,
        candidate_checksum=args.candidate_checksum,
        require_answer=not args.no_require_answer,
    )
    out = args.out or args.summary.with_name("gate.json")
    dump_json(out, gate)
    print(f"[gate] {gate['verdict']} reasons={len(gate['reasons'])} -> {out}")
    for r in gate["reasons"]:
        print(f"  - {r}")
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
