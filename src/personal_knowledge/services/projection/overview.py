"""overview.get — 五只读权威一屏总览投影。

聚合 personal / decision / proactive / external / knowledge 五个权威节,返回
前端可直接渲染的投影信封;单权威失败被 _collect 隔离为 error + limitation。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.retrieval.unified_search import get_knowledge_status
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _collect,
    _envelope,
    _intelligence_data_or_raise,
    _utc_now,
)

_TOP_ITEMS = 10


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    db = Path(db) if db else UNIFIED_DB
    read_service = read_service or DecisionIntelligenceReadService()
    generated_at = _utc_now()
    limitations: list[str] = []
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "personal": lambda: _personal_section(db),
        "decision": lambda: _decision_section(db),
        "proactive": lambda: _proactive_section(db),
        "external": lambda: _external_section(read_service),
        "knowledge": lambda: _knowledge_section(),
    }
    sections, authorities = _collect(loaders, limitations, _overview_empty)
    personal = sections.get("personal") or {}
    external = sections.get("external") or {}
    knowledge = sections.get("knowledge") or {}
    snapshot_bindings = {
        "personal": personal.get("snapshot_id"),
        "external": external.get("snapshot_id"),
        "serving": knowledge.get("serving_snapshot_id"),
    }
    freshness = {
        "personal_as_of": personal.get("as_of"),
        "knowledge_unit_count": knowledge.get("unit_count"),
        "generated_at": generated_at,
    }
    return _envelope(
        "overview.get", generated_at, sections, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _overview_empty(name: str, section: dict[str, Any]) -> bool:
    if name in {"personal", "decision", "proactive"}:
        return not section.get("total_available")
    if name == "external":
        return not (section.get("sources_count") or section.get("facts_count"))
    return not section.get("unit_count")


def _personal_section(db_path: Path) -> dict[str, Any]:
    result = IntelligenceService(db_path).invoke("state.current", limit=50)
    data = _intelligence_data_or_raise(result, "personal_read_failed")
    if data is None:
        return {
            "snapshot_id": None, "as_of": None, "total_available": 0,
            "domains": {}, "status_counts": {}, "top_items": [],
        }
    items = list(data.get("items") or [])
    domains: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for item in items:
        key = item.get("key") or {}
        domain = str(key.get("domain") or "unknown")
        domains[domain] = domains.get(domain, 0) + 1
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    top_items = [
        {
            "key": item.get("key"),
            "status": item.get("status"),
            "confidence": item.get("confidence"),
            "provenance_class": item.get("provenance_class"),
        }
        for item in items[:_TOP_ITEMS]
    ]
    return {
        "snapshot_id": (result.get("snapshot") or {}).get("snapshot_id"),
        "as_of": data.get("as_of"),
        "total_available": data.get("total_available", len(items)),
        "domains": domains,
        "status_counts": status_counts,
        "top_items": top_items,
    }


def _decision_section(db_path: Path) -> dict[str, Any]:
    result = DecisionFeedbackService(db_path).invoke("recommendations.list", limit=20)
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "decision_read_failed"))
    data = result["data"]
    items = list(data.get("items") or [])
    queue: dict[str, int] = {}
    for item in items:
        state = str(item.get("confirmation_state") or "unknown")
        queue[state] = queue.get(state, 0) + 1
    return {
        "total_available": data.get("total_available", len(items)),
        "queue": queue,
        "items": [
            {
                "recommendation_id": item.get("recommendation_id"),
                "domain": item.get("domain"),
                "recommendation_kind": item.get("recommendation_kind"),
                "horizon": item.get("horizon"),
                "confidence": item.get("confidence"),
                "confirmation_state": item.get("confirmation_state"),
                "action_state": item.get("action_state"),
                "expires_at": item.get("expires_at"),
            }
            for item in items[:_TOP_ITEMS]
        ],
    }


def _proactive_section(db_path: Path) -> dict[str, Any]:
    result = ProactiveIntelligenceService(db_path).invoke("inbox.list", limit=10)
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "proactive_read_failed"))
    data = result["data"]
    items = list(data.get("items") or [])
    return {
        "total_available": data.get("total_available", len(items)),
        "items": [
            {
                "candidate_id": item.get("candidate_id"),
                "domains": item.get("domains"),
                "importance": item.get("importance"),
                "candidate_class": item.get("candidate_class"),
                "expires_at": item.get("expires_at"),
                "reason_codes": item.get("reason_codes"),
            }
            for item in items[:_TOP_ITEMS]
        ],
    }


def _external_section(read_service: DecisionIntelligenceReadService) -> dict[str, Any]:
    result = read_service.invoke("external.list", limit=10)
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "external_read_failed"))
    data = result["data"]
    snapshot = data.get("snapshot") or {}
    return {
        "snapshot_id": snapshot.get("snapshot_id"),
        "sources_count": len(data.get("sources") or []),
        "facts_count": len(data.get("facts") or []),
    }


def _knowledge_section() -> dict[str, Any]:
    status = get_knowledge_status(probe_chroma=False)
    return {
        "active_collection": status.get("active_collection"),
        "unit_count": status.get("unit_count") or status.get("db_unit_count"),
        "serving_snapshot_id": status.get("serving_snapshot_id"),
    }
