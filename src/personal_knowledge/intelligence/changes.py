"""Deterministic, metadata-only change intelligence over typed projections.

The functions in this module are pure.  They compare immutable projections and
emit checksums, assertion IDs and evidence references; source bodies never enter
the change manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .schema import RISK_SEVERITIES, canonical_json, checksum
from .state_projection import FormationStep, ProjectedState, StateKey, StateProjection


CHANGE_ALGORITHM_VERSION = "personal_state_changes_v1"
BASE_CHANGE_RULE_ID = "typed_state_delta"
BASE_CHANGE_RULE_VERSION = "1"
BASE_CHANGE_TYPES = frozenset(
    {"created", "updated", "reaffirmed", "stale", "conflict", "resolved"}
)
INFERENCE_PROVENANCE = "inference"


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    version: str
    inference_type: str
    minimum_samples: int
    description: str


RULE_REGISTRY = MappingProxyType(
    {
        "ordered_numeric_trend": RuleSpec(
            rule_id="ordered_numeric_trend",
            version="1",
            inference_type="trend",
            minimum_samples=3,
            description="Endpoint direction over at least three ordered comparable observations.",
        ),
        "increasing_constraint_pressure": RuleSpec(
            rule_id="increasing_constraint_pressure",
            version="1",
            inference_type="risk",
            minimum_samples=3,
            description="An increasing constraint observation trend is a medium non-prescriptive risk.",
        ),
        "decreasing_goal_signal": RuleSpec(
            rule_id="decreasing_goal_signal",
            version="1",
            inference_type="risk",
            minimum_samples=3,
            description="A decreasing goal observation trend is a medium non-prescriptive risk.",
        ),
    }
)


class ChangeError(ValueError):
    """Stable fail-closed error for change derivation."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    change_type: str
    key: StateKey
    before_assertion_ids: tuple[str, ...]
    after_assertion_ids: tuple[str, ...]
    before_value_checksum: str | None
    after_value_checksum: str | None
    effective_at: str
    evidence_refs: tuple[str, ...]
    rule_id: str
    rule_version: str
    confidence: float
    uncertainty: tuple[str, ...]


@dataclass(frozen=True)
class ChangeSet:
    algorithm_version: str
    snapshot_id: str
    snapshot_hash: str
    before_as_of: str
    after_as_of: str
    records: tuple[ChangeRecord, ...]
    manifest_checksum: str


@dataclass(frozen=True)
class TrendSample:
    assertion_id: str
    key: StateKey
    value: float
    unit: str
    observed_at: str
    evidence_refs: tuple[str, ...]
    evidence_eligible: bool
    confidence: float
    uncertainty: tuple[str, ...] = ()


@dataclass(frozen=True)
class InferenceRecord:
    inference_id: str
    inference_type: str
    result_status: str
    key: StateKey
    provenance_class: str
    rule_id: str
    rule_version: str
    assertion_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    sample_count: int
    window_start: str | None
    window_end: str | None
    direction: str | None
    magnitude: float | None
    magnitude_method: str | None
    unit: str | None
    severity: str | None
    confidence: float
    uncertainty: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.severity is not None and self.severity not in RISK_SEVERITIES:
            raise ValueError(f"invalid risk severity: {self.severity}")


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ChangeError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise ChangeError("invalid_time", f"{field}:timezone_required")
    return parsed


def _ordered_steps(state: ProjectedState | None) -> tuple[FormationStep, ...]:
    if state is None:
        return ()
    return tuple(
        sorted(
            state.formation_path,
            key=lambda row: (
                _parse_time(row.valid_from, "valid_from"),
                _parse_time(row.observed_at, "observed_at"),
                row.assertion_id,
                row.run_id,
            ),
        )
    )


def _latest_steps(state: ProjectedState | None) -> tuple[FormationStep, ...]:
    ordered = _ordered_steps(state)
    if not ordered:
        return ()
    latest = max(
        (_parse_time(row.valid_from, "valid_from"), _parse_time(row.observed_at, "observed_at"))
        for row in ordered
    )
    return tuple(
        row
        for row in ordered
        if (
            _parse_time(row.valid_from, "valid_from"),
            _parse_time(row.observed_at, "observed_at"),
        )
        == latest
    )


def _assertion_ids(state: ProjectedState | None) -> tuple[str, ...]:
    if state is None:
        return ()
    if state.current_assertion_id:
        return (state.current_assertion_id,)
    return tuple(sorted(row.assertion_id for row in _latest_steps(state)))


