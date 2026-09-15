from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from personal_knowledge.intelligence.changes import (
    TrendSample,
    change_set_checksum,
    compare_projections,
    derive_risk,
    derive_trend,
)
from personal_knowledge.intelligence.explanations import (
    ExplanationError,
    build_recent_changes,
    explain_state,
)
from personal_knowledge.intelligence.schema import ValidatedEvidence, canonical_json, checksum
from personal_knowledge.intelligence.state_projection import LifecycleTrace
from tests.unit.test_personal_state_changes import (
    KEY,
    _projection,
    _state,
    _step,
)


class Resolver:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.include_content: list[bool] = []

    def resolve(
        self, ref, *, artifact_type=None, include_content=False, source_version=None
    ):
        self.include_content.append(include_content)
        status = self.statuses.get(ref, "ok")
        return {
            "ref": ref,
            "artifact_type": artifact_type or "canonical_message",
            "status": status,
            "eligible": status == "ok",
            "source_version": source_version or "av1",
            "content": "must never be copied",
        }


def _change_set():
    old = _state("a1", 10, steps=(_step("a1", 10, valid_from="2026-01-01T00:00:00Z"),))
    middle = _state("a2", 20, steps=old.formation_path + (_step("a2", 20, valid_from="2026-02-01T00:00:00Z"),))
    latest = _state("a3", 30, steps=middle.formation_path + (_step("a3", 30, valid_from="2026-03-01T00:00:00Z"),))
    first = compare_projections(_projection("2026-01-01T00:00:00Z", (old,)), _projection("2026-02-01T00:00:00Z", (middle,)))
    second = compare_projections(_projection("2026-02-01T00:00:00Z", (middle,)), _projection("2026-03-01T00:00:00Z", (latest,)))
    combined = replace(
        first,
        after_as_of=second.after_as_of,
        records=first.records + second.records,
        manifest_checksum="pending",
    )
    return replace(combined, manifest_checksum=change_set_checksum(combined))


def _summary(limit=10, resolver=None, inferences=()):
    refs = {
        ref
        for record in _change_set().records
        for ref in record.evidence_refs
    }
    refs.update(ref for record in inferences for ref in record.evidence_refs)
    catalog = {
        ref: ValidatedEvidence(
            ref, "canonical_message", "canonical_message", "av1", checksum(ref), "R2"
        )
        for ref in refs
    }
    return build_recent_changes(
        _change_set(),
        run_id="psr-1",
        run_checksum="run-checksum",
        as_of="2026-04-01T00:00:00Z",
        window_start="2026-01-15T00:00:00Z",
        limit=limit,
        resolver=resolver or Resolver(),
        inferences=inferences,
        evidence_catalog=catalog,
    )


def test_recent_summary_is_bounded_deterministic_and_reconstructable() -> None:
    resolver = Resolver()
    first = _summary(limit=1, resolver=resolver)
    second = _summary(limit=1, resolver=Resolver())

    assert first == second
    assert first.total_available == 2
    assert len(first.items) == 1
    assert first.items[0].effective_at == "2026-03-01T00:00:00Z"
    assert first.items[0].before_assertion_ids == ("a2",)
    assert first.items[0].after_assertion_ids == ("a3",)
    assert first.items[0].before_value_checksum == checksum(20)
    assert first.items[0].after_value_checksum == checksum(30)
    assert first.manifest_checksum == checksum({
        key: value for key, value in asdict(first).items() if key != "manifest_checksum"
    })
    assert resolver.include_content and not any(resolver.include_content)


@pytest.mark.parametrize("limit", [0, 101, True])
def test_recent_summary_rejects_invalid_limits(limit) -> None:
    with pytest.raises(ExplanationError, match="invalid_limit"):
        _summary(limit=limit)


def test_window_excludes_out_of_range_records() -> None:
    result = build_recent_changes(
        _change_set(),
        run_id="psr-1",
        run_checksum="run-checksum",
        as_of="2026-04-01T00:00:00Z",
        window_start="2026-03-02T00:00:00Z",
        limit=10,
        resolver=Resolver(),
    )
    assert result.total_available == 0
    assert result.items == ()


