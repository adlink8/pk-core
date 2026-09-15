"""Typed non-causal project outcome observations and assessment."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .workflow import PilotEventReceipt, PilotWorkflowError, _append, _utc, read_event_stream


@dataclass(frozen=True)
class OutcomeAssessment:
    case_id: str
    status: str
    metric: str
    observed_value: float | None
    target: float
    direction: str
    window_complete: bool
    confounded: bool
    causal_claim: bool
    reason_codes: tuple[str, ...]


def record_outcome_observation(
    db_path: Path | str, *, case_id: str, observed_value: float | None,
    actual_time_minutes: float, actual_cost: float, completion: str,
    quality: float | None, satisfaction: float | None, side_effects: tuple[str, ...],
    regret: float | None, confounders: tuple[str, ...], source: str,
    observed_at: str, expected_sequence: int, idempotency_key: str,
    actor_identity_hash: str,
) -> PilotEventReceipt:
    if completion not in {"completed", "partial", "abandoned"}:
        raise PilotWorkflowError("outcome_completion_invalid")
    if actual_time_minutes < 0 or actual_cost < 0 or not source.strip() or not observed_at.endswith("Z"):
        raise PilotWorkflowError("outcome_observation_invalid")
    for value in (quality, satisfaction, regret):
        if value is not None and not 0.0 <= float(value) <= 1.0:
            raise PilotWorkflowError("outcome_rating_invalid")
    stream = read_event_stream(db_path, case_id)
    action_states = [item["payload"]["body"]["action_state"] for item in stream if item["event_type"] == "manual_action"]
    if action_states != ["started", "completed"]:
        raise PilotWorkflowError("completed_manual_action_required")
    protocols = [item for item in stream if item["event_type"] == "outcome_preregistered"]
    if len(protocols) != 1:
        raise PilotWorkflowError("outcome_preregistration_required")
    body: Mapping[str, Any] = {
        "observed_value": None if observed_value is None else float(observed_value),
        "actual_time_minutes": float(actual_time_minutes), "actual_cost": float(actual_cost),
        "completion": completion, "quality": quality, "satisfaction": satisfaction,
        "side_effects": list(side_effects), "regret": regret,
        "confounders": list(confounders), "source": source, "observed_at": observed_at,
        "causal_claim": False,
    }
    return _append(
        db_path, case_id=case_id, event_type="outcome_observed", body=body,
        actor="user", actor_identity_hash=actor_identity_hash,
        expected_sequence=expected_sequence, idempotency_key=idempotency_key,
        occurred_at=observed_at,
    )


def assess_outcome(db_path: Path | str, case_id: str, *, as_of: str) -> OutcomeAssessment:
    stream = read_event_stream(db_path, case_id)
    protocols = [item["payload"]["body"] for item in stream if item["event_type"] == "outcome_preregistered"]
    observations = [item["payload"]["body"] for item in stream if item["event_type"] == "outcome_observed"]
    if len(protocols) != 1:
        raise PilotWorkflowError("outcome_preregistration_required")
    protocol = protocols[0]
    window_complete = _utc(as_of) >= _utc(protocol["window_end"])
    if not observations:
        return OutcomeAssessment(
            case_id, "inconclusive", protocol["metric"], None, float(protocol["target"]),
            protocol["direction"], window_complete, False, False, ("observation_missing",),
        )
    observation = observations[-1]
    confounded = bool(observation["confounders"])
    value = observation["observed_value"]
    reasons: list[str] = []
    if not window_complete:
        reasons.append("window_incomplete")
    if value is None:
        reasons.append("metric_missing")
    if confounded:
        reasons.append("confounded")
    if reasons:
        status = "inconclusive"
    elif protocol["direction"] == "higher":
        status = "pass" if float(value) >= float(protocol["target"]) else "fail"
    elif protocol["direction"] == "lower":
        status = "pass" if float(value) <= float(protocol["target"]) else "fail"
    else:
        status = "pass" if float(value) == float(protocol["target"]) else "fail"
    return OutcomeAssessment(
        case_id, status, protocol["metric"], None if value is None else float(value),
        float(protocol["target"]), protocol["direction"], window_complete,
        confounded, False, tuple(reasons),
    )


__all__ = ["OutcomeAssessment", "assess_outcome", "record_outcome_observation"]
