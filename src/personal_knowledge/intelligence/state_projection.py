"""Pure normalization and projection for snapshot-bound personal state.

This module accepts already-extracted, evidence-backed metadata.  It performs no
network access and never writes source, knowledge, lifecycle, or serving state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .schema import (
    ASSERTION_KINDS,
    ASSERTION_LIFECYCLES,
    EVIDENCE_TYPES,
    PRIVACY_CLASSES,
    PROVENANCE_CLASSES,
    EvidenceReference,
    PersonalStateRun,
    SnapshotBinding,
    StateAssertion,
    ValidatedAssertion,
    ValidatedEvidence,
    canonical_json,
    checksum,
)
from .runs import REGISTRY_ID, plan_run


NORMALIZABLE_KINDS = frozenset({"goal", "constraint", "observation"})
DERIVATION_PROVENANCE = {
    "canonical_fact": "fact",
    "occurrence": "observation",
    "synthesis": "inference",
}
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"content", "body", "raw_text", "evidence_quote", "prompt", "response_text"}
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s${][^\s]*"
)


class ProjectionError(ValueError):
    """Stable fail-closed error for normalization/projection contracts."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _required(record: Mapping[str, Any], field: str) -> Any:
    if field not in record or record[field] is None or record[field] == "":
        raise ProjectionError("missing_field", field)
    return record[field]


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProjectionError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise ProjectionError("invalid_time", f"{field}:timezone_required")
    return parsed