def test_tampered_change_manifest_fails_closed() -> None:
    with pytest.raises(ExplanationError, match="change_manifest_checksum_mismatch"):
        build_recent_changes(
            replace(_change_set(), manifest_checksum="tampered"),
            run_id="psr-1", run_checksum="run-checksum",
            as_of="2026-04-01T00:00:00Z",
            window_start="2026-01-01T00:00:00Z",
            limit=10, resolver=Resolver(),
        )


def test_ineligible_or_missing_evidence_abstains_without_copying_body() -> None:
    resolver = Resolver({"msg:a2": "ineligible", "msg:a3": "missing"})
    result = _summary(limit=1, resolver=resolver)
    item = result.items[0]
    assert item.abstained is True
    assert item.derivation == "abstained_evidence_unavailable"
    assert "evidence_unavailable_or_ineligible" in item.uncertainty
    assert all(not row.eligible for row in item.evidence if row.ref in {"msg:a2", "msg:a3"})
    assert "must never be copied" not in canonical_json(result)


@pytest.mark.parametrize("mode", ["missing_eligibility", "version_drift"])
def test_evidence_must_be_explicitly_eligible_and_version_bound(mode) -> None:
    class DriftResolver(Resolver):
        def resolve(self, ref, **kwargs):
            result = super().resolve(ref, **kwargs)
            if mode == "missing_eligibility":
                result.pop("eligible")
            else:
                result["source_version"] = "different-version"
            return result

    item = _summary(limit=1, resolver=DriftResolver()).items[0]
    assert item.abstained is True
    assert all(row.eligible is False for row in item.evidence)


def test_trend_and_risk_derivation_is_explained_without_prescriptive_fields() -> None:
    key = replace(KEY, assertion_kind="constraint")
    samples = tuple(
        TrendSample(
            assertion_id=f"o{i}", key=key, value=float(i), unit="count",
            observed_at=f"2026-0{i}-01T00:00:00Z",
            evidence_refs=(f"cm|{i}",), evidence_eligible=True, confidence=0.9,
        )
        for i in range(1, 4)
    )
    trend = derive_trend(samples)
    risk = derive_risk(trend, rule_id="increasing_constraint_pressure")
    result = _summary(inferences=(risk, trend))
    assert {row.record_type for row in result.items} >= {"trend", "risk"}
    assert all(row.rule_version == "1" for row in result.items if row.record_type in {"trend", "risk"})
    serialized = canonical_json(result)
    for forbidden in ("recommendation", "confirmation", "action", "priority", "advice"):
        assert f'"{forbidden}"' not in serialized


def test_state_explanation_reconstructs_ordered_assertions_and_reviewed_events() -> None:
    state = _state(
        "a2",
        20,
        steps=(
            _step("a2", 20, valid_from="2026-02-01T00:00:00Z"),
            _step("a1", 10, valid_from="2026-01-01T00:00:00Z"),
        ),
    )
    state = replace(
        state,
        lifecycle_path=(
            LifecycleTrace(
                event_id="event-1", unit_id="ku1", event_type="correct",
                lifecycle_before="conflict", lifecycle_after="current",
                created_at="2026-02-02T00:00:00Z", reason_checksum="reason-hash",
            ),
        ),
    )
    result = explain_state(
        state,
        snapshot_id="ss_test",
        snapshot_hash="snapshot-hash",
        run_id="psr-1",
        run_checksum="run-checksum",
        as_of="2026-03-01T00:00:00Z",
        resolver=Resolver(),
    )
    assert [row.assertion_id for row in result.formation_path] == ["a1", "a2"]
    assert result.lifecycle_path[0].event_id == "event-1"
    assert result.current_value_checksum == checksum(20)
    assert result.explanation_checksum == checksum({
        key: value for key, value in asdict(result).items() if key != "explanation_checksum"
    })
    assert not hasattr(result, "recommendation")


def test_state_explanation_abstains_when_any_lineage_ref_is_unavailable() -> None:
    state = _state("a1", 10, steps=(_step("a1", 10, valid_from="2026-01-01T00:00:00Z"),))
    result = explain_state(
        state,
        snapshot_id="ss_test", snapshot_hash="snapshot-hash",
        run_id="psr-1", run_checksum="run-checksum",
        as_of="2026-02-01T00:00:00Z",
        resolver=Resolver({"msg:a1": "missing"}),
    )
    assert result.abstained is True
    assert result.current_value_checksum == checksum(10)
    assert "evidence_unavailable_or_ineligible" in result.uncertainty
