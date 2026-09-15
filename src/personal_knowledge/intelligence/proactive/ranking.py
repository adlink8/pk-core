"""Pure, versioned proactive importance and notification-noise governance."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import (
    CANONICAL_DOMAINS, CandidateDraft, ImportanceVector, ProactiveCandidate,
    ProactiveEvaluation, canonical_domain, checksum, validate_metadata_payload,
)

CANDIDATE_CLASSES = frozenset({
    "important_change", "goal_conflict", "deadline_risk", "stalled_project",
    "cross_domain_opportunity", "outcome_followup", "trust_attention",
})
PRESENTATION_KINDS = frozenset({"inbox_item", "digest_item"})


@dataclass(frozen=True)
class RankingPolicy:
    policy_id: str
    version: str
    threshold: float
    weights: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class NoisePolicy:
    policy_id: str
    version: str
    cooldown_hours: int
    quiet_start_hour: int
    quiet_end_hour: int
    global_budget: int
    domain_budget: int
    critical_threshold: float
    critical_budget_override: bool = True


@dataclass(frozen=True)
class SurfaceRecord:
    dedup_key: str
    event_type: str
    occurred_at: str


@dataclass(frozen=True)
class EvaluationContext:
    as_of: str
    timezone: str
    window_start: str
    window_end: str
    surface_records: tuple[SurfaceRecord, ...] = ()
    explicit_suppressions: tuple[str, ...] = ()

    @classmethod
    def fixed(cls, *, surface_records: tuple[SurfaceRecord, ...] = ()) -> "EvaluationContext":
        return cls("2026-07-18T12:00:00Z", "UTC", "2026-07-18T00:00:00Z",
                   "2026-07-19T00:00:00Z", surface_records)


DEFAULT_RANKING_POLICY = RankingPolicy(
    "importance", "v1", 0.55,
    (("severity", .18), ("urgency", .18), ("goal_impact", .14),
     ("cross_domain_impact", .10), ("novelty", .15), ("evidence_strength", .15),
     ("user_relevance", .08), ("outcome_signal", .02)),
)
DEFAULT_NOISE_POLICY = NoisePolicy("noise", "v1", 24, 22, 7, 3, 2, .95)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _bounded(value: float, name: str) -> float:
    if not 0 <= value <= 1:
        raise ValueError(f"importance_component_invalid:{name}")
    return round(float(value), 6)


def _validate_policy(policy: RankingPolicy) -> None:
    if not policy.policy_id or not policy.version:
        raise ValueError("policy_version_invalid")
    names = tuple(name for name, _ in policy.weights)
    required = ("severity", "urgency", "goal_impact", "cross_domain_impact", "novelty",
                "evidence_strength", "user_relevance", "outcome_signal")
    if names != required or abs(sum(weight for _, weight in policy.weights) - 1.0) > 1e-9:
        raise ValueError("policy_weights_invalid")


def _material_signature(draft: CandidateDraft) -> str:
    def band(value: float) -> int:
        return 0 if value < .34 else 1 if value < .67 else 2
    return checksum({
        "severity_band": band(draft.severity), "urgency_band": band(draft.urgency),
        "eligible_evidence": sorted((ref.authority_id, ref.record_type, ref.record_id, ref.record_checksum)
                                    for ref in draft.support_refs),
        "goal_relation_version": draft.goal_relation_version,
    })


def _validate_draft(draft: CandidateDraft) -> tuple[str, ...]:
    if draft.candidate_class not in CANDIDATE_CLASSES:
        raise ValueError(f"candidate_class_invalid:{draft.candidate_class}")
    if draft.presentation_kind not in PRESENTATION_KINDS:
        raise ValueError(f"presentation_kind_invalid:{draft.presentation_kind}")
    if not draft.support_refs:
        raise ValueError("support_required")
    domains = tuple(sorted({canonical_domain(item) for item in draft.domains}, key=CANONICAL_DOMAINS.index))
    snapshots = {(ref.snapshot_id, ref.snapshot_hash) for ref in draft.support_refs}
    if len(snapshots) != 1:
        raise ValueError("mixed_snapshot_support")
    for ref in draft.support_refs:
        if len(ref.record_checksum) != 64 or len(ref.source_run_checksum) != 64:
            raise ValueError("support_checksum_invalid")
    validate_metadata_payload(draft.metadata or {}, "candidate.metadata")
    _time(draft.valid_from); _time(draft.expires_at)
    if _time(draft.expires_at) <= _time(draft.valid_from):
        raise ValueError("candidate_window_invalid")
    return domains


def rank_candidates(
    drafts: Iterable[CandidateDraft], *, policy: RankingPolicy,
    prior_candidates: tuple[ProactiveCandidate, ...] = (), run_id: str = "preview",
    prior_dedup_keys: frozenset[str] = frozenset(), expired_prior_keys: frozenset[str] = frozenset(),
) -> tuple[ProactiveCandidate, ...]:
    """Construct stable metadata-only candidates; no eligibility veto is scored away."""
    _validate_policy(policy)
    prior = {item.dedup_key for item in prior_candidates} | set(prior_dedup_keys)
    results: list[ProactiveCandidate] = []
    for draft in drafts:
        domains = _validate_draft(draft)
        material = _material_signature(draft)
        dedup_key = checksum({
            "candidate_class": draft.candidate_class, "target_group": sorted(draft.target_group),
            "domains": domains, "scope": draft.scope, "material_change_signature": material,
            "policy_version": policy.version,
        })
        cooldown_key = checksum({"candidate_class": draft.candidate_class,
                                 "subject": draft.subject, "scope": draft.scope,
                                 "domains": domains, "policy_version": policy.version})
        novelty = 0.0 if dedup_key in prior and dedup_key not in expired_prior_keys else 1.0
        values: Mapping[str, float] = {
            "severity": _bounded(draft.severity, "severity"),
            "urgency": _bounded(draft.urgency, "urgency"),
            "goal_impact": _bounded(draft.goal_impact, "goal_impact"),
            "cross_domain_impact": _bounded(draft.cross_domain_impact, "cross_domain_impact"),
            "novelty": novelty,
            "evidence_strength": _bounded(draft.evidence_strength, "evidence_strength"),
            "user_relevance": _bounded(draft.user_relevance, "user_relevance"),
            "outcome_signal": _bounded(draft.outcome_signal, "outcome_signal"),
        }
        final = round(sum(values[name] * weight for name, weight in policy.weights), 6)
        importance = ImportanceVector(**values, final_score=final)
        identity = {
            "run_id": run_id, "candidate_class": draft.candidate_class, "subject": draft.subject,
            "scope": draft.scope, "domains": domains, "dedup_key": dedup_key,
            "cooldown_key": cooldown_key,
            "valid_from": draft.valid_from, "expires_at": draft.expires_at,
            "policy_id": policy.policy_id, "policy_version": policy.version,
        }
        candidate_id = f"pcd_{checksum(identity)[:24]}"
        payload = {
            "schema_version": "proactive_candidate_v1", "candidate_id": candidate_id, **identity,
            "presentation_kind": draft.presentation_kind, "target_group": sorted(draft.target_group),
            "material_change_signature": material, "importance": asdict(importance),
            "uncertainty": draft.uncertainty, "reason_codes": sorted(set(draft.reason_codes)),
            "support": [asdict(ref) for ref in draft.support_refs],
            "fixture_label": "contract_fixture_not_real_usefulness", "metadata": draft.metadata or {},
        }
        validate_metadata_payload(payload, "candidate")
        results.append(ProactiveCandidate(
            candidate_id, run_id, draft.candidate_class, draft.presentation_kind, draft.subject,
            draft.scope, domains, tuple(sorted(draft.target_group)), dedup_key, cooldown_key, material,
            draft.valid_from, draft.expires_at, policy.policy_id, policy.version, importance,
            novelty, draft.uncertainty, tuple(sorted(set(draft.reason_codes))),
            draft.evidence_eligible, draft.trust_eligible, draft.sensitive, draft.support_refs,
            "contract_fixture_not_real_usefulness", payload, checksum(payload),
        ))
    return tuple(sorted(results, key=lambda item: (-item.importance.final_score,
                                                    -item.importance.urgency, item.candidate_id)))


def _quiet_until(as_of: datetime, zone: ZoneInfo, policy: NoisePolicy) -> str | None:
    local = as_of.astimezone(zone)
    hour = local.hour
    if hour >= policy.quiet_start_hour:
        end = (local + timedelta(days=1)).replace(hour=policy.quiet_end_hour, minute=0, second=0, microsecond=0)
    elif hour < policy.quiet_end_hour:
        end = local.replace(hour=policy.quiet_end_hour, minute=0, second=0, microsecond=0)
    else:
        return None
    return end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evaluate_candidates(
    candidates: Iterable[ProactiveCandidate], *, context: EvaluationContext, policy: NoisePolicy,
    ranking_policy: RankingPolicy = DEFAULT_RANKING_POLICY,
) -> tuple[ProactiveEvaluation, ...]:
    if not policy.policy_id or not policy.version:
        raise ValueError("noise_policy_invalid")
    as_of = _time(context.as_of); start = _time(context.window_start); end = _time(context.window_end)
    if not start <= as_of < end:
        raise ValueError("evaluation_window_invalid")
    try:
        zone = ZoneInfo(context.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    surfaced: dict[str, datetime] = {}
    for record in context.surface_records:
        if record.event_type in {"presented", "acknowledged"}:
            surfaced[record.dedup_key] = max(surfaced.get(record.dedup_key, datetime.min.replace(tzinfo=timezone.utc)), _time(record.occurred_at))
    ordered = sorted(candidates, key=lambda item: (-item.importance.final_score,
                                                   -item.importance.urgency, item.candidate_id))
    global_used = 0
    domain_used = {domain: 0 for domain in CANONICAL_DOMAINS}
    output: list[ProactiveEvaluation] = []
    for candidate in ordered:
        result, reasons, deferred = "eligible", (), None
        if candidate.sensitive:
            result, reasons = "abstained", ("privacy_veto",)
        elif not candidate.evidence_eligible:
            result, reasons = "abstained", ("evidence_veto",)
        elif not candidate.trust_eligible or candidate.dedup_key in context.explicit_suppressions:
            result, reasons = "abstained", ("trust_veto",)
        elif as_of >= _time(candidate.expires_at):
            result, reasons = "expired", ("expired",)
        elif zone is None and candidate.presentation_kind != "inbox_item":
            result, reasons = "abstained", ("invalid_timezone_inbox_only",)
        elif candidate.importance.final_score < ranking_policy.threshold:
            result, reasons = "abstained", ("below_threshold",)
        elif candidate.novelty == 0:
            result, reasons = "suppressed", ("duplicate_no_material_change",)
        elif candidate.cooldown_key in surfaced and as_of - surfaced[candidate.cooldown_key] < timedelta(hours=policy.cooldown_hours):
            result, reasons = "suppressed", ("cooldown_active",)
        elif zone is not None and (quiet_until := _quiet_until(as_of, zone, policy)) is not None:
            result, reasons, deferred = "deferred", ("quiet_period",), quiet_until
        else:
            critical = candidate.importance.final_score >= policy.critical_threshold and policy.critical_budget_override
            exhausted = tuple(domain for domain in candidate.domains if domain_used[domain] >= policy.domain_budget)
            if global_used >= policy.global_budget and not critical:
                result, reasons = "suppressed", ("global_budget_exhausted",)
            elif exhausted and not critical:
                result, reasons = "suppressed", ("domain_budget_exhausted",) + exhausted
            else:
                global_used += 1
                for domain in candidate.domains:
                    domain_used[domain] += 1
        state = {
            "as_of": context.as_of, "timezone": context.timezone,
            "window_start": context.window_start, "window_end": context.window_end,
            "surface_records": [asdict(item) for item in context.surface_records],
            "explicit_suppressions": sorted(context.explicit_suppressions),
            "global_used": global_used, "domain_used": domain_used,
        }
        state_checksum = checksum(state)
        identity = {"candidate_id": candidate.candidate_id, "policy_id": policy.policy_id,
                    "policy_version": policy.version, "window_start": context.window_start,
                    "window_end": context.window_end}
        evaluation_id = f"pev_{checksum(identity)[:24]}"
        payload = {"schema_version": "proactive_evaluation_v1", "evaluation_id": evaluation_id,
                   **identity, "result": result, "reason_codes": reasons,
                   "deferred_until": deferred, "state_checksum": state_checksum}
        output.append(ProactiveEvaluation(evaluation_id, candidate.candidate_id, policy.policy_id,
                                          policy.version, context.window_start, context.window_end,
                                          result, reasons, deferred, state_checksum, payload, checksum(payload)))
    return tuple(sorted(output, key=lambda item: item.candidate_id))


def build_digest(candidates: Iterable[ProactiveCandidate]) -> Mapping[str, object]:
    """Return a metadata-only grouping proposal without merging source evidence."""
    ordered = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    return {"presentation_kind": "digest_item", "candidate_ids": [item.candidate_id for item in ordered],
            "support_manifests": [[asdict(ref) for ref in item.support_refs] for item in ordered],
            "contradictory_evidence_merged": False, "payload_checksum": checksum([item.payload_checksum for item in ordered])}