def _reject_private_payload(value: Any, path: str = "candidate") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key)
            if label.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                raise ProjectionError("private_payload", f"{path}.{label}")
            _reject_private_payload(item, f"{path}.{label}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _reject_private_payload(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_RE.search(value):
        raise ProjectionError("secret_payload", path)


def _normalize_evidence(
    rows: Any,
    *,
    snapshot: SnapshotBinding,
) -> tuple[EvidenceReference, ...]:
    if not isinstance(rows, (tuple, list)) or not rows:
        raise ProjectionError("evidence_required")
    evidence: list[EvidenceReference] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ProjectionError("invalid_evidence", str(index))
        if str(_required(raw, "snapshot_id")) != snapshot.snapshot_id or str(
            _required(raw, "snapshot_hash")
        ) != snapshot.snapshot_hash:
            raise ProjectionError("mixed_snapshot", str(index))
        role = str(_required(raw, "serving_role"))
        version_id = str(_required(raw, "artifact_version_id"))
        member = snapshot.members.get(role)
        if member is None or str(member.get("artifact_version_id") or "") != version_id:
            raise ProjectionError("evidence_version_mismatch", str(index))
        privacy = str(_required(raw, "privacy_class"))
        if privacy not in PRIVACY_CLASSES or privacy != str(
            member.get("privacy_class") or ""
        ):
            raise ProjectionError("evidence_privacy_mismatch", str(index))
        evidence.append(
            EvidenceReference(
                ref=str(_required(raw, "ref")),
                artifact_type=str(_required(raw, "artifact_type")),
                serving_role=role,
                artifact_version_id=version_id,
                checksum=str(raw.get("checksum") or ""),
                privacy_class=privacy,
            )
        )
        if evidence[-1].artifact_type not in EVIDENCE_TYPES:
            raise ProjectionError("unknown_evidence_type", evidence[-1].artifact_type)
    ordered = tuple(
        sorted(
            evidence,
            key=lambda item: (
                item.artifact_type,
                item.ref,
                item.serving_role,
                item.artifact_version_id,
            ),
        )
    )
    keys = {(row.artifact_type, row.ref) for row in ordered}
    if len(keys) != len(ordered):
        raise ProjectionError("duplicate_evidence")
    return ordered


def normalize_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    snapshot: SnapshotBinding,
) -> tuple[StateAssertion, ...]:
    """Normalize extracted candidates into deterministic typed assertions."""
    normalized: list[StateAssertion] = []
    for ordinal, raw in enumerate(candidates):
        if not isinstance(raw, Mapping):
            raise ProjectionError("invalid_candidate", str(ordinal))
        _reject_private_payload(raw, f"candidate[{ordinal}]")
        if str(_required(raw, "snapshot_id")) != snapshot.snapshot_id or str(
            _required(raw, "snapshot_hash")
        ) != snapshot.snapshot_hash:
            raise ProjectionError("mixed_snapshot", str(ordinal))
        assertion_kind = str(_required(raw, "assertion_kind"))
        if assertion_kind not in NORMALIZABLE_KINDS or assertion_kind not in ASSERTION_KINDS:
            raise ProjectionError("invalid_assertion_kind", assertion_kind)
        provenance = str(_required(raw, "provenance_class"))
        if provenance not in PROVENANCE_CLASSES:
            raise ProjectionError("invalid_provenance_class", provenance)
        derivation = str(_required(raw, "derivation"))
        expected = DERIVATION_PROVENANCE.get(derivation)
        if expected is None:
            raise ProjectionError("invalid_derivation", derivation)
        if provenance != expected:
            raise ProjectionError(
                "provenance_rule_violation", f"{derivation}:{provenance}"
            )
        if assertion_kind == "observation" and provenance == "fact":
            raise ProjectionError("observation_as_fact")

        valid_from = str(_required(raw, "valid_from"))
        observed_at = str(_required(raw, "observed_at"))
        valid_from_dt = _parse_time(valid_from, "valid_from")
        _parse_time(observed_at, "observed_at")
        valid_to = str(raw["valid_to"]) if raw.get("valid_to") else None
        if valid_to and _parse_time(valid_to, "valid_to") < valid_from_dt:
            raise ProjectionError("invalid_time_interval")
        confidence = float(_required(raw, "confidence"))
        if not 0.0 <= confidence <= 1.0:
            raise ProjectionError("invalid_confidence", str(confidence))
        uncertainty = str(_required(raw, "uncertainty_reason")).strip()
        if not uncertainty:
            raise ProjectionError("missing_field", "uncertainty_reason")
        values = {
            field: str(_required(raw, field)).strip()
            for field in ("subject", "domain", "scope", "predicate")
        }
        if any(not value for value in values.values()):
            raise ProjectionError("missing_identity_field")
        lifecycle = str(raw.get("lifecycle") or "current")
        if lifecycle not in ASSERTION_LIFECYCLES:
            raise ProjectionError("invalid_assertion_lifecycle", lifecycle)
        normalized.append(
            StateAssertion(
                assertion_kind=assertion_kind,
                provenance_class=provenance,
                subject=values["subject"],
                domain=values["domain"],
                scope=values["scope"],
                predicate=values["predicate"],
                value=_required(raw, "value"),
                valid_from=valid_from,
                valid_to=valid_to,
                observed_at=observed_at,
                confidence=confidence,
                uncertainty=uncertainty,
                lifecycle=lifecycle,
                evidence=_normalize_evidence(raw.get("evidence"), snapshot=snapshot),
            )
        )
    ordered = tuple(sorted(normalized, key=lambda item: checksum(item)))
    if not ordered:
        raise ProjectionError("candidates_required")
    if len({canonical_json(item) for item in ordered}) != len(ordered):
        raise ProjectionError("duplicate_candidate")
    return ordered


@dataclass(frozen=True, order=True)
class StateKey:
    assertion_kind: str
    subject: str
    domain: str
    scope: str
    predicate: str


@dataclass(frozen=True)
class FormationStep:
    run_id: str
    assertion_id: str
    valid_from: str
    valid_to: str | None
    observed_at: str
    provenance_class: str
    lifecycle: str
    status: str
    confidence: float
    value_checksum: str
    evidence_refs: tuple[str, ...]
    uncertainty: tuple[str, ...]
    value_type: str = ""
    evidence: tuple[ValidatedEvidence, ...] = ()


@dataclass(frozen=True)
class LifecycleTrace:
    event_id: str
    unit_id: str
    event_type: str
    lifecycle_before: str
    lifecycle_after: str
    created_at: str
    reason_checksum: str


@dataclass(frozen=True)
class ProjectedState:
    key: StateKey
    status: str
    current_assertion_id: str | None
    current_value: Any
    provenance_class: str | None
    confidence: float | None
    uncertainty: tuple[str, ...]
    evidence: tuple[ValidatedEvidence, ...]
    formation_path: tuple[FormationStep, ...]
    lifecycle_path: tuple[LifecycleTrace, ...]


