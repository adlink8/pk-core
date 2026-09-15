"""Calibrate retrieval abstention thresholds on the private development set only."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from personal_knowledge.core.project_paths import ROOT
from personal_knowledge.evaluation.eval_contracts import load_cases_jsonl
from personal_knowledge.evaluation.knowledge_eval_metrics import CaseScore
from personal_knowledge.evaluation.run_knowledge_eval import load_config, stage_retrieval
from personal_knowledge.retrieval.evidence import EvidenceResolver

DEV_SET = ROOT / "var" / "runtime" / "private_evals" / "abstention_dev_v1.private.jsonl"
CONFIG = ROOT / "assets" / "evals" / "knowledge_units" / "eval_v1.yaml"
WORK_DIR = ROOT / "var" / "runtime" / "private_evals" / "abstention_calibration_v1"
REPORT = ROOT / "var" / "reports" / "analysis" / "evaluations" / "abstention_calibration_v1.json"


@dataclass
class ThresholdResult:
    threshold: float | None
    positive_n: int
    negative_n: int
    positive_retention: float | None
    negative_fp_rate: float | None
    passed: bool


@dataclass
class EvidencePolicyResult:
    positive_n: int
    invalid_positive_n: int
    negative_n: int
    positive_result_retention: float | None
    negative_emission_fp_rate: float | None
    passed: bool


def _eligible_positive(case, resolver: EvidenceResolver) -> bool:
    if case.expected_abstain:
        return False
    statuses: list[str] = []
    for unit_id in case.gold_unit_ids:
        unit = resolver.resolve(unit_id, artifact_type="knowledge_unit")
        primary_ref = str((unit.get("metadata") or {}).get("source_message_ref") or "")
        refs = [primary_ref] if primary_ref else list(unit.get("evidence_refs") or [])
        statuses.extend(
            str(resolver.resolve(ref, artifact_type="canonical_message").get("status") or "")
            for ref in refs
        )
    return bool(statuses) and all(status == "ok" for status in statuses)


def evaluate_evidence_policy(cases, ranked_by_case, eligible_positive_ids: set[str]) -> EvidencePolicyResult:
    positive_n = negative_n = positive_supported = negative_supported = 0
    invalid_positive_n = 0
    for case, ranked in zip(cases, ranked_by_case):
        emitted = bool(ranked)
        if case.expected_abstain:
            negative_n += 1
            negative_supported += int(emitted)
        else:
            if case.id not in eligible_positive_ids:
                invalid_positive_n += 1
                continue
            positive_n += 1
            positive_supported += int(emitted)
    retention = positive_supported / positive_n if positive_n else None
    fp_rate = negative_supported / negative_n if negative_n else None
    return EvidencePolicyResult(
        positive_n=positive_n,
        invalid_positive_n=invalid_positive_n,
        negative_n=negative_n,
        positive_result_retention=retention,
        negative_emission_fp_rate=fp_rate,
        passed=bool(
            retention is not None and fp_rate is not None
            and retention >= 0.80 and fp_rate <= 0.10
        ),
    )


def select_threshold(
    scores: Sequence[CaseScore],
    *,
    max_negative_fp: float = 0.10,
    min_positive_retention: float = 0.80,
) -> ThresholdResult:
    positives = [
        float(score.top_score)
        for score in scores
        if not score.expected_abstain and score.top_score is not None
    ]
    negatives = [
        float(score.top_score)
        for score in scores
        if score.expected_abstain and score.top_score is not None
    ]
    if not positives or not negatives:
        return ThresholdResult(None, len(positives), len(negatives), None, None, False)
    candidates = sorted(set(positives + negatives))
    candidates.append(max(candidates) + 1e-9)
    feasible: list[tuple[float, float, float]] = []
    for threshold in candidates:
        positive_retention = sum(value >= threshold for value in positives) / len(positives)
        negative_fp = sum(value >= threshold for value in negatives) / len(negatives)
        if negative_fp <= max_negative_fp:
            feasible.append((positive_retention, -threshold, negative_fp))
    if not feasible:
        return ThresholdResult(None, len(positives), len(negatives), 0.0, 1.0, False)
    positive_retention, negative_threshold, negative_fp = max(feasible)
    threshold = -negative_threshold
    return ThresholdResult(
        threshold=threshold,
        positive_n=len(positives),
        negative_n=len(negatives),
        positive_retention=positive_retention,
        negative_fp_rate=negative_fp,
        passed=positive_retention >= min_positive_retention,
    )


def run() -> dict:
    cases = load_cases_jsonl(DEV_SET)
    cfg = load_config(CONFIG)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    retrieval = stage_retrieval(cases, cfg, WORK_DIR, offline=False)
    evidence_resolver = EvidenceResolver()
    eligible_positive_ids = {
        case.id
        for case in cases
        if not case.expected_abstain and _eligible_positive(case, evidence_resolver)
    }
    invalid_positive_ids = sorted(
        case.id
        for case in cases
        if not case.expected_abstain and case.id not in eligible_positive_ids
    )
    modes = {}
    for mode, scores in (retrieval.get("mode_scores") or {}).items():
        modes[mode] = {
            "score_threshold_diagnostic": asdict(select_threshold(scores)),
            "evidence_policy": asdict(
                evaluate_evidence_policy(
                    cases,
                    (retrieval.get("mode_ranked") or {}).get(mode) or [],
                    eligible_positive_ids,
                )
            ),
        }
    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": "private:abstention_dev_v1",
        "case_count": len(cases),
        "constraints": {"max_negative_fp": 0.10, "min_positive_retention": 0.80},
        "modes": modes,
        "deployment_policy": "evidence_support_v1",
        "similarity_threshold_deployed": None,
        "serving_snapshot": retrieval.get("serving_snapshot"),
        "gold_eligibility_audit": {
            "eligible_positive_case_ids": sorted(eligible_positive_ids),
            "invalid_positive_case_ids": invalid_positive_ids,
            "invalid_reason": "gold knowledge unit primary canonical evidence is not currently eligible",
        },
        "passed": bool(modes) and all(
            value["evidence_policy"]["passed"] for value in modes.values()
        ),
        "note": "Development-only calibration; score thresholds remain diagnostic and are never deployed.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
