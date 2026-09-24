from personal_knowledge.adapters.conversation_sources.discovery import (
    DiscoveryLedger,
    UnclaimedFile,
)
from personal_knowledge.application.conversation.authority_reconcile import (
    LayerAccount,
    account_from_discovery_ledger,
    reconcile,
)


def test_layer_that_closes_passes():
    verdict = reconcile([
        LayerAccount(layer="adapt", seen=10, emitted=8, dropped={"empty_native": 2}),
    ])
    assert verdict.passed is True
    assert verdict.findings == ()


def test_layer_that_loses_rows_is_blocked_and_names_the_layer():
    verdict = reconcile([
        LayerAccount(layer="adapt", seen=10, emitted=8, dropped={}),
    ])
    assert verdict.passed is False
    assert len(verdict.findings) == 1
    finding = verdict.findings[0]
    assert finding.layer == "adapt"
    assert finding.reason == "layer_not_closed"
    assert "10" in finding.detail
    assert "8" in finding.detail


def test_discovery_ledger_with_no_unclaimed_is_a_closed_layer():
    ledger = DiscoveryLedger(scanned_roots=2, candidates=3, claimed=3)

    account = account_from_discovery_ledger(ledger)

    assert account.layer == "discovery"
    assert account.seen == 3
    assert account.emitted == 3
    assert account.dropped == {}
    assert reconcile([account]).passed is True


def test_discovery_ledger_aggregates_drop_reasons_and_stays_closed():
    ledger = DiscoveryLedger(
        scanned_roots=4,
        candidates=7,
        claimed=3,
        unclaimed=[
            UnclaimedFile("codex", "a.jsonl", "not_this_family"),
            UnclaimedFile("codex", "b.jsonl", "not_this_family"),
            UnclaimedFile("codex", "c.jsonl", "not_this_family"),
            UnclaimedFile("codex", "d.jsonl", "probe_error"),
        ],
    )

    account = account_from_discovery_ledger(ledger)

    assert account.seen == 7
    assert account.emitted == 3
    assert account.dropped == {"not_this_family": 3, "probe_error": 1}
    assert account.seen == account.emitted + sum(account.dropped.values())
    assert reconcile([account]).passed is True


def test_empty_discovery_ledger_is_a_closed_layer():
    account = account_from_discovery_ledger(DiscoveryLedger())

    assert account.seen == 0
    assert account.emitted == 0
    assert account.dropped == {}
    assert reconcile([account]).passed is True