@dataclass(frozen=True)
class StateProjection:
    """Versioned, time-aware projection surface (Plan 61-09 / D-21).

    Every derived projection is a versioned inference: it carries a version, its
    supersession record (which accepted inference a newer one replaced), the
    source/snapshot/freshness binding and its limitations. These are the fields
    ``personal.model_projection.get`` serves without raw Evidence or canonical
    claims.
    """

    snapshot_id: str
    snapshot_hash: str
    as_of: str
    states: tuple[ProjectedState, ...]
    version: int = 1
    supersession: Any = None
    freshness: tuple = ()
    limitations: tuple[str, ...] = ()

    @property
    def current_goals(self) -> tuple[ProjectedState, ...]:
        return tuple(
            row
            for row in self.states
            if row.key.assertion_kind == "goal" and row.current_assertion_id
        )

    @property
    def current_constraints(self) -> tuple[ProjectedState, ...]:
        return tuple(
            row
            for row in self.states
            if row.key.assertion_kind == "constraint" and row.current_assertion_id
        )

    @property
    def current_observations(self) -> tuple[ProjectedState, ...]:
        return tuple(
            row
            for row in self.states
            if row.key.assertion_kind == "observation" and row.current_assertion_id
        )


def _key(assertion: ValidatedAssertion) -> StateKey:
    return StateKey(
        assertion_kind=assertion.assertion_kind,
        subject=assertion.subject,
        domain=assertion.domain,
        scope=assertion.scope,
        predicate=assertion.predicate,
    )


def _assertion_status(assertion: ValidatedAssertion, as_of: datetime) -> str:
    # Bitemporal boundary: an assertion cannot be known before it was observed,
    # even when its business-valid interval starts earlier.
    if _parse_time(assertion.observed_at, "observed_at") > as_of:
        return "future"
    if _parse_time(assertion.valid_from, "valid_from") > as_of:
        return "future"
    if assertion.valid_to and _parse_time(assertion.valid_to, "valid_to") < as_of:
        return "expired"
    if assertion.lifecycle != "current":
        return assertion.lifecycle
    return "candidate"


def _lineage_is_bound(
    assertion: ValidatedAssertion,
    snapshot: SnapshotBinding,
) -> bool:
    if not assertion.evidence:
        return False
    for evidence in assertion.evidence:
        member = snapshot.members.get(evidence.serving_role)
        if member is None or str(member.get("artifact_version_id") or "") != str(
            evidence.artifact_version_id
        ):
            return False
        if evidence.privacy_class != str(member.get("privacy_class") or ""):
            return False
    return True


def _formation_step(
    run_id: str,
    assertion: ValidatedAssertion,
    *,
    as_of: datetime,
) -> FormationStep:
    uncertainty = [f"source:{assertion.uncertainty}"]
    if assertion.confidence < 0.6:
        uncertainty.append("low_confidence")
    return FormationStep(
        run_id=run_id,
        assertion_id=assertion.assertion_id,
        valid_from=assertion.valid_from,
        valid_to=assertion.valid_to,
        observed_at=assertion.observed_at,
        provenance_class=assertion.provenance_class,
        lifecycle=assertion.lifecycle,
        status=_assertion_status(assertion, as_of),
        confidence=assertion.confidence,
        value_checksum=checksum(assertion.value),
        evidence_refs=tuple(sorted(item.ref for item in assertion.evidence)),
        uncertainty=tuple(uncertainty),
        value_type=_value_type(assertion.value),
        evidence=assertion.evidence,
    )


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
    raise ProjectionError("unsupported_value_type", type(value).__name__)


def _history_matches(row: Mapping[str, Any], key: StateKey) -> bool:
    if str(row.get("subject") or "") != key.subject:
        return False
    return all(
        not row.get(field) or str(row[field]) == getattr(key, field)
        for field in ("domain", "scope", "predicate")
    )


