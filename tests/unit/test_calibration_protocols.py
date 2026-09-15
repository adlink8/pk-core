from __future__ import annotations

import pytest

from personal_knowledge.intelligence.calibration.protocols import (
    CalibrationProtocolError, REQUIRED_METRICS, build_protocol,
)


def _protocol(**changes):
    values = dict(
        question="Which local runtime path should the project use?", domain="project",
        external_snapshot_id="external-1", external_snapshot_hash="1" * 64,
        provider="codex-chatgpt", model="gpt-5.4", prompt_version="calibration-v1",
        schema_version="decision_analysis_candidate_v1", temperature=0,
        max_output_tokens=4096, max_total_tokens=27000,
        cohort=({"case_id": "case-1", "case_checksum": "2" * 64,
                 "outcome_event_checksum": "3" * 64},),
        exclusions=("health", "finance"), window_start="2026-07-18T14:00:00Z",
        window_end="2026-07-18T15:00:00Z",
        thresholds={name: 0 for name in REQUIRED_METRICS}, minimum_evidence=2,
        frozen_at="2026-07-18T13:00:00Z",
    )
    values.update(changes); return build_protocol(**values)


def test_protocol_freezes_every_pdi08_measure_and_failure_rule() -> None:
    protocol = _protocol()
    assert protocol.payload["metrics"] == list(REQUIRED_METRICS)
    assert protocol.payload["minimum_evidence"] == 2
    assert protocol.payload["only_arm_difference"] == "personal_snapshot_and_history_access"
    assert protocol.payload["causal_claim"] is False
    assert set(protocol.payload["inconclusive_rules"]) == {
        "sample_below_minimum", "missing_window", "protocol_deviation", "confounded_or_ambiguous",
    }


@pytest.mark.parametrize(
    ("changes", "code"),
    [({"minimum_evidence": 1}, "protocol_thresholds_invalid"),
     ({"thresholds": {"quality": 1}}, "protocol_thresholds_invalid"),
     ({"temperature": .2}, "protocol_budget_invalid"),
     ({"frozen_at": "2026-07-18T14:30:00Z"}, "protocol_chronology_invalid")],
)
def test_invalid_or_result_fitted_protocol_fails_closed(changes, code) -> None:
    with pytest.raises(CalibrationProtocolError, match=code): _protocol(**changes)
