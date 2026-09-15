"""Bounded, reconstructable and metadata-safe personal-state explanations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .changes import (
    ChangeRecord,
    ChangeSet,
    InferenceRecord,
    change_set_checksum,
)
from .schema import ValidatedEvidence, checksum
from .state_projection import FormationStep, LifecycleTrace, ProjectedState, StateKey


EXPLANATION_SCHEMA_VERSION = "personal_state_explanation_v1"
MAX_RECENT_LIMIT = 100


class ExplanationError(ValueError):
    """Stable error for invalid explanation bounds or lineage."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class EvidenceStatus:
    ref: str
    artifact_type: str
    status: str
    eligible: bool
    source_version: str | None
    serving_role: str | None = None
    expected_version: str | None = None
    privacy_class: str | None = None
    evidence_checksum: str | None = None


@dataclass(frozen=True)
class ExplanationRecord:
    record_id: str
    record_type: str
    key: StateKey
    effective_at: str
    status: str
    provenance_class: str
    before_assertion_ids: tuple[str, ...]
    after_assertion_ids: tuple[str, ...]
    before_value_checksum: str | None
    after_value_checksum: str | None
    rule_id: str
    rule_version: str
    derivation: str
    sample_count: int | None
    window_start: str | None
    window_end: str | None
    direction: str | None
    magnitude: float | None
    magnitude_method: str | None
    unit: str | None
    severity: str | None
    evidence: tuple[EvidenceStatus, ...]
    uncertainty: tuple[str, ...]
    abstained: bool


@dataclass(frozen=True)
class RecentChangesSummary:
    schema_version: str
    snapshot_id: str
    snapshot_hash: str
    run_id: str
    run_checksum: str
    algorithm_version: str
    as_of: str
    window_start: str
    limit: int
    total_available: int
    items: tuple[ExplanationRecord, ...]
    manifest_checksum: str


@dataclass(frozen=True)
class StateExplanation:
    schema_version: str
    snapshot_id: str
    snapshot_hash: str
    run_id: str
    run_checksum: str
    as_of: str
    key: StateKey
    state_status: str
    current_assertion_id: str | None
    current_value_checksum: str | None
    provenance_class: str | None
    confidence: float | None
    formation_path: tuple[FormationStep, ...]
    lifecycle_path: tuple[LifecycleTrace, ...]
    evidence: tuple[EvidenceStatus, ...]
    uncertainty: tuple[str, ...]
    abstained: bool
    explanation_checksum: str


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ExplanationError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise ExplanationError("invalid_time", f"{field}:timezone_required")
    return parsed


def _validate_context(
    *,
    snapshot_id: str,
    snapshot_hash: str,
    run_id: str,
    run_checksum: str,
) -> None:
    for field, value in (
        ("snapshot_id", snapshot_id),
        ("snapshot_hash", snapshot_hash),
        ("run_id", run_id),
        ("run_checksum", run_checksum),
    ):
        if not str(value).strip():
            raise ExplanationError("missing_context", field)


def _resolve_evidence(
    refs: Iterable[str],
    resolver: Any,
    catalog: Mapping[str, ValidatedEvidence],
) -> tuple[tuple[EvidenceStatus, ...], bool]:
    rows: list[EvidenceStatus] = []
    for ref in sorted(set(str(item) for item in refs if str(item))):
        expected = catalog.get(ref)
        if expected is None:
            rows.append(EvidenceStatus(ref, "unknown", "unbound", False, None))
            continue
        try:
            result = resolver.resolve(
                ref,
                artifact_type=expected.artifact_type,
                include_content=False,
                source_version=expected.artifact_version_id,
            )
        except Exception:
            result = {"ref": ref, "artifact_type": "unknown", "status": "resolver_error", "eligible": False}
        status = str(result.get("status") or "missing")
        actual_version = (
            str(result["source_version"])
            if result.get("source_version") is not None
            else None
        )
        eligible = (
            result.get("eligible") is True
            and status == "ok"
            and str(result.get("artifact_type") or "") == expected.artifact_type
            and actual_version == expected.artifact_version_id
        )
        rows.append(
            EvidenceStatus(
                ref=ref,
                artifact_type=str(result.get("artifact_type") or "unknown"),
                status=status,
                eligible=eligible,
                source_version=actual_version,
                serving_role=expected.serving_role,
                expected_version=expected.artifact_version_id,
                privacy_class=expected.privacy_class,
                evidence_checksum=expected.evidence_checksum,
            )
        )
    ordered = tuple(rows)
    return ordered, bool(ordered) and all(row.eligible for row in ordered)