def _lifecycle_trace(
    history_rows: Iterable[Mapping[str, Any]],
    key: StateKey,
    *,
    as_of: datetime,
) -> tuple[tuple[LifecycleTrace, ...], bool]:
    matched = [row for row in history_rows if _history_matches(row, key)]
    accepted_rows = [
        row
        for row in matched
        if str(row.get("status") or "").lower()
        not in {"pending", "proposed", "unreviewed"}
        and str(row.get("decision") or "").lower() not in {"pending", "reject"}
    ]
    ids = {str(row.get("unit_id") or row.get("canonical_unit_id") or "") for row in accepted_rows}
    missing_predecessor = any(
        row.get("supersedes_id") and str(row["supersedes_id"]) not in ids
        for row in accepted_rows
    )
    traces: list[LifecycleTrace] = []
    for row in accepted_rows:
        unit_id = str(row.get("unit_id") or row.get("canonical_unit_id") or "")
        for event in row.get("lifecycle_events") or []:
            if not isinstance(event, Mapping):
                continue
            if not event.get("event_id") or not event.get("reviewer_id_hash") or not event.get("actor_id"):
                continue
            if str(event.get("status") or "").lower() in {"pending", "proposed"}:
                continue
            created_at = str(event.get("created_at") or "")
            if not created_at or _parse_time(created_at, "created_at") > as_of:
                continue
            traces.append(
                LifecycleTrace(
                    event_id=str(event["event_id"]),
                    unit_id=unit_id,
                    event_type=str(event.get("event_type") or ""),
                    lifecycle_before=str(event.get("lifecycle_before") or ""),
                    lifecycle_after=str(event.get("lifecycle_after") or ""),
                    created_at=created_at,
                    reason_checksum=checksum(str(event.get("reason") or "")),
                )
            )
    return (
        tuple(sorted(traces, key=lambda row: (row.created_at, row.event_id))),
        missing_predecessor,
    )