def _evidence_refs(
    before: ProjectedState | None,
    after: ProjectedState | None,
) -> tuple[str, ...]:
    refs: set[str] = set()
    for state in (before, after):
        if state is None:
            continue
        refs.update(item.ref for item in state.evidence)
        refs.update(ref for row in _latest_steps(state) for ref in row.evidence_refs)
    return tuple(sorted(refs))


def _latest_time(state: ProjectedState | None, fallback: str) -> str:
    steps = _latest_steps(state)
    if not steps:
        return fallback
    return max(steps, key=lambda row: (_parse_time(row.valid_from, "valid_from"), _parse_time(row.observed_at, "observed_at"))).observed_at


def _has_evidenced_resolution(before: ProjectedState, after: ProjectedState) -> bool:
    before_steps = _latest_steps(before)
    after_steps = _latest_steps(after)
    if not after.current_assertion_id or not after_steps:
        return False
    before_moment = max(
        (
            _parse_time(row.valid_from, "valid_from"),
            _parse_time(row.observed_at, "observed_at"),
        )
        for row in before_steps
    ) if before_steps else None
    after_moment = max(
        (
            _parse_time(row.valid_from, "valid_from"),
            _parse_time(row.observed_at, "observed_at"),
        )
        for row in after_steps
    )
    later_assertion = before_moment is not None and after_moment > before_moment
    reviewed_event = any(
        trace.event_type in {"correct", "restore", "supersede", "rollback"}
        and bool(trace.event_id)
        for trace in after.lifecycle_path
    )
    return later_assertion or reviewed_event


def _classify(
    before: ProjectedState | None,
    after: ProjectedState | None,
) -> tuple[str, tuple[str, ...]] | None:
    if after is None:
        return None
    if after.status == "conflict":
        latest = _latest_steps(after)
        value_types = {row.value_type for row in latest if row.value_type}
        if (
            len(latest) >= 2
            and len(value_types) == 1
            and len({row.value_checksum for row in latest}) >= 2
        ):
            if before is None or before.status != "conflict":
                return "conflict", tuple(sorted(set(after.uncertainty)))
        return None
    if before is not None and before.status == "conflict":
        if after.status in {"current", "uncertain"} and _has_evidenced_resolution(before, after):
            return "resolved", tuple(sorted(set(before.uncertainty + after.uncertainty)))
        return None
    if before is None or not before.current_assertion_id:
        if after.current_assertion_id and after.status in {"current", "uncertain"}:
            return "created", tuple(sorted(set(after.uncertainty)))
        return None
    if after.status in {"stale", "expired"} and _latest_steps(after):
        return "stale", tuple(sorted(set(after.uncertainty + ("no_current_evidence",))))
    if not after.current_assertion_id or after.status not in {"current", "uncertain"}:
        return None
    if _value_type(before.current_value) != _value_type(after.current_value):
        return None
    before_value = canonical_json(before.current_value)
    after_value = canonical_json(after.current_value)
    change_type = "reaffirmed" if before_value == after_value else "updated"
    return change_type, tuple(sorted(set(before.uncertainty + after.uncertainty)))


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (tuple, list)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    raise ChangeError("unsupported_value_type", type(value).__name__)


def _record(
    change_type: str,
    before: ProjectedState | None,
    after: ProjectedState,
    *,
    effective_at: str,
    uncertainty: tuple[str, ...],
) -> ChangeRecord:
    before_checksum = (
        checksum(before.current_value)
        if before is not None and before.current_assertion_id
        else None
    )
    after_checksum = checksum(after.current_value) if after.current_assertion_id else None
    confidence_values = [
        value
        for value in (
            before.confidence if before is not None else None,
            after.confidence,
        )
        if value is not None
    ]
    fields = {
        "change_type": change_type,
        "key": after.key,
        "before_assertion_ids": _assertion_ids(before),
        "after_assertion_ids": _assertion_ids(after),
        "before_value_checksum": before_checksum,
        "after_value_checksum": after_checksum,
        "effective_at": effective_at,
        "evidence_refs": _evidence_refs(before, after),
        "rule_id": BASE_CHANGE_RULE_ID,
        "rule_version": BASE_CHANGE_RULE_VERSION,
        "confidence": min(confidence_values) if confidence_values else 0.0,
        "uncertainty": uncertainty,
    }
    return ChangeRecord(
        change_id=f"psc_{checksum({'algorithm_version': CHANGE_ALGORITHM_VERSION, **fields})[:24]}",
        **fields,
    )


