"""personal_state.get — 个人状态断言、生命周期分布与近期变更投影(metadata-only)。

只暴露 key/status/confidence/provenance_class 等元数据,不含明文断言值。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.proactive.schema import CANONICAL_DOMAINS
from personal_knowledge.intelligence.schema import (
    ASSERTION_KINDS,
    ASSERTION_LIFECYCLES,
    PROVENANCE_CLASSES,
)
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _collect,
    _envelope,
    _intelligence_data_or_raise,
    _safe_failure_message,
    _utc_now,
)

# personal_state.get 的固定词表与上限
_ASSERTION_KINDS = tuple(sorted(ASSERTION_KINDS))
_PROVENANCE_CLASSES = tuple(sorted(PROVENANCE_CLASSES))
_ASSERTION_LIFECYCLES = tuple(sorted(ASSERTION_LIFECYCLES))
# IntelligenceService 的 limit 硬上限为 MAX_HISTORY_LIMIT=100
_STATE_LIMIT = 100
_CHANGES_LIMIT = 20
_DOMAIN_ASSERTIONS_MAX = 20


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    db = Path(db) if db else UNIFIED_DB
    generated_at = _utc_now()
    limitations: list[str] = []
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "state": lambda: _personal_state_detail(db, limitations),
        "changes": lambda: _recent_changes_detail(db),
    }
    sections, authorities = _collect(loaders, limitations, _personal_state_empty)
    # data 为扁平复合形状,失败节贡献零值字段(partial + limitations 表达降级)
    state = sections.get("state") or {}
    changes = sections.get("changes") or {}
    data = {
        "snapshot_id": state.get("snapshot_id"),
        "as_of": state.get("as_of"),
        "total_available": state.get("total_available", 0),
        "history_total_available": state.get("history_total_available"),
        "domains": state.get("domains") or _empty_domains(),
        "lifecycle_counts": state.get("lifecycle_counts") or _zero_lifecycle_counts(),
        "recent_changes": changes.get("items") or [],
    }
    snapshot_bindings = {
        "personal": state.get("snapshot_id"),
        "external": None,
        "serving": None,
    }
    freshness = {
        "personal_as_of": state.get("as_of"),
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "personal_state.get", generated_at, data, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _personal_state_empty(_name: str, section: dict[str, Any]) -> bool:
    return not section.get("total_available")


def _empty_domain_bucket() -> dict[str, Any]:
    return {
        "total": 0,
        "by_kind": {kind: 0 for kind in _ASSERTION_KINDS},
        "by_provenance": {cls: 0 for cls in _PROVENANCE_CLASSES},
        "conflicts": 0,
        "assertions": [],
    }


def _empty_domains() -> dict[str, Any]:
    return {domain: _empty_domain_bucket() for domain in CANONICAL_DOMAINS}


def _zero_lifecycle_counts() -> dict[str, int]:
    return {status: 0 for status in _ASSERTION_LIFECYCLES}


def _personal_state_detail(db_path: Path, limitations: list[str]) -> dict[str, Any]:
    service = IntelligenceService(db_path)
    result = service.invoke("state.current", limit=_STATE_LIMIT)
    data = _intelligence_data_or_raise(result, "personal_read_failed")
    if data is None:
        return {
            "snapshot_id": None, "as_of": None, "total_available": 0,
            "history_total_available": None,
            "domains": _empty_domains(), "lifecycle_counts": _zero_lifecycle_counts(),
        }
    items = list(data.get("items") or [])
    total_available = data.get("total_available", len(items))
    domains = _empty_domains()
    lifecycle_counts = _zero_lifecycle_counts()
    skipped_domains = 0
    for item in items:
        key = item.get("key") or {}
        status = str(item.get("status") or "")
        if status in lifecycle_counts:
            lifecycle_counts[status] += 1
        bucket = domains.get(str(key.get("domain") or ""))
        if bucket is None:
            skipped_domains += 1
            continue
        bucket["total"] += 1
        kind = str(key.get("assertion_kind") or "")
        if kind in bucket["by_kind"]:
            bucket["by_kind"][kind] += 1
        provenance = str(item.get("provenance_class") or "")
        if provenance in bucket["by_provenance"]:
            bucket["by_provenance"][provenance] += 1
        if status == "conflict":
            bucket["conflicts"] += 1
        if len(bucket["assertions"]) < _DOMAIN_ASSERTIONS_MAX:
            bucket["assertions"].append({
                "key": key,
                "provenance_class": item.get("provenance_class"),
                "status": item.get("status"),
                "confidence": item.get("confidence"),
                "current_assertion_id": item.get("current_assertion_id"),
                # snapshot 绑定见 data.snapshot_id(单快照全局一致);checksum 与
                # current_assertion_id 一起构成 evidence.resolve 的稳定引用三元组
                # (Phase 37:EVID-01),不额外暴露断言明文值
                "current_value_checksum": item.get("current_value_checksum"),
                "evidence_count": len(item.get("evidence_status") or []),
            })
    if skipped_domains:
        limitations.append(
            f"{skipped_domains} 条断言的 domain 不在固定八域词表内,未计入 domains 分桶"
        )
    if total_available > len(items):
        limitations.append(
            f"state.current 仅取前 {len(items)}/{total_available} 条,"
            "domains/lifecycle_counts 为截断统计"
        )
    history_total: Any = None
    try:
        history = service.invoke("state.history", limit=1)
        if history.get("ok"):
            history_total = (history.get("data") or {}).get("total_available")
        else:
            limitations.append(
                "state.history 计数读取失败(%s)"
                % str((history.get("error") or {}).get("code") or "unknown")
            )
    except Exception:  # noqa: BLE001 — 历史计数降级不拖垮 state 节,详情不进入公开响应
        limitations.append(_safe_failure_message("state.history", "history_count_unavailable"))
    return {
        "snapshot_id": (result.get("snapshot") or {}).get("snapshot_id"),
        "as_of": data.get("as_of"),
        "total_available": total_available,
        "history_total_available": history_total,
        "domains": domains,
        "lifecycle_counts": lifecycle_counts,
    }


def _recent_changes_detail(db_path: Path) -> dict[str, Any]:
    result = IntelligenceService(db_path).invoke("changes.recent", limit=_CHANGES_LIMIT)
    data = _intelligence_data_or_raise(result, "changes_read_failed")
    if data is None:
        return {"total_available": 0, "items": []}
    items = [
        {
            "record_id": item.get("record_id"),
            "record_type": item.get("record_type"),
            "status": item.get("status"),
            "domain": (item.get("key") or {}).get("domain"),
            "subject": (item.get("key") or {}).get("subject"),
            "effective_at": item.get("effective_at"),
        }
        for item in list(data.get("items") or [])
    ]
    return {"total_available": data.get("total_available", len(items)), "items": items}
