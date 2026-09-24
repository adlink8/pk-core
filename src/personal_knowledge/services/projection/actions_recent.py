"""actions_recent.get — 近期推荐全链时间线投影(游标分页,最新在前)。

逐条组装 history→timeline 并复用 decision_workspace 的 history / outcomes /
effectiveness 只读节;单条失败隔离为内联 error(D-36-06 allowlisted 文案)。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _collect,
    _envelope,
    _safe_failure_error,
    _safe_failure_message,
    _utc_now,
)
from .decision_workspace import _workspace_history, _workspace_typed

# actions_recent.get 的固定词表与上限
# 单页显示上限,同时作为游标分页的每页大小;recommendations.list 现在 newest-first
# 返回,超出部分经 next_cursor 暴露给前端「加载更早」,不再用升序尾窗 hack。
_ACTIONS_RECENT_MAX = 10
_TIMELINE_STAGES = (
    "recommendation", "decision", "action_start",
    "action_complete", "outcome", "effectiveness",
)
# event_type → stage(action 事件例外,按 typed record 的 action_state 细分)
_EVENT_TYPE_TO_STAGE = {
    "recommendation_published": "recommendation",
    "confirmation": "decision",
    "outcome": "outcome",
    "assessment": "effectiveness",
}
_ACTION_STATE_TO_STAGE = {"started": "action_start", "completed": "action_complete"}
# 时间线条目保留的推荐卡字段(沿用 recommendations.list 真实字段名)
_ACTION_CARD_KEYS = (
    "recommendation_id", "domain", "recommendation_kind",
    "confirmation_state", "action_state", "expires_at",
)


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    db = Path(db) if db else UNIFIED_DB
    return _actions_recent_get(db, **params)


def _actions_recent_get(
    db_path: Path, *, cursor: str | None = None, limit: int | None = None,
) -> dict[str, Any]:
    generated_at = _utc_now()
    limitations: list[str] = [
        "recommendations.history 不暴露事件时间戳/status,timeline 仅含链上校验字段",
        "时间线按 created_at 降序分页呈现(最新在前),更早记录经 next_cursor 加载,非全量快照",
    ]
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "decision": lambda: _actions_recent_section(
            db_path, limitations, cursor=cursor, limit=limit,
        ),
    }
    sections, authorities = _collect(loaders, limitations, _actions_recent_empty)
    # data 恒为完整形状,decision 节失败时退化为全零(authorities/limitations 表达降级)
    data = sections.get("decision") or _empty_actions_recent()
    snapshot_bindings = {"personal": None, "external": None, "serving": None}
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "actions_recent.get", generated_at, data, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _actions_recent_empty(_name: str, section: dict[str, Any]) -> bool:
    return not section.get("total_available")


def _empty_actions_recent() -> dict[str, Any]:
    return {
        "total_available": 0, "shown": 0,
        "with_outcome": 0, "awaiting_outcome": 0, "items": [],
        "next_cursor": None,
    }


def _empty_timeline() -> list[dict[str, Any]]:
    return [
        {"stage": stage, "present": False, "event_id": None, "sequence": None, "checksum": None}
        for stage in _TIMELINE_STAGES
    ]


def _build_timeline(
    history: list[dict[str, Any]], action_states: Mapping[str, str],
) -> list[dict[str, Any]]:
    """把一条 recommendation 的 history 折成六阶段时间线(纯函数,便于单测)。

    六阶段键恒在;每阶段引用链上首个匹配事件,无该阶段 → present=False 其余 None。
    映射词表见模块 docstring;action 事件经 action_states(action_id → action_state)
    细分,planned/abandoned/not_taken 不占 stage,词表外 event_type 一律忽略。
    """
    chosen: dict[str, Mapping[str, Any]] = {}
    for event in history:
        event_type = str(event.get("event_type") or "")
        if event_type == "action":
            state = action_states.get(str(event.get("typed_record_id") or ""))
            stage = _ACTION_STATE_TO_STAGE.get(state or "")
        else:
            stage = _EVENT_TYPE_TO_STAGE.get(event_type)
        if stage and stage not in chosen:
            chosen[stage] = event
    timeline = []
    for stage in _TIMELINE_STAGES:
        event = chosen.get(stage)
        timeline.append({
            "stage": stage,
            "present": event is not None,
            "event_id": event.get("event_id") if event else None,
            "sequence": event.get("sequence") if event else None,
            "checksum": event.get("payload_checksum") if event else None,
        })
    return timeline


def _actions_recent_section(
    db_path: Path, limitations: list[str], *, cursor: str | None = None, limit: int | None = None,
) -> dict[str, Any]:
    list_limit = int(limit) if limit is not None else _ACTIONS_RECENT_MAX
    result = DecisionFeedbackService(db_path).invoke(
        "recommendations.list", limit=list_limit, cursor=cursor,
    )
    if not result.get("ok"):
        # fail-closed:游标非法/读取失败 → 单节降级为 error,详情不进入公开响应
        raise ValueError(str((result.get("error") or {}).get("code") or "decision_read_failed"))
    data = result["data"]
    items = list(data.get("items") or [])
    next_cursor = data.get("next_cursor")
    total_available = data.get("total_available", len(items))
    action_states = _action_states(db_path)
    section = _empty_actions_recent()
    section["total_available"] = total_available
    section["next_cursor"] = next_cursor
    if cursor and total_available:
        # 分页视图:total_available 为全量计数,items 仅含本页,避免「过期窗口误导」
        limitations.append(
            "当前为分页视图(已加载更早记录),total_available 为全量计数,items 仅含本页"
        )
    for item in items:  # items 已由 recommendations.list 按 created_at 降序返回
        card = {key: item.get(key) for key in _ACTION_CARD_KEYS}
        rid = str(item.get("recommendation_id") or "")
        try:
            entry = _actions_recent_item(db_path, rid, card, action_states)
        except Exception:  # noqa: BLE001 — 单条组装失败必须被隔离,详情不进入公开响应
            limitations.append(
                _safe_failure_message(f"recommendation {rid}", "item_assembly_failed")
            )
            entry = {
                **card, "timeline": _empty_timeline(),
                "outcomes": [], "effectiveness": [],
                "error": _safe_failure_error("item_assembly_failed"),
            }
        else:
            if entry["outcomes"]:
                section["with_outcome"] += 1
            elif str(item.get("action_state") or "") == "completed":
                section["awaiting_outcome"] += 1
        section["items"].append(entry)
    section["shown"] = len(section["items"])
    return section


def _actions_recent_item(
    db_path: Path, rid: str, card: dict[str, Any], action_states: Mapping[str, str],
) -> dict[str, Any]:
    """组装单条推荐的全链时间线条目。

    复用 decision_workspace 的三个只读节函数(history / outcomes /
    effectiveness),不另起一套调用;失败抛异常由调用方隔离成单条 error。
    """
    history = _workspace_history(db_path, rid)
    outcomes = _workspace_typed(db_path, rid, "recommendations.outcomes")
    effectiveness = _workspace_typed(db_path, rid, "recommendations.effectiveness")
    return {
        **card,
        "timeline": _build_timeline(history, action_states),
        "outcomes": outcomes,
        "effectiveness": effectiveness,
    }


def _action_states(db_path: Path) -> dict[str, str]:
    """一次性只读取 action_id → action_state。

    history 只暴露链上六字段(无 action_state),timeline 的 action_start /
    action_complete 细分只需状态词,故用单条 mode=ro SQL 取全量映射
    (同 _outcome_counts 先例);明细仍以 recommendations.history 为准。
    """
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT action_id, action_state FROM decision_actions"
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    finally:
        con.close()