def _change_explanation(
    record: ChangeRecord,
    resolver: Any,
    catalog: Mapping[str, ValidatedEvidence],
) -> ExplanationRecord:
    evidence, eligible = _resolve_evidence(record.evidence_refs, resolver, catalog)
    uncertainty = set(record.uncertainty)
    if not eligible:
        uncertainty.add("evidence_unavailable_or_ineligible")
    derivation = (
        "abstained_evidence_unavailable"
        if not eligible
        else f"{record.rule_id}:{record.change_type}"
    )
    return ExplanationRecord(
        record_id=record.change_id,
        record_type="change",
        key=record.key,
        effective_at=record.effective_at,
        status=record.change_type,
        provenance_class="inference",
        before_assertion_ids=record.before_assertion_ids,
        after_assertion_ids=record.after_assertion_ids,
        before_value_checksum=record.before_value_checksum,
        after_value_checksum=record.after_value_checksum,
        rule_id=record.rule_id,
        rule_version=record.rule_version,
        derivation=derivation,
        sample_count=None,
        window_start=None,
        window_end=None,
        direction=None,
        magnitude=None,
        magnitude_method=None,
        unit=None,
        severity=None,
        evidence=evidence,
        uncertainty=tuple(sorted(uncertainty)),
        abstained=not eligible,
    )


def _inference_explanation(
    record: InferenceRecord,
    resolver: Any,
    catalog: Mapping[str, ValidatedEvidence],
) -> ExplanationRecord:
    evidence, eligible = _resolve_evidence(record.evidence_refs, resolver, catalog)
    uncertainty = set(record.uncertainty)
    if not eligible:
        uncertainty.add("evidence_unavailable_or_ineligible")
    derived = record.result_status == "derived" and eligible
    return ExplanationRecord(
        record_id=record.inference_id,
        record_type=record.inference_type,
        key=record.key,
        effective_at=record.window_end or record.window_start or "",
        status=record.result_status if derived else "uncertain",
        provenance_class=record.provenance_class,
        before_assertion_ids=(),
        after_assertion_ids=record.assertion_ids,
        before_value_checksum=None,
        after_value_checksum=None,
        rule_id=record.rule_id,
        rule_version=record.rule_version,
        derivation=(
            f"{record.rule_id}:{record.result_status}"
            if derived
            else "abstained_evidence_or_rule_uncertain"
        ),
        sample_count=record.sample_count,
        window_start=record.window_start,
        window_end=record.window_end,
        direction=record.direction if derived else None,
        magnitude=record.magnitude if derived else None,
        magnitude_method=record.magnitude_method if derived else None,
        unit=record.unit if derived else None,
        severity=record.severity if derived else None,
        evidence=evidence,
        uncertainty=tuple(sorted(uncertainty)),
        abstained=not derived,
    )


