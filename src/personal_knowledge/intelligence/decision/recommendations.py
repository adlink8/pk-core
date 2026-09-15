"""Versioned deterministic recommendation rules with explicit abstention."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .schema import CognitionReference, RecommendationDraft


class RecommendationPolicyError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RecommendationPolicyError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise RecommendationPolicyError("invalid_time", f"{field}:timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RecommendationRule:
    rule_id: str
    version: str
    eligible_cognition_types: frozenset[str]
    minimum_evidence: int
    max_evidence_age_seconds: int
    domain: str
    recommendation_kind: str
    uncertainty_behavior: str
    contraindications: frozenset[str]
    expiry_seconds: int

    def __post_init__(self) -> None:
        if not self.rule_id or not self.version:
            raise RecommendationPolicyError("rule_identity_required")
        if not self.eligible_cognition_types <= {"fact", "observation", "inference"}:
            raise RecommendationPolicyError("invalid_cognition_type")
        if self.minimum_evidence < 1 or self.max_evidence_age_seconds < 0 or self.expiry_seconds <= 0:
            raise RecommendationPolicyError("invalid_rule_bound")
        if self.uncertainty_behavior not in {"abstain", "allow"}:
            raise RecommendationPolicyError("invalid_uncertainty_behavior")


@dataclass(frozen=True)
class RecommendationInput:
    subject: str
    scope: str
    target: str
    horizon: str
    expected_benefit: str
    rationale_codes: tuple[str, ...]
    costs_constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    support: tuple[CognitionReference, ...]
    observed_at: str
    uncertainty: str
    conflicting: bool
    contraindications: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationEvaluation:
    policy_id: str
    policy_version: str
    reason_code: str
    draft: RecommendationDraft | None


class RecommendationRuleRegistry:
    """An immutable registry; duplicate identities are rejected at construction."""

    def __init__(self, rules: Iterable[RecommendationRule]) -> None:
        values = tuple(rules)
        index = {(rule.rule_id, rule.version): rule for rule in values}
        if len(index) != len(values):
            raise RecommendationPolicyError("duplicate_rule_identity")
        self._rules = values
        self._index = index

    @property
    def rules(self) -> tuple[RecommendationRule, ...]:
        return self._rules

    def resolve(self, rule_id: str, version: str) -> RecommendationRule:
        try:
            return self._index[(rule_id, version)]
        except KeyError as exc:
            raise RecommendationPolicyError("rule_not_found", f"{rule_id}:{version}") from exc


def evaluate_rule(
    rule: RecommendationRule,
    input_value: RecommendationInput,
    *,
    now: datetime,
) -> RecommendationEvaluation:
    """Evaluate metadata-only Phase 25 references and return a proposal or abstention."""
    now = now.astimezone(timezone.utc)
    if len(input_value.support) < rule.minimum_evidence:
        return RecommendationEvaluation(rule.rule_id, rule.version, "insufficient_evidence", None)
    if any(ref.cognitive_type not in rule.eligible_cognition_types for ref in input_value.support):
        return RecommendationEvaluation(rule.rule_id, rule.version, "ineligible_cognition_type", None)
    anchor = input_value.support[0]
    if any(
        ref.source_run_id != anchor.source_run_id
        or ref.source_run_checksum != anchor.source_run_checksum
        or ref.source_publication_sequence != anchor.source_publication_sequence
        or ref.snapshot_id != anchor.snapshot_id
        or ref.snapshot_hash != anchor.snapshot_hash
        for ref in input_value.support
    ):
        return RecommendationEvaluation(rule.rule_id, rule.version, "cross_snapshot_support", None)
    observed = _time(input_value.observed_at, "observed_at")
    age = (now - observed).total_seconds()
    if age < 0 or age > rule.max_evidence_age_seconds:
        return RecommendationEvaluation(rule.rule_id, rule.version, "stale_evidence", None)
    if input_value.conflicting:
        return RecommendationEvaluation(rule.rule_id, rule.version, "conflicting_evidence", None)
    if input_value.uncertainty.strip() and rule.uncertainty_behavior == "abstain":
        return RecommendationEvaluation(rule.rule_id, rule.version, "uncertain_evidence", None)
    matched = sorted(set(input_value.contraindications) & rule.contraindications)
    if matched:
        return RecommendationEvaluation(rule.rule_id, rule.version, "contraindicated", None)
    expires_at = _stamp(now + timedelta(seconds=rule.expiry_seconds))
    draft = RecommendationDraft(
        subject=input_value.subject,
        domain=rule.domain,
        scope=input_value.scope,
        recommendation_kind=rule.recommendation_kind,
        target=input_value.target,
        horizon=input_value.horizon,
        rationale_codes=input_value.rationale_codes,
        expected_benefit=input_value.expected_benefit,
        costs_constraints=input_value.costs_constraints,
        assumptions=input_value.assumptions,
        contraindications=input_value.contraindications,
        confidence=1.0,
        uncertainty=input_value.uncertainty,
        expires_at=expires_at,
        support=input_value.support,
    )
    return RecommendationEvaluation(rule.rule_id, rule.version, "eligible", draft)


__all__ = [
    "RecommendationEvaluation",
    "RecommendationInput",
    "RecommendationPolicyError",
    "RecommendationRule",
    "RecommendationRuleRegistry",
    "evaluate_rule",
]
