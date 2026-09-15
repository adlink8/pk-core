"""Confirmed bridges to immutable Pilot and Calibration authorities."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.intelligence.calibration.proposals import create_proposal
from personal_knowledge.intelligence.calibration.service import explain as explain_calibration
from personal_knowledge.intelligence.pilot.cases import admit_project_case
from personal_knowledge.intelligence.pilot.outcomes import record_outcome_observation
from personal_knowledge.intelligence.pilot.workflow import (
    preregister_outcome, record_manual_action, record_user_decision,
)

from .models import OrchestrationError, OperationResult, Preview
from .service import OrchestrationService


def _item(preview: Preview | Mapping[str, Any]) -> Preview:
    return preview if isinstance(preview, Preview) else Preview.from_dict(preview)


def _input(preview: Preview) -> dict[str, Any]:
    value = preview.payload.get("input")
    if not isinstance(value, Mapping):
        raise OrchestrationError("operation_input_invalid")
    return dict(value)


def _authorize(
    service: OrchestrationService, preview: Preview, token: str,
    idempotency_key: str, now: str,
) -> dict[str, Any]:
    return service.authorize_transition(
        preview, confirmation_token=token, idempotency_key=idempotency_key, now=now,
    )


def execute_publish(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, pilot_db: Path | str,
    analysis_db: Path | str, now: str,
) -> OperationResult:
    item = _item(preview)
    auth = _authorize(service, item, confirmation_token, idempotency_key, now)
    if auth["replay"]:
        return service.commit_transition(
            item, confirmation_token=confirmation_token,
            idempotency_key=idempotency_key, now=now,
        )
    value = _input(item)
    result = admit_project_case(
        pilot_db_path=pilot_db, analysis_db_path=analysis_db,
        personal_db_path=service.personal_db, external_db_path=service.external_db,
        run_id=str(value.get("run_id") or ""),
        candidate_id=str(value.get("candidate_id") or ""),
        selected_option_id=str(value.get("selected_option_id") or ""),
        case_confirmation_event_id=str(value.get("case_confirmation_event_id") or ""),
        write=True, now=now,
    )
    if result.status != "candidate" or result.case is None or result.recommendation is None:
        raise OrchestrationError(
            str(result.reason_codes[0] if result.reason_codes else "pilot_admission_abstained")
        )
    refs = {
        "case_id": result.case.case_id, "case_checksum": result.case.payload_checksum,
        "recommendation_id": result.recommendation.recommendation_id,
        "recommendation_checksum": result.recommendation.payload_checksum,
    }
    return service.commit_transition(
        item, confirmation_token=confirmation_token, idempotency_key=idempotency_key,
        references=refs, now=now,
    )


def execute_decide(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, pilot_db: Path | str, now: str,
) -> OperationResult:
    item = _item(preview)
    auth = _authorize(service, item, confirmation_token, idempotency_key, now)
    if auth["replay"]:
        return service.commit_transition(item, confirmation_token=confirmation_token, idempotency_key=idempotency_key, now=now)
    value = _input(item)
    receipt = record_user_decision(
        pilot_db, case_id=str(value.get("case_id") or ""),
        decision=str(value.get("decision") or ""),
        confirmed_case_checksum=str(value.get("confirmed_case_checksum") or ""),
        reason_code=str(value.get("reason_code") or ""),
        expected_sequence=int(value.get("pilot_expected_sequence", -1)),
        idempotency_key=idempotency_key,
        actor_identity_hash=item.actor_identity_hash, occurred_at=now,
    )
    return service.commit_transition(
        item, confirmation_token=confirmation_token, idempotency_key=idempotency_key,
        references=asdict(receipt), now=now,
    )


def execute_preregister(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, pilot_db: Path | str, now: str,
) -> OperationResult:
    item = _item(preview)
    auth = _authorize(service, item, confirmation_token, idempotency_key, now)
    if auth["replay"]:
        return service.commit_transition(item, confirmation_token=confirmation_token, idempotency_key=idempotency_key, now=now)
    value = _input(item)
    receipt = preregister_outcome(
        pilot_db, case_id=str(value.get("case_id") or ""), metric=str(value.get("metric") or ""),
        unit=str(value.get("unit") or ""), baseline=float(value.get("baseline", 0)),
        target=float(value.get("target", 0)), direction=str(value.get("direction") or ""),
        window_start=str(value.get("window_start") or ""), window_end=str(value.get("window_end") or ""),
        collection_source=str(value.get("collection_source") or ""),
        estimated_time_minutes=float(value.get("estimated_time_minutes", 0)),
        estimated_cost=float(value.get("estimated_cost", 0)),
        expected_sequence=int(value.get("pilot_expected_sequence", -1)),
        idempotency_key=idempotency_key, actor_identity_hash=item.actor_identity_hash,
        occurred_at=now,
    )
    return service.commit_transition(
        item, confirmation_token=confirmation_token, idempotency_key=idempotency_key,
        references=asdict(receipt), now=now,
    )


def execute_manual_action(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, pilot_db: Path | str, now: str,
) -> OperationResult:
    item = _item(preview)
    if item.operation not in {"action_start", "action_complete"}:
        raise OrchestrationError("manual_action_operation_invalid")
    auth = _authorize(service, item, confirmation_token, idempotency_key, now)
    if auth["replay"]:
        return service.commit_transition(item, confirmation_token=confirmation_token, idempotency_key=idempotency_key, now=now)
    value = _input(item)
    expected_action = "started" if item.operation == "action_start" else "completed"
    if value.get("action_state") != expected_action:
        raise OrchestrationError("manual_action_state_mismatch")
    receipt = record_manual_action(
        pilot_db, case_id=str(value.get("case_id") or ""), action_state=expected_action,
        description=str(value.get("description") or ""), operator=str(value.get("operator") or "user"),
        expected_sequence=int(value.get("pilot_expected_sequence", -1)),
        idempotency_key=idempotency_key, actor_identity_hash=item.actor_identity_hash,
        occurred_at=now,
    )
    return service.commit_transition(
        item, confirmation_token=confirmation_token, idempotency_key=idempotency_key,
        references=asdict(receipt), now=now,
    )


def execute_observe(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, pilot_db: Path | str, now: str,
) -> OperationResult:
    item = _item(preview)
    auth = _authorize(service, item, confirmation_token, idempotency_key, now)
    if auth["replay"]:
        return service.commit_transition(item, confirmation_token=confirmation_token, idempotency_key=idempotency_key, now=now)
    value = _input(item)
    receipt = record_outcome_observation(
        pilot_db, case_id=str(value.get("case_id") or ""), observed_value=value.get("observed_value"),
        actual_time_minutes=float(value.get("actual_time_minutes", 0)),
        actual_cost=float(value.get("actual_cost", 0)), completion=str(value.get("completion") or ""),
        quality=value.get("quality"), satisfaction=value.get("satisfaction"),
        side_effects=tuple(str(x) for x in value.get("side_effects") or ()),
        regret=value.get("regret"), confounders=tuple(str(x) for x in value.get("confounders") or ()),
        source=str(value.get("source") or ""), observed_at=str(value.get("observed_at") or now),
        expected_sequence=int(value.get("pilot_expected_sequence", -1)),
        idempotency_key=idempotency_key, actor_identity_hash=item.actor_identity_hash,
    )
    return service.commit_transition(
        item, confirmation_token=confirmation_token, idempotency_key=idempotency_key,
        references={**asdict(receipt), "causal_claim": False}, now=now,
    )


def execute_calibrate(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, calibration_db: Path | str,
    now: str, calibration_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> OperationResult:
    item = _item(preview)
    auth = _authorize(service, item, confirmation_token, idempotency_key, now)
    if auth["replay"]:
        return service.commit_transition(item, confirmation_token=confirmation_token, idempotency_key=idempotency_key, now=now)
    value = _input(item)
    if calibration_runner is not None:
        result = dict(calibration_runner(value))
    else:
        protocol_id = str(value.get("protocol_id") or "")
        result = explain_calibration(calibration_db, protocol_id)
        if not result.get("protocol") or not result.get("verdicts"):
            raise OrchestrationError("calibration_evidence_incomplete")
        proposal_spec = value.get("proposal")
        if proposal_spec:
            spec = dict(proposal_spec)
            result["proposal"] = create_proposal(
                calibration_db, protocol_id,
                parent_version=str(spec.get("parent_version") or ""),
                parent_checksum=str(spec.get("parent_checksum") or ""),
                proposal_kind=str(spec.get("proposal_kind") or ""),
                changes=dict(spec.get("changes") or {}),
                rationale=tuple(str(x) for x in spec.get("rationale") or ()), created_at=now,
            )
    if result.get("causal_claim") is not False or result.get("promotion_available") is not False:
        raise OrchestrationError("calibration_boundary_invalid")
    refs = {
        "protocol_id": str(value.get("protocol_id") or result.get("protocol_id") or ""),
        "causal_claim": False, "promotion_available": False,
    }
    if isinstance(result.get("proposal"), Mapping):
        refs.update({key: result["proposal"][key] for key in ("proposal_id", "proposal_checksum") if key in result["proposal"]})
    return service.commit_transition(
        item, confirmation_token=confirmation_token, idempotency_key=idempotency_key,
        references=refs, now=now,
    )


__all__ = [
    "execute_calibrate", "execute_decide", "execute_manual_action", "execute_observe",
    "execute_preregister", "execute_publish",
]