def build_recent_changes(
    changes: ChangeSet,
    *,
    run_id: str,
    run_checksum: str,
    as_of: str,
    window_start: str,
    limit: int,
    resolver: Any,
    inferences: Iterable[InferenceRecord] = (),
    evidence_catalog: Mapping[str, ValidatedEvidence] | None = None,
) -> RecentChangesSummary:
    """Build one deterministic, explicitly bounded recent-change summary."""
    _validate_context(
        snapshot_id=changes.snapshot_id,
        snapshot_hash=changes.snapshot_hash,
        run_id=run_id,
        run_checksum=run_checksum,
    )
    if change_set_checksum(changes) != changes.manifest_checksum:
        raise ExplanationError("change_manifest_checksum_mismatch")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RECENT_LIMIT:
        raise ExplanationError("invalid_limit", str(limit))
    start = _parse_time(window_start, "window_start")
    end = _parse_time(as_of, "as_of")
    if start > end:
        raise ExplanationError("invalid_window")
    if _parse_time(changes.after_as_of, "change_after_as_of") > end:
        raise ExplanationError("future_change_manifest")

    catalog = evidence_catalog or {}
    items = [_change_explanation(row, resolver, catalog) for row in changes.records]
    items.extend(_inference_explanation(row, resolver, catalog) for row in inferences)
    bounded = [
        row
        for row in items
        if row.effective_at
        and start <= _parse_time(row.effective_at, "effective_at") <= end
    ]
    ordered = tuple(
        sorted(
            bounded,
            key=lambda row: (
                -_parse_time(row.effective_at, "effective_at").timestamp(),
                row.record_type,
                row.record_id,
            ),
        )
    )
    selected = ordered[:limit]
    manifest = {
        "schema_version": EXPLANATION_SCHEMA_VERSION,
        "snapshot_id": changes.snapshot_id,
        "snapshot_hash": changes.snapshot_hash,
        "run_id": run_id,
        "run_checksum": run_checksum,
        "algorithm_version": changes.algorithm_version,
        "as_of": as_of,
        "window_start": window_start,
        "limit": limit,
        "total_available": len(ordered),
        "items": selected,
    }
    return RecentChangesSummary(
        **manifest,
        manifest_checksum=checksum(manifest),
    )


def explain_state(
    state: ProjectedState,
    *,
    snapshot_id: str,
    snapshot_hash: str,
    run_id: str,
    run_checksum: str,
    as_of: str,
    resolver: Any,
) -> StateExplanation:
    """Reconstruct a current state from ordered assertion/event metadata."""
    _validate_context(
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        run_id=run_id,
        run_checksum=run_checksum,
    )
    as_of_dt = _parse_time(as_of, "as_of")
    formation = tuple(
        sorted(
            (
                row for row in state.formation_path
                if _parse_time(row.observed_at, "observed_at") <= as_of_dt
                and _parse_time(row.valid_from, "valid_from") <= as_of_dt
            ),
            key=lambda row: (
                _parse_time(row.valid_from, "valid_from"),
                _parse_time(row.observed_at, "observed_at"),
                row.assertion_id,
                row.run_id,
            ),
        )
    )
    lifecycle = tuple(
        sorted(
            (
                row for row in state.lifecycle_path
                if _parse_time(row.created_at, "created_at") <= as_of_dt
            ),
            key=lambda row: (_parse_time(row.created_at, "created_at"), row.event_id),
        )
    )
    typed = [item for step in formation for item in step.evidence]
    typed.extend(state.evidence)
    catalog: dict[str, ValidatedEvidence] = {}
    conflicts: set[str] = set()
    for item in typed:
        existing = catalog.get(item.ref)
        if existing is not None and existing != item:
            conflicts.add(item.ref)
        else:
            catalog[item.ref] = item
    for ref in conflicts:
        catalog.pop(ref, None)
    refs = {ref for step in formation for ref in step.evidence_refs}
    refs.update(item.ref for item in state.evidence)
    evidence, eligible = _resolve_evidence(refs, resolver, catalog)
    uncertainty = set(state.uncertainty)
    if not eligible:
        uncertainty.add("evidence_unavailable_or_ineligible")
    payload = {
        "schema_version": EXPLANATION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "run_id": run_id,
        "run_checksum": run_checksum,
        "as_of": as_of,
        "key": state.key,
        "state_status": state.status,
        "current_assertion_id": state.current_assertion_id,
        "current_value_checksum": (
            checksum(state.current_value) if state.current_assertion_id else None
        ),
        "provenance_class": state.provenance_class,
        "confidence": state.confidence,
        "formation_path": formation,
        "lifecycle_path": lifecycle,
        "evidence": evidence,
        "uncertainty": tuple(sorted(uncertainty)),
        "abstained": not eligible,
    }
    return StateExplanation(
        **payload,
        explanation_checksum=checksum(payload),
    )
