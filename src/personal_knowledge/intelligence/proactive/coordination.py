"""Deterministic, abstention-first coordination over eight explicit domains."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import combinations
from typing import Iterable

from .schema import (
    CANONICAL_DOMAINS, RESOURCE_TYPES, CoordinationDraft, GoalSignal,
    ResourceClaim, canonical_domain, checksum,
)

COORDINATION_RULES = {
    "goal_support": "v1", "goal_conflict": "v1", "dependency": "v1",
    "resource_competition": "v1", "risk_propagation": "v1", "opportunity": "v1",
}
SENSITIVE_DOMAINS = frozenset({"health", "finance", "relationship"})


@dataclass(frozen=True)
class Abstention:
    goal_ids: tuple[str, ...]
    reason_code: str
    detail_checksum: str


@dataclass(frozen=True)
class CoordinationResult:
    items: tuple[CoordinationDraft, ...]
    abstentions: tuple[Abstention, ...]


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _overlap(left: ResourceClaim, right: ResourceClaim) -> bool:
    return max(_time(left.horizon_start), _time(right.horizon_start)) < min(_time(left.horizon_end), _time(right.horizon_end))


def _abstain(ids: tuple[str, ...], code: str, detail: object = None) -> Abstention:
    return Abstention(ids, code, checksum({"goal_ids": ids, "reason": code, "detail": detail}))


def _validate_goal(goal: GoalSignal, as_of: datetime) -> str | None:
    canonical_domain(goal.domain)
    if not 0 <= goal.confidence <= 1:
        return "invalid_confidence"
    if _time(goal.observed_at) > as_of:
        return "future_observation"
    if goal.valid_to and _time(goal.valid_to) <= as_of:
        return "expired_input"
    if goal.unresolved_conflict:
        return "unresolved_source_conflict"
    if goal.sensitive or (goal.domain in SENSITIVE_DOMAINS and goal.confidence < 0.75):
        return "sensitive_or_insufficient"
    for resource in goal.resources:
        if resource.resource_type not in RESOURCE_TYPES or resource.amount < 0 or resource.capacity < 0:
            return "invalid_resource"
        if not resource.resource_id.strip():
            return "invalid_resource_identity"
        if ((resource.source.snapshot_id, resource.source.snapshot_hash,
             resource.source.source_run_id, resource.source.source_run_checksum)
                != (goal.support.snapshot_id, goal.support.snapshot_hash,
                    goal.support.source_run_id, goal.support.source_run_checksum)):
            return "resource_source_mismatch"
        if resource.resource_type == "budget" and not resource.declared_by_user:
            return "undeclared_financial_resource"
    return None


def coordinate_goals(goals: Iterable[GoalSignal], *, as_of: str) -> CoordinationResult:
    """Return byte-stable coordination items; never infer missing resource evidence."""
    current = _time(as_of)
    ordered = tuple(sorted(goals, key=lambda item: item.goal_id))
    abstentions: list[Abstention] = []
    eligible: list[GoalSignal] = []
    for goal in ordered:
        reason = _validate_goal(goal, current)
        if reason:
            abstentions.append(_abstain((goal.goal_id,), reason))
        else:
            eligible.append(goal)
    items: list[CoordinationDraft] = []
    for left, right in combinations(eligible, 2):
        ids = (left.goal_id, right.goal_id)
        if (left.support.snapshot_id, left.support.snapshot_hash) != (right.support.snapshot_id, right.support.snapshot_hash):
            abstentions.append(_abstain(ids, "cross_snapshot_input")); continue
        if left.support.source_run_id != right.support.source_run_id or left.support.source_run_checksum != right.support.source_run_checksum:
            abstentions.append(_abstain(ids, "cross_version_input")); continue
        shared: list[tuple[ResourceClaim, ResourceClaim]] = []
        incompatible = False
        for a in left.resources:
            for b in right.resources:
                if a.resource_type != b.resource_type:
                    continue
                if a.resource_id != b.resource_id:
                    incompatible = True
                    continue
                if a.unit != b.unit:
                    incompatible = True
                    continue
                if _overlap(a, b):
                    shared.append((a, b))
        over = [(a, b) for a, b in shared if a.amount + b.amount > min(a.capacity, b.capacity)]
        domains = tuple(sorted({canonical_domain(left.domain), canonical_domain(right.domain)}, key=CANONICAL_DOMAINS.index))
        if over:
            resources = tuple(resource for pair in over for resource in pair)
            source_refs = tuple(sorted(
                {ref for ref in (left.support, right.support, *(resource.source for resource in resources))},
                key=lambda ref: (ref.authority_id, ref.record_type, ref.record_id, ref.record_checksum),
            ))
            items.append(CoordinationDraft(
                relation_type="goal_conflict", subject=left.subject, scope=left.scope,
                domains=domains, valid_from=max(left.valid_from, right.valid_from),
                valid_to=min(v for v in (left.valid_to, right.valid_to) if v is not None) if left.valid_to or right.valid_to else None,
                observed_at=max(left.observed_at, right.observed_at), rule_id="bounded-resource-conflict",
                rule_version=COORDINATION_RULES["goal_conflict"], confidence=min(left.confidence, right.confidence),
                uncertainty="bounded resource demand exceeds declared capacity",
                source_refs=source_refs, resource_manifest=resources,
            ))
        elif left.target.strip() and left.target.strip().casefold() == right.target.strip().casefold() and left.domain != right.domain:
            items.append(CoordinationDraft(
                relation_type="opportunity", subject=left.subject, scope=left.scope,
                domains=domains, valid_from=max(left.valid_from, right.valid_from),
                valid_to=min(v for v in (left.valid_to, right.valid_to) if v is not None) if left.valid_to or right.valid_to else None,
                observed_at=max(left.observed_at, right.observed_at), rule_id="shared-explicit-target",
                rule_version=COORDINATION_RULES["opportunity"], confidence=min(left.confidence, right.confidence),
                uncertainty="explicit compatible target only", source_refs=(left.support, right.support),
                resource_manifest=tuple(resource for pair in shared for resource in pair),
            ))
        elif incompatible:
            same_type_and_unit = any(a.resource_type == b.resource_type and a.unit == b.unit
                                     for a in left.resources for b in right.resources)
            abstentions.append(_abstain(ids, "resource_identity_mismatch" if same_type_and_unit else "incompatible_resource_units"))
        elif left.resources and right.resources and not shared:
            abstentions.append(_abstain(ids, "incompatible_resource_horizons"))
        else:
            abstentions.append(_abstain(ids, "no_bounded_conflict_evidence"))
    items.sort(key=lambda item: (item.relation_type, item.domains, checksum(asdict(item))))
    abstentions.sort(key=lambda item: (item.goal_ids, item.reason_code, item.detail_checksum))
    return CoordinationResult(tuple(items), tuple(abstentions))