def compare_projections(
    before: StateProjection,
    after: StateProjection,
    *,
    algorithm_version: str = CHANGE_ALGORITHM_VERSION,
) -> ChangeSet:
    """Compare two compatible projections and return a stable metadata manifest."""
    if algorithm_version != CHANGE_ALGORITHM_VERSION:
        raise ChangeError("unsupported_algorithm_version", algorithm_version)
    if (
        before.snapshot_id != after.snapshot_id
        or before.snapshot_hash != after.snapshot_hash
    ):
        raise ChangeError("incompatible_snapshot")
    if _parse_time(before.as_of, "before_as_of") > _parse_time(after.as_of, "after_as_of"):
        raise ChangeError("invalid_projection_order")
    before_by_key = {state.key: state for state in before.states}
    after_by_key = {state.key: state for state in after.states}
    if len(before_by_key) != len(before.states) or len(after_by_key) != len(after.states):
        raise ChangeError("duplicate_state_key")

    records: list[ChangeRecord] = []
    for key in sorted(set(before_by_key) | set(after_by_key)):
        before_state = before_by_key.get(key)
        after_state = after_by_key.get(key)
        if after_state is None:
            continue
        classified = _classify(before_state, after_state)
        if classified is None:
            continue
        change_type, uncertainty = classified
        # Comparing an identical projection boundary is a no-op, not a
        # reaffirmation.  A real reaffirmation must introduce a distinct
        # evidence-backed assertion with the same typed value.
        if (
            change_type == "reaffirmed"
            and _assertion_ids(before_state) == _assertion_ids(after_state)
        ):
            continue
        records.append(
            _record(
                change_type,
                before_state,
                after_state,
                effective_at=_latest_time(after_state, after.as_of),
                uncertainty=uncertainty,
            )
        )
    ordered = tuple(
        sorted(
            records,
            key=lambda row: (
                _parse_time(row.effective_at, "effective_at"),
                row.key,
                row.change_type,
                row.change_id,
            ),
        )
    )
    manifest = {
        "algorithm_version": algorithm_version,
        "snapshot_id": after.snapshot_id,
        "snapshot_hash": after.snapshot_hash,
        "before_as_of": before.as_of,
        "after_as_of": after.as_of,
        "records": ordered,
    }
    return ChangeSet(
        **manifest,
        manifest_checksum=checksum(manifest),
    )


def change_set_checksum(value: ChangeSet) -> str:
    """Recompute the manifest checksum for validation and replay tests."""
    return checksum(
        {
            "algorithm_version": value.algorithm_version,
            "snapshot_id": value.snapshot_id,
            "snapshot_hash": value.snapshot_hash,
            "before_as_of": value.before_as_of,
            "after_as_of": value.after_as_of,
            "records": value.records,
        }
    )


def _inference_record(**fields: Any) -> InferenceRecord:
    identity = {
        "algorithm_version": CHANGE_ALGORITHM_VERSION,
        **fields,
    }
    return InferenceRecord(
        inference_id=f"psi_{checksum(identity)[:24]}",
        **fields,
    )


def _uncertain_inference(
    *,
    inference_type: str,
    key: StateKey,
    rule: RuleSpec,
    samples: tuple[TrendSample, ...],
    reasons: Iterable[str],
) -> InferenceRecord:
    times = tuple(sample.observed_at for sample in samples)
    return _inference_record(
        inference_type=inference_type,
        result_status="uncertain",
        key=key,
        provenance_class=INFERENCE_PROVENANCE,
        rule_id=rule.rule_id,
        rule_version=rule.version,
        assertion_ids=tuple(sorted(sample.assertion_id for sample in samples)),
        evidence_refs=tuple(sorted({ref for sample in samples for ref in sample.evidence_refs})),
        sample_count=len(samples),
        window_start=min(times, key=lambda value: _parse_time(value, "observed_at")) if times else None,
        window_end=max(times, key=lambda value: _parse_time(value, "observed_at")) if times else None,
        direction=None,
        magnitude=None,
        magnitude_method=None,
        unit=None,
        severity=None,
        confidence=0.0,
        uncertainty=tuple(sorted(set(reasons))),
    )


