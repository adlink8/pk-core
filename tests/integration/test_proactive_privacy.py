from __future__ import annotations

from dataclasses import replace

import pytest

from personal_knowledge.intelligence.proactive.ranking import DEFAULT_RANKING_POLICY, rank_candidates
from personal_knowledge.intelligence.proactive.schema import CandidateDraft, SupportReference, checksum


def test_transport_contract_has_no_delivery_or_write_operations() -> None:
    from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
    forbidden = ("notify", "send", "schedule", "execute", "dispatch", "write")
    assert not any(token in operation for operation in ProactiveIntelligenceService.READ_OPERATIONS for token in forbidden)


def _draft() -> CandidateDraft:
    ref = SupportReference("a.personal_change", "change", "chg-1", "1" * 64,
                           "psr-1", "2" * 64, "ss-1", "3" * 64)
    return CandidateDraft("important_change", "inbox_item", "subject", "personal", ("health",),
                          ("change:chg-1",), "2026-07-18T00:00:00Z", "2026-07-20T00:00:00Z",
                          (ref,), 1, 1, 1, 1, 1, 1, 0, "fixture only", ("fixture_only",))


@pytest.mark.parametrize("payload", [
    {"body": "private"}, {"secret": "x"}, {"credential": "x"}, {"recipient": "x"},
    {"webhook": "x"}, {"command": "x"}, {"connector": "x"}, {"send_target": "x"},
])
def test_candidate_payload_rejects_private_and_external_action_fields(payload: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="forbidden_payload"):
        rank_candidates([replace(_draft(), metadata=payload)], policy=DEFAULT_RANKING_POLICY)


def test_mixed_snapshot_or_invalid_support_checksum_is_rejected() -> None:
    draft = _draft()
    other = replace(draft.support_refs[0], record_id="chg-2", snapshot_id="ss-other")
    with pytest.raises(ValueError, match="mixed_snapshot_support"):
        rank_candidates([replace(draft, support_refs=(draft.support_refs[0], other))], policy=DEFAULT_RANKING_POLICY)
    with pytest.raises(ValueError, match="support_checksum_invalid"):
        rank_candidates([replace(draft, support_refs=(replace(draft.support_refs[0], record_checksum="bad"),))], policy=DEFAULT_RANKING_POLICY)


def test_policy_version_and_checksum_tamper_fail_closed() -> None:
    item = rank_candidates([_draft()], policy=DEFAULT_RANKING_POLICY)[0]
    assert item.policy_version == "v1"
    assert checksum(item.payload) == item.payload_checksum
    with pytest.raises(ValueError, match="policy_version_invalid"):
        rank_candidates([_draft()], policy=replace(DEFAULT_RANKING_POLICY, version=""))
