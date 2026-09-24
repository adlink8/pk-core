"""decision_queue.get — 决策看板六 stage 投影(proposed 优先,needs_attention 就近)。

stage 归类为纯函数 ``_classify_stage``(真实状态词表读自
intelligence/decision/state_machine.py),词表外 confirmation_state 一律保守
归 needs_attention(D-36-05),不得凭 action_state 被提升为可执行 stage。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _QUEUE_LIMIT,
    _collect,
    _envelope,
    _parse_iso,
    _utc_now,
)

# decision_queue.get 的固定词表与上限
_STAGE_KEYS = (
    "needs_attention", "awaiting_confirmation", "in_progress",
    "awaiting_outcome", "completed", "closed",
)
# proposed 且 expires_at 距 now 不足该窗口(或已过 / 无法解析)→ needs_attention
_NEEDS_ATTENTION_WINDOW = timedelta(hours=72)
# confirmation_state 已发布真实词表(读自 intelligence/decision/state_machine.py);
# 词表外的值一律不进入 action_state 驱动的 in_progress/awaiting_outcome 等可执行分支
# (D-36-05:Projection 不得凭空提升未知/伪造的 confirmation_state)
_KNOWN_CONFIRMATION_STATES = frozenset({"proposed", "accepted", "rejected", "deferred", "revoked"})
# 队列卡片保留的关键字段(沿用 recommendations.list 真实字段名)
_QUEUE_CARD_KEYS = (
    "recommendation_id", "domain", "recommendation_kind", "horizon", "confidence",
    "confirmation_state", "action_state", "expires_at", "current_sequence", "snapshot_id",
)


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    db = Path(db) if db else UNIFIED_DB
    generated_at = _utc_now()
    limitations: list[str] = []
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "decision": lambda: _decision_queue_section(db, limitations),
    }
    sections, authorities = _collect(loaders, limitations, _decision_queue_empty)
    # data 恒为完整看板形状,decision 节失败时退化为全零(authorities/limitations 表达降级)
    data = sections.get("decision") or _empty_decision_queue()
    snapshot_bindings = {"personal": None, "external": None, "serving": None}
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "decision_queue.get", generated_at, data, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _decision_queue_empty(_name: str, section: dict[str, Any]) -> bool:
    return not section.get("total_available")


def _empty_decision_queue() -> dict[str, Any]:
    return {
        "total_available": 0,
        "stage_counts": {key: 0 for key in _STAGE_KEYS},
        "stages": {key: [] for key in _STAGE_KEYS},
    }


def _classify_stage(item: Mapping[str, Any], now: datetime) -> str:
    """把一条 recommendation 卡归入六个看板 stage 之一(纯函数,便于单测)。

    真实状态词汇(读自 intelligence/decision/state_machine.py):
    - confirmation_state ∈ {proposed, accepted, rejected, deferred, revoked}
    - action_state ∈ {None, planned, started, completed, abandoned, not_taken}
      (terminal: completed / abandoned / not_taken;outcome 只允许挂在 terminal action 上)
    - has_outcome 由调用方注入(decision_outcomes 是否已有记录)

    映射规则(按优先级):
    1. confirmation_state ∈ {rejected, deferred, revoked} → closed
    2. has_outcome → completed(已有 outcome,反馈闭环完成)
    3. confirmation_state 不在已发布词表内(词表见上)→ 保守 needs_attention,
       不进入下列任何 action_state 驱动分支——action 只能在 accepted 之后产生,
       词表外 confirmation 与非空 action_state 的组合视为数据异常,不得被提升
       为 in_progress/awaiting_outcome 等可执行/进行中 stage(D-36-05 词表锁定)
    4. action_state == completed 且无 outcome → awaiting_outcome
    5. action_state ∈ {abandoned, not_taken} 且无 outcome → closed
    6. action_state ∈ {planned, started} → in_progress
    7. confirmation_state == accepted(action 尚未开始)→ in_progress
    8. proposed 且 expires_at 已过 / 72h 内到期 / 无法解析 → needs_attention
    9. proposed 其余 → awaiting_confirmation
    10. 无法识别的组合保守归 needs_attention
    """
    confirmation = str(item.get("confirmation_state") or "")
    action_raw = item.get("action_state")
    action = str(action_raw) if action_raw else None
    if confirmation in {"rejected", "deferred", "revoked"}:
        return "closed"
    if bool(item.get("has_outcome")):
        return "completed"
    if confirmation not in _KNOWN_CONFIRMATION_STATES:
        return "needs_attention"
    if action == "completed":
        return "awaiting_outcome"
    if action in {"abandoned", "not_taken"}:
        return "closed"
    if action in {"planned", "started"}:
        return "in_progress"
    if confirmation == "accepted":
        return "in_progress"
    if confirmation == "proposed":
        expires = _parse_iso(item.get("expires_at"))
        if expires is None or expires <= now + _NEEDS_ATTENTION_WINDOW:
            return "needs_attention"
        return "awaiting_confirmation"
    return "needs_attention"


def _decision_queue_section(db_path: Path, limitations: list[str]) -> dict[str, Any]:
    result = DecisionFeedbackService(db_path).invoke(
        "recommendations.list", limit=_QUEUE_LIMIT,
    )
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "decision_read_failed"))
    data = result["data"]
    items = list(data.get("items") or [])
    total_available = data.get("total_available", len(items))
    outcome_counts = _outcome_counts(db_path)
    now = datetime.now(timezone.utc)
    queue = _empty_decision_queue()
    queue["total_available"] = total_available
    for item in items:
        card = {key: item.get(key) for key in _QUEUE_CARD_KEYS}
        has_outcome = outcome_counts.get(str(item.get("recommendation_id") or ""), 0) > 0
        stage = _classify_stage({**card, "has_outcome": has_outcome}, now)
        queue["stages"][stage].append(card)
        queue["stage_counts"][stage] += 1
    if total_available > len(items):
        limitations.append(
            f"recommendations.list 仅取前 {len(items)}/{total_available} 条,"
            "stage_counts/stages 为截断统计"
        )
    return queue


def _outcome_counts(db_path: Path) -> dict[str, int]:
    """一次性只读统计各 recommendation 的 outcome 数。

    逐条调 recommendations.outcomes 会对每条重跑全链校验(N+1),看板只需
    has_outcome 布尔,故用单条 mode=ro SQL 聚合;明细仍以 recommendations.outcomes 为准。
    """
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT recommendation_id, COUNT(*) FROM decision_outcomes"
            " GROUP BY recommendation_id"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
    finally:
        con.close()
