from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LayerAccount:
    layer: str
    seen: int
    emitted: int
    dropped: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationFinding:
    layer: str
    reason: str
    detail: str


@dataclass(frozen=True)
class ReconciliationVerdict:
    passed: bool
    findings: tuple[ReconciliationFinding, ...] = ()

    @property
    def blocked_reason(self) -> str | None:
        if self.passed or not self.findings:
            return None
        return self.findings[0].reason


def _dropped_total(account: LayerAccount) -> int:
    return sum(account.dropped.values())


def _is_closed(account: LayerAccount) -> bool:
    return account.seen == account.emitted + _dropped_total(account)


DISCOVERY_LAYER = "discovery"


def account_from_discovery_ledger(ledger) -> LayerAccount:
    """把发现层台账翻译成一条层账（鸭子类型，不 import 适配器层）。"""
    dropped: dict[str, int] = {}
    for item in ledger.unclaimed:
        reason = item.reason
        dropped[reason] = dropped.get(reason, 0) + 1
    return LayerAccount(
        layer=DISCOVERY_LAYER,
        seen=ledger.candidates,
        emitted=ledger.claimed,
        dropped=dropped,
    )


def reconcile(accounts: Sequence[LayerAccount]) -> ReconciliationVerdict:
    # 只做层内算术闭合；跨层相等在真实数据上不成立（一条原生记录可分解出多条事件）。
    findings: list[ReconciliationFinding] = []
    for account in accounts:
        if _is_closed(account):
            continue
        emitted_total = account.emitted
        dropped_total = _dropped_total(account)
        delta = account.seen - (emitted_total + dropped_total)
        findings.append(
            ReconciliationFinding(
                layer=account.layer,
                reason="layer_not_closed",
                detail=(
                    f"layer={account.layer} seen={account.seen} "
                    f"emitted={emitted_total} dropped={dropped_total} delta={delta}"
                ),
            )
        )
    return ReconciliationVerdict(passed=not findings, findings=tuple(findings))