def project_current_state(
    runs: Iterable[PersonalStateRun],
    *,
    as_of: str,
    history_rows: Iterable[Mapping[str, Any]] = (),
    expected_keys: Iterable[StateKey] = (),
) -> StateProjection:
    """Project deterministic current state from immutable runs at one snapshot."""
    run_rows = tuple(sorted(runs, key=lambda row: row.run_id))
    if not run_rows:
        raise ProjectionError("runs_required")
    as_of_dt = _parse_time(as_of, "as_of")
    snapshot = run_rows[0].snapshot
    seen_runs: set[str] = set()
    grouped: dict[StateKey, list[tuple[str, ValidatedAssertion]]] = {}
    for run in run_rows:
        if run.run_id in seen_runs:
            raise ProjectionError("duplicate_run", run.run_id)
        seen_runs.add(run.run_id)
        if run.registry_id != REGISTRY_ID:
            raise ProjectionError("registry_authority_mismatch", run.registry_id)
        if (
            run.snapshot.snapshot_id != snapshot.snapshot_id
            or run.snapshot.snapshot_hash != snapshot.snapshot_hash
            or canonical_json(run.snapshot.members) != canonical_json(snapshot.members)
        ):
            raise ProjectionError("mixed_snapshot", run.run_id)
        if checksum(run.input_manifest) != run.input_manifest_checksum or checksum(
            run.output_manifest
        ) != run.output_manifest_checksum:
            raise ProjectionError("run_manifest_checksum_mismatch", run.run_id)
        for assertion in run.assertions:
            if not _lineage_is_bound(assertion, snapshot):
                raise ProjectionError("evidence_snapshot_mismatch", assertion.assertion_id)
            grouped.setdefault(_key(assertion), []).append((run.run_id, assertion))
    for key in expected_keys:
        grouped.setdefault(key, [])

    history = tuple(history_rows)
    superseded_assertion_ids: list[str] = []
    states: list[ProjectedState] = []
    for key, rows in sorted(grouped.items(), key=lambda item: item[0]):
        ordered = sorted(
            rows,
            key=lambda item: (
                _parse_time(item[1].valid_from, "valid_from"),
                _parse_time(item[1].observed_at, "observed_at"),
                item[1].assertion_id,
                item[0],
            ),
        )
        # Formation/history/explanation share the same knowledge boundary:
        # exclude assertions not observed or not yet valid at as_of.
        bounded = [
            item for item in ordered
            if _parse_time(item[1].observed_at, "observed_at") <= as_of_dt
            and _parse_time(item[1].valid_from, "valid_from") <= as_of_dt
        ]
        formation = tuple(
            _formation_step(run_id, assertion, as_of=as_of_dt)
            for run_id, assertion in bounded
        )
        lifecycle_path, missing_predecessor = _lifecycle_trace(
            history, key, as_of=as_of_dt
        )
        uncertainty: list[str] = []
        if missing_predecessor:
            uncertainty.append("missing_predecessor")
        active = [
            item
            for item in bounded
            if _assertion_status(item[1], as_of_dt) == "candidate"
        ]
        relevant = [
            item
            for item in bounded
            if _assertion_status(item[1], as_of_dt) != "future"
        ]
        latest_moment = (
            max(
                (
                    _parse_time(item[1].valid_from, "valid_from"),
                    _parse_time(item[1].observed_at, "observed_at"),
                )
                for item in relevant
            )
            if relevant
            else None
        )
        explicit_conflicts = [
            item
            for item in relevant
            if _assertion_status(item[1], as_of_dt) == "conflict"
            and (
                _parse_time(item[1].valid_from, "valid_from"),
                _parse_time(item[1].observed_at, "observed_at"),
            )
            == latest_moment
        ]
        selected: ValidatedAssertion | None = None
        status = "unknown"
        if explicit_conflicts:
            status = "conflict"
            uncertainty.append("unresolved_conflict")
        elif active:
            selected = active[-1][1]
            if len(active) > 1:
                previous = active[-2][1]
                simultaneous = (
                    previous.valid_from == selected.valid_from
                    and previous.observed_at == selected.observed_at
                )
                if simultaneous and canonical_json(previous.value) != canonical_json(
                    selected.value
                ):
                    selected = None
                    status = "conflict"
                    uncertainty.append("unresolved_conflict")
            if selected is not None:
                status = "current"
                uncertainty.append(f"source:{selected.uncertainty}")
                if selected.confidence < 0.6:
                    status = "uncertain"
                    uncertainty.append("low_confidence")
        elif bounded:
            statuses = {step.status for step in formation}
            if statuses <= {"expired", "future"} and "expired" in statuses:
                status = "expired"
            elif "stale" in statuses:
                status = "stale"
            uncertainty.append("no_current_evidence")
        else:
            uncertainty.append("unknown_no_evidence")
        if selected is not None:
            # Supersession: a newer current accepted inference replaces the
            # earlier current ones on the same key (D-21 formation history).
            for step in formation:
                if step.assertion_id != selected.assertion_id and step.lifecycle == "current":
                    superseded_assertion_ids.append(step.assertion_id)
        states.append(
            ProjectedState(
                key=key,
                status=status,
                current_assertion_id=selected.assertion_id if selected else None,
                current_value=selected.value if selected else None,
                provenance_class=selected.provenance_class if selected else None,
                confidence=selected.confidence if selected else None,
                uncertainty=tuple(sorted(set(uncertainty))),
                evidence=selected.evidence if selected else (),
                formation_path=formation,
                lifecycle_path=lifecycle_path,
            )
        )
    superseded = tuple(sorted(set(superseded_assertion_ids)))
    projection_limitations = ["derived projection; not a personal fact or stable label"]
    for state in states:
        if state.status == "unknown":
            projection_limitations.append("unknown: some scope content has no current derived state")
        elif state.status == "expired":
            projection_limitations.append("expired: some scope content is outside its valid interval")
        elif state.status == "conflict":
            projection_limitations.append("conflict: some scope content is unresolved")
        elif state.status == "uncertain":
            projection_limitations.append("uncertain: some scope content is low-confidence")
    return StateProjection(
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.snapshot_hash,
        as_of=as_of,
        states=tuple(states),
        version=max(1, len(run_rows)),
        supersession=superseded if superseded else None,
        limitations=tuple(sorted(set(projection_limitations))),
    )


def plan_projection_run(
    db_path: Path,
    candidates: Iterable[Mapping[str, Any]],
    *,
    snapshot: SnapshotBinding,
    producer_version: str,
    input_manifest: Mapping[str, Any],
    resolver: Any = None,
) -> PersonalStateRun:
    """Normalize and delegate persistence planning to the 25-01 run API."""
    assertions = normalize_candidates(candidates, snapshot=snapshot)
    return plan_run(
        db_path,
        assertions,
        producer_version=producer_version,
        input_manifest=input_manifest,
        snapshot_id=snapshot.snapshot_id,
        resolver=resolver,
    )
