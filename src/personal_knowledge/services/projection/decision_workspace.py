"""decision_workspace.get — 单条 recommendation 工作区投影(recommendation + 全链 + 支持)。

四个权威节(recommendation / history / outcomes / effectiveness)由 _collect 隔离;
history 只暴露链上六字段(无时间戳/status),action 细分由 decision_actions 聚合映射。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _QUEUE_LIMIT,
    _collect,
    _envelope,
    _error,
    _utc_now,
)

# decision_workspace.get 的固定词表与上限
_WORKSPACE_TYPED_LIMIT = 50
# recommendations.history 真实暴露的链上字段(该接口不暴露时间戳 / status)
_HISTORY_EVENT_KEYS = (
    "event_id", "sequence", "event_type", "typed_record_id",
    "previous_event_checksum", "payload_checksum",
)


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    db = Path(db) if db else UNIFIED_DB
    return _decision_workspace_get(db, **params)


def _decision_workspace_get(
    db_path: Path, recommendation_id: Any = None, **_params: Any,
) -> dict[str, Any]:
    rid = str(recommendation_id or "").strip()
    if not rid:
        return _error(
            "decision_workspace.get", "invalid_input", "recommendation_id 必填",
        )
    generated_at = _utc_now()
    limitations: list[str] = [
        "recommendations.history 不暴露事件时间戳/status,history 仅含链上校验字段",
    ]
    loaders: dict[str, Callable[[], Any]] = {
        "recommendation": lambda: _workspace_recommendation(db_path, rid),
        "history": lambda: _workspace_history(db_path, rid),
        "outcomes": lambda: _workspace_typed(db_path, rid, "recommendations.outcomes"),
        "effectiveness": lambda: _workspace_typed(db_path, rid, "recommendations.effectiveness"),
    }
    sections, authorities = _collect(loaders, limitations, _workspace_empty)
    recommendation = sections.get("recommendation")
    data = {
        "recommendation": recommendation,
        "history": sections.get("history") or [],
        "outcomes": sections.get("outcomes") or [],
        "effectiveness": sections.get("effectiveness") or [],
        "linked_analysis_run_id": _linked_analysis_run_id(recommendation),
    }
    snapshot_bindings = {
        "personal": (recommendation or {}).get("snapshot_id"),
        "external": None,
        "serving": None,
    }
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "decision_workspace.get", generated_at, data, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _workspace_empty(name: str, section: Any) -> bool:
    if name == "recommendation":
        return False
    return not section


def _workspace_recommendation(db_path: Path, rid: str) -> dict[str, Any]:
    result = DecisionFeedbackService(db_path).invoke(
        "recommendations.get", recommendation_id=rid,
    )
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "decision_read_failed"))
    return dict(result["data"])


def _workspace_history(db_path: Path, rid: str) -> list[dict[str, Any]]:
    result = DecisionFeedbackService(db_path).invoke(
        "recommendations.history", recommendation_id=rid, limit=_QUEUE_LIMIT,
    )
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "decision_read_failed"))
    return [
        {key: event.get(key) for key in _HISTORY_EVENT_KEYS}
        for event in list(result["data"].get("items") or [])
    ]


def _workspace_typed(db_path: Path, rid: str, operation: str) -> list[dict[str, Any]]:
    result = DecisionFeedbackService(db_path).invoke(
        operation, recommendation_id=rid, limit=_WORKSPACE_TYPED_LIMIT,
    )
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "decision_read_failed"))
    return [dict(item) for item in list(result["data"].get("items") or [])]


def _linked_analysis_run_id(recommendation: Any) -> str | None:
    """从 support[] 提取上游 analysis run;真实词表中 support 条目的
    authority_id 恒为 a.personal_change(无 "analysis" 值),其 source_run_id
    即来源分析 run;support 缺失时回退 recommendation.source_run_id。"""
    if not isinstance(recommendation, Mapping):
        return None
    for entry in recommendation.get("support") or ():
        if isinstance(entry, Mapping) and entry.get("source_run_id"):
            return str(entry["source_run_id"])
    source_run_id = recommendation.get("source_run_id")
    return str(source_run_id) if source_run_id else None
