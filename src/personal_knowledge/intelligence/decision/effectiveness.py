"""Versioned observational assessment and bounded cohort summaries.

This module is deliberately pure: it compares declared measurements and emits
non-causal inferences.  It neither edits policy nor rewrites prior records.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterable

from .schema import OutcomeObservation, checksum


@dataclass(frozen=True)
class EffectivenessRule:
    rule_id: str
    version: str
    metric: str
    unit: str
    direction: str
    minimum_window_seconds: int


@dataclass(frozen=True)
class EffectivenessAssessment:
    assessment_id: str
    recommendation_id: str
    recommendation_checksum: str
    outcome_id: str
    outcome_checksum: str
    cognitive_type: str
    verdict: str
    causal_claim: bool
    rule_id: str
    rule_version: str
    input_checksums: tuple[str, ...]
    limitations: tuple[str, ...]
    confidence: float
    uncertainty: tuple[str, ...]


@dataclass(frozen=True)
class CohortSummary:
    cohort_key: tuple[str, str, str]
    status: str
    minimum_sample: int
    sample_count: int
    verdict_counts: tuple[tuple[str, int], ...]
    effectiveness_rate: float | None
    confidence_interval: tuple[float, float] | None
    uncertainty: tuple[str, ...]
    causal_claim: bool


_EFFECTIVENESS_RULES: dict[tuple[str, str], EffectivenessRule] = {
    ("observed_goal_attainment", "1"): EffectivenessRule(
        "observed_goal_attainment", "1", "focus_blocks", "count/week", "increase", 86400
    ),
    ("goal-attainment", "1"): EffectivenessRule(
        "goal-attainment", "1", "progress", "count", "increase", 86400
    ),
}


def registered_effectiveness_rule(rule_id: str, version: str) -> EffectivenessRule:
    """Resolve the immutable rule definition allowed at the persistence boundary."""
    try:
        return _EFFECTIVENESS_RULES[(rule_id, version)]
    except KeyError as exc:
        raise ValueError("unknown_effectiveness_rule") from exc


def outcome_from_payload(
    outcome_id: str,
    payload: dict[str, object],
    payload_checksum: str,
) -> OutcomeObservation:
    """Hydrate a verified outcome payload without trusting caller-owned objects."""
    return OutcomeObservation(
        outcome_id=outcome_id,
        recommendation_id=str(payload["recommendation_id"]),
        recommendation_checksum=str(payload["recommendation_checksum"]),
        action_id=str(payload["action_id"]),
        action_checksum=str(payload["action_checksum"]),
        source_class=str(payload["source_class"]),
        measurement_definition=str(payload["measurement_definition"]),
        metric=str(payload["metric"]),
        baseline_value=payload["baseline_value"],
        target_value=payload["target_value"],
        observed_value=payload["observed_value"],
        unit=str(payload["unit"]),
        direction=str(payload["direction"]),
        window_start=str(payload["window_start"]),
        window_end=str(payload["window_end"]),
        adherence_status=str(payload["adherence_status"]),
        evidence_refs=tuple(payload["evidence_refs"]),
        confidence=float(payload["confidence"]),
        uncertainty=tuple(payload["uncertainty"]),
        confounders=tuple(payload["confounders"]),
        concurrent_actions=tuple(payload["concurrent_actions"]),
        payload_checksum=payload_checksum,
    )


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def assess_outcome(
    outcome: OutcomeObservation,
    rule: EffectivenessRule,
    *,
    action_state: str,
) -> EffectivenessAssessment:
    """Assess observed goal attainment without inferring causal impact."""
    limitations: list[str] = []
    if action_state != "completed":
        limitations.append("action_not_completed")
    if outcome.adherence_status != "adhered":
        limitations.append("non_adherent" if outcome.adherence_status == "non_adherent" else "adherence_unknown")
    if outcome.metric != rule.metric:
        limitations.append("metric_mismatch")
    if outcome.unit != rule.unit:
        limitations.append("unit_mismatch")
    if outcome.direction != rule.direction:
        limitations.append("direction_mismatch")
    if outcome.baseline_value is None:
        limitations.append("missing_baseline")
    if outcome.target_value is None:
        limitations.append("missing_target")
    if outcome.observed_value is None:
        limitations.append("missing_observed_value")
    start, end = _time(outcome.window_start), _time(outcome.window_end)
    if start is None or end is None or end <= start:
        limitations.append("invalid_window")
    elif (end - start).total_seconds() < rule.minimum_window_seconds:
        limitations.append("insufficient_window")
    if outcome.confounders:
        limitations.append("confounded")
    if outcome.concurrent_actions:
        limitations.append("concurrent_actions")
    if "selection_bias" in outcome.uncertainty:
        limitations.append("selection_bias")
    if not outcome.evidence_refs:
        limitations.append("missing_evidence_refs")

    verdict = "inconclusive"
    if not limitations:
        baseline = float(outcome.baseline_value)  # guarded above
        target = float(outcome.target_value)
        observed = float(outcome.observed_value)
        if not all(math.isfinite(value) for value in (baseline, target, observed)):
            limitations.append("non_finite_measurement")
        elif rule.direction == "increase":
            verdict = "effective" if observed >= target else "ineffective" if observed <= baseline else "mixed"
        elif rule.direction == "decrease":
            verdict = "effective" if observed <= target else "ineffective" if observed >= baseline else "mixed"
        elif rule.direction == "maintain":
            verdict = "effective" if observed == target else "ineffective" if abs(observed - target) > abs(baseline - target) else "mixed"
        else:
            limitations.append("unsupported_rule_direction")

    limitations_tuple = tuple(sorted(set(limitations)))
    if limitations_tuple:
        verdict = "inconclusive"
    fields = {
        "recommendation_id": outcome.recommendation_id,
        "recommendation_checksum": outcome.recommendation_checksum,
        "outcome_id": outcome.outcome_id,
        "outcome_checksum": outcome.payload_checksum,
        "cognitive_type": "inference",
        "verdict": verdict,
        "causal_claim": False,
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "input_checksums": (outcome.action_checksum, outcome.payload_checksum),
        "limitations": limitations_tuple,
        "confidence": outcome.confidence if verdict != "inconclusive" else 0.0,
        "uncertainty": tuple(sorted(set((*outcome.uncertainty, "observational_only", *limitations_tuple)))),
    }
    return EffectivenessAssessment(
        assessment_id=f"dea_{checksum(fields)[:24]}",
        recommendation_id=outcome.recommendation_id,
        recommendation_checksum=outcome.recommendation_checksum,
        outcome_id=outcome.outcome_id,
        outcome_checksum=outcome.payload_checksum,
        cognitive_type="inference",
        verdict=verdict,
        causal_claim=False,
        rule_id=rule.rule_id,
        rule_version=rule.version,
        input_checksums=(outcome.action_checksum, outcome.payload_checksum),
        limitations=limitations_tuple,
        confidence=fields["confidence"],
        uncertainty=fields["uncertainty"],
    )


def summarize_assessments(
    assessments: Iterable[EffectivenessAssessment],
    *,
    policy_version: str,
    domain: str,
    recommendation_kind: str,
    minimum_sample: int,
) -> CohortSummary:
    """Return a named, read-only observational cohort aggregate."""
    if minimum_sample < 1:
        raise ValueError("minimum_sample must be positive")
    rows = tuple(assessments)
    if any(row.causal_claim or row.cognitive_type != "inference" for row in rows):
        raise ValueError("invalid assessment cohort")
    counts = {name: 0 for name in ("effective", "ineffective", "mixed", "inconclusive")}
    for row in rows:
        if row.verdict not in counts:
            raise ValueError("invalid assessment verdict")
        counts[row.verdict] += 1
    eligible = len(rows) >= minimum_sample
    rate = counts["effective"] / len(rows) if eligible and rows else None
    interval = None
    if rate is not None:
        # Wilson 95% interval exposes sampling uncertainty without an opaque score.
        n, z = len(rows), 1.96
        center = (rate + z * z / (2 * n)) / (1 + z * z / n)
        radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        interval = (max(0.0, center - radius), min(1.0, center + radius))
    return CohortSummary(
        cohort_key=(policy_version, domain, recommendation_kind),
        status="observational_summary" if eligible else "insufficient_sample",
        minimum_sample=minimum_sample,
        sample_count=len(rows),
        verdict_counts=tuple(sorted(counts.items())),
        effectiveness_rate=rate,
        confidence_interval=interval,
        uncertainty=("observational_only",) if eligible else ("insufficient_sample", "observational_only"),
        causal_claim=False,
    )


def load_outcome(db_path: Path, outcome_id: str) -> OutcomeObservation:
    """Hydrate one immutable outcome after recomputing its checksum."""
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        row = con.execute(
            "SELECT payload_json,payload_checksum FROM decision_outcomes WHERE outcome_id=?", (outcome_id,)
        ).fetchone()
        if row is None:
            raise ValueError("outcome_missing")
        payload = json.loads(str(row["payload_json"]))
        if checksum(payload) != str(row["payload_checksum"]):
            raise ValueError("outcome_checksum_mismatch")
        return outcome_from_payload(outcome_id, payload, str(row["payload_checksum"]))
    finally:
        con.close()


__all__ = [
    "CohortSummary", "EffectivenessAssessment", "EffectivenessRule", "OutcomeObservation",
    "assess_outcome", "load_outcome", "outcome_from_payload", "registered_effectiveness_rule",
    "summarize_assessments",
]
