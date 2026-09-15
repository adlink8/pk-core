from __future__ import annotations

from dataclasses import replace

from personal_knowledge.intelligence.proactive.coordination import COORDINATION_RULES, coordinate_goals
from personal_knowledge.intelligence.proactive.schema import CANONICAL_DOMAINS, GoalSignal, ResourceClaim, SupportReference, canonical_json


def _ref(record: str = "a1", *, snapshot: str = "ss1") -> SupportReference:
    return SupportReference("a.personal_change", "assertion", record, "a" * 64, "run1", "b" * 64, snapshot, "snapshot-hash")


def _goal(goal_id: str, domain: str, *, target: str = "", resources=(), **changes) -> GoalSignal:
    values = dict(goal_id=goal_id, domain=domain, subject="user", scope="personal", target=target,
                  valid_from="2026-07-01T00:00:00Z", valid_to="2026-08-01T00:00:00Z",
                  observed_at="2026-07-17T00:00:00Z", confidence=0.9, uncertainty="explicit fixture",
                  support=_ref(goal_id), resources=tuple(resources))
    values.update(changes)
    return GoalSignal(**values)


def _resource(goal_id: str, kind: str, amount: float, capacity: float, *, unit: str = "hours",
              resource_id: str | None = None, source: SupportReference | None = None) -> ResourceClaim:
    return ResourceClaim(kind, amount, unit, "2026-07-18T00:00:00Z", "2026-07-25T00:00:00Z", capacity,
                         source or _ref(goal_id), kind == "budget", resource_id or f"shared:{kind}")


def test_rule_registry_covers_all_relation_types() -> None:
    assert set(COORDINATION_RULES) == {"goal_support", "goal_conflict", "dependency", "resource_competition", "risk_propagation", "opportunity"}


def test_bounded_time_and_energy_conflicts_are_deterministic() -> None:
    goals = [_goal("career", "career", resources=[_resource("career", "time", 7, 10)]),
             _goal("project", "project", resources=[_resource("project", "time", 6, 10)]),
             _goal("health", "health", resources=[_resource("health", "energy", 7, 10)]),
             _goal("learning", "learning", resources=[_resource("learning", "energy", 6, 10)])]
    first = coordinate_goals(goals, as_of="2026-07-18T00:00:00Z")
    second = coordinate_goals(reversed(goals), as_of="2026-07-18T00:00:00Z")
    assert canonical_json(first) == canonical_json(second)
    conflicts = [item for item in first.items if item.relation_type == "goal_conflict"]
    assert {item.domains for item in conflicts} >= {("career", "project"), ("learning", "health")}


def test_all_eight_domains_and_cross_domain_opportunity() -> None:
    goals = [_goal(domain, domain, target="one explicit compatible action") for domain in CANONICAL_DOMAINS]
    result = coordinate_goals(goals, as_of="2026-07-18T00:00:00Z")
    covered = {domain for item in result.items for domain in item.domains}
    assert covered == set(CANONICAL_DOMAINS)
    assert all(item.relation_type == "opportunity" for item in result.items)


def test_coexistence_missing_resource_and_incompatible_units_abstain() -> None:
    coexist = coordinate_goals([_goal("a", "career"), _goal("b", "project")], as_of="2026-07-18T00:00:00Z")
    assert not coexist.items and coexist.abstentions[0].reason_code == "no_bounded_conflict_evidence"
    bad_units = coordinate_goals([
        _goal("a", "career", resources=[_resource("a", "time", 9, 10, unit="hours")]),
        _goal("b", "project", resources=[_resource("b", "time", 9, 10, unit="days")]),
    ], as_of="2026-07-18T00:00:00Z")
    assert bad_units.abstentions[0].reason_code == "incompatible_resource_units"


def test_sensitive_future_expired_conflicted_and_cross_snapshot_inputs_abstain() -> None:
    cases = [
        (_goal("s", "health", sensitive=True), "sensitive_or_insufficient"),
        (_goal("f", "finance", confidence=0.5), "sensitive_or_insufficient"),
        (_goal("x", "career", observed_at="2026-07-20T00:00:00Z"), "future_observation"),
        (_goal("e", "career", valid_to="2026-07-18T00:00:00Z"), "expired_input"),
        (_goal("c", "career", unresolved_conflict=True), "unresolved_source_conflict"),
    ]
    for goal, reason in cases:
        result = coordinate_goals([goal], as_of="2026-07-18T00:00:00Z")
        assert result.abstentions[0].reason_code == reason
    mixed = coordinate_goals([_goal("a", "career", target="same"), replace(_goal("b", "project", target="same"), support=_ref("b", snapshot="ss2"))], as_of="2026-07-18T00:00:00Z")
    assert mixed.abstentions[0].reason_code == "cross_snapshot_input"


def test_resource_identity_and_source_are_decisive_support() -> None:
    left = _goal("a", "career", resources=[_resource("a", "time", 8, 10, resource_id="calendar:week")])
    right = _goal("b", "project", resources=[_resource("b", "time", 8, 10, resource_id="calendar:week")])
    result = coordinate_goals([left, right], as_of="2026-07-18T00:00:00Z")
    assert len(result.items) == 1
    assert {ref.record_id for ref in result.items[0].source_refs} == {"a", "b"}

    other_resource = replace(right.resources[0], resource_id="different-calendar")
    mismatched_identity = coordinate_goals([left, replace(right, resources=(other_resource,))], as_of="2026-07-18T00:00:00Z")
    assert not mismatched_identity.items
    assert mismatched_identity.abstentions[0].reason_code == "resource_identity_mismatch"

    forged = replace(right.resources[0], source=replace(right.resources[0].source, snapshot_id="other-snapshot"))
    bad_source = coordinate_goals([left, replace(right, resources=(forged,))], as_of="2026-07-18T00:00:00Z")
    assert not bad_source.items
    assert bad_source.abstentions[0].reason_code == "resource_source_mismatch"