def derive_trend(
    samples: Iterable[TrendSample],
    *,
    rule_id: str = "ordered_numeric_trend",
) -> InferenceRecord:
    """Apply the versioned numeric trend rule without promoting it to fact."""
    rule = RULE_REGISTRY.get(rule_id)
    if rule is None or rule.inference_type != "trend":
        raise ChangeError("unknown_trend_rule", rule_id)
    rows = tuple(
        sorted(
            samples,
            key=lambda row: (
                _parse_time(row.observed_at, "observed_at"),
                row.assertion_id,
            ),
        )
    )
    if not rows:
        raise ChangeError("trend_samples_required")
    key = rows[0].key
    reasons: list[str] = []
    if any(row.key != key for row in rows):
        reasons.append("incompatible_state_key")
    if len({row.assertion_id for row in rows}) != len(rows):
        reasons.append("duplicate_assertion")
    if len(rows) < rule.minimum_samples:
        reasons.append("insufficient_samples")
    units = {row.unit.strip() for row in rows if row.unit.strip()}
    if len(units) != 1 or any(not row.unit.strip() for row in rows):
        reasons.append("missing_or_incompatible_unit")
    if any(not row.evidence_eligible or not row.evidence_refs for row in rows):
        reasons.append("evidence_ineligible")
    if any(not 0.0 <= row.confidence <= 1.0 for row in rows):
        reasons.append("invalid_confidence")
    elif min(row.confidence for row in rows) < 0.6:
        reasons.append("weak_support")
    numeric_values_are_valid = all(
        not isinstance(row.value, bool)
        and isinstance(row.value, (int, float))
        and math.isfinite(float(row.value))
        for row in rows
    )
    values_by_time: dict[str, set[float]] = {}
    if numeric_values_are_valid:
        for row in rows:
            values_by_time.setdefault(row.observed_at, set()).add(float(row.value))
    else:
        reasons.append("invalid_numeric_value")
    if any(len(values) > 1 for values in values_by_time.values()):
        reasons.append("conflicting_inputs")
    if len({row.observed_at for row in rows}) < rule.minimum_samples:
        reasons.append("insufficient_ordered_samples")
    if reasons:
        return _uncertain_inference(
            inference_type="trend",
            key=key,
            rule=rule,
            samples=rows,
            reasons=(*reasons, *(reason for row in rows for reason in row.uncertainty)),
        )

    magnitude = float(rows[-1].value) - float(rows[0].value)
    direction = "up" if magnitude > 0 else "down" if magnitude < 0 else "stable"
    fields = {
        "inference_type": "trend",
        "result_status": "derived",
        "key": key,
        "provenance_class": INFERENCE_PROVENANCE,
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "assertion_ids": tuple(row.assertion_id for row in rows),
        "evidence_refs": tuple(sorted({ref for row in rows for ref in row.evidence_refs})),
        "sample_count": len(rows),
        "window_start": rows[0].observed_at,
        "window_end": rows[-1].observed_at,
        "direction": direction,
        "magnitude": magnitude,
        "magnitude_method": "endpoint_delta",
        "unit": rows[0].unit,
        "severity": None,
        "confidence": min(row.confidence for row in rows),
        "uncertainty": tuple(sorted({reason for row in rows for reason in row.uncertainty})),
    }
    return _inference_record(**fields)


def derive_risk(
    trend: InferenceRecord,
    *,
    rule_id: str,
) -> InferenceRecord:
    """Apply a named non-prescriptive risk rule to one derived trend."""
    rule = RULE_REGISTRY.get(rule_id)
    if rule is None or rule.inference_type != "risk":
        raise ChangeError("unknown_risk_rule", rule_id)
    reasons: list[str] = []
    expected_direction = {
        "increasing_constraint_pressure": ("constraint", "up"),
        "decreasing_goal_signal": ("goal", "down"),
    }[rule_id]
    if trend.inference_type != "trend" or trend.provenance_class != INFERENCE_PROVENANCE:
        reasons.append("invalid_trend_input")
    if trend.result_status != "derived":
        reasons.append("upstream_trend_uncertain")
    if trend.sample_count < rule.minimum_samples:
        reasons.append("insufficient_samples")
    if not trend.evidence_refs:
        reasons.append("evidence_ineligible")
    if (trend.key.assertion_kind, trend.direction) != expected_direction:
        reasons.append("rule_not_matched")
    if trend.confidence < 0.6:
        reasons.append("weak_support")
    result_status = "uncertain" if reasons else "derived"
    fields = {
        "inference_type": "risk",
        "result_status": result_status,
        "key": trend.key,
        "provenance_class": INFERENCE_PROVENANCE,
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "assertion_ids": trend.assertion_ids,
        "evidence_refs": trend.evidence_refs,
        "sample_count": trend.sample_count,
        "window_start": trend.window_start,
        "window_end": trend.window_end,
        "direction": trend.direction if not reasons else None,
        "magnitude": trend.magnitude if not reasons else None,
        "magnitude_method": trend.magnitude_method if not reasons else None,
        "unit": trend.unit if not reasons else None,
        "severity": "medium" if not reasons else None,
        "confidence": trend.confidence if not reasons else 0.0,
        "uncertainty": tuple(sorted(set((*trend.uncertainty, *reasons)))),
    }
    return _inference_record(**fields)
