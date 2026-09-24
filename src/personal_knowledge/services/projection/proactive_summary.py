"""proactive_summary.get — 主动候选 inbox 分组(inbox / metrics 双节)投影。

inbox 候选按 importance.final_score 相对 ranking policy threshold 归入
now / deferrable;score 缺失/非数值保守归 deferrable + limitation,不臆造分组。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.proactive.ranking import DEFAULT_RANKING_POLICY
from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _collect,
    _envelope,
    _utc_now,
)

# proactive_summary.get 的固定上限与卡片字段(沿用 inbox.list 真实字段名)
_PROACTIVE_INBOX_LIMIT = 50
_PROACTIVE_CARD_KEYS = (
    "candidate_id", "domains", "candidate_class", "presentation_kind", "importance",
    "expires_at", "valid_from", "reason_codes",
    "current_control_eligible", "current_control_reason_codes",
    "control_as_of", "control_history", "control_frontier_checksum",
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
        "inbox": lambda: _proactive_inbox_section(db, limitations),
        "metrics": lambda: _proactive_metrics_section(db),
    }
    sections, authorities = _collect(loaders, limitations, _proactive_summary_empty)
    # data 恒为完整复合形状,失败节贡献零值字段(partial + limitations 表达降级)
    inbox = sections.get("inbox") or {}
    data = {
        "total_available": inbox.get("total_available", 0),
        "groups": inbox.get("groups") or _empty_proactive_groups(),
        "metrics": sections.get("metrics"),
        "notes": [
            "suppressed/cooldown 状态需按 candidate_id 调 /proactive/controls/status "
            "逐条查询(前端按需)",
            "now/deferrable 按 importance.final_score >= ranking policy "
            f"threshold({DEFAULT_RANKING_POLICY.threshold}) 分组,仅覆盖 eligible 候选",
        ],
    }
    snapshot_bindings = {"personal": None, "external": None, "serving": None}
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "proactive_summary.get", generated_at, data, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _proactive_summary_empty(name: str, section: dict[str, Any]) -> bool:
    if name == "inbox":
        return not section.get("total_available")
    return False


def _empty_proactive_groups() -> dict[str, list[Any]]:
    return {"now": [], "deferrable": []}


def _proactive_inbox_section(db_path: Path, limitations: list[str]) -> dict[str, Any]:
    result = ProactiveIntelligenceService(db_path).invoke(
        "inbox.list", limit=_PROACTIVE_INBOX_LIMIT,
    )
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "proactive_read_failed"))
    data = result["data"]
    items = list(data.get("items") or [])
    total_available = data.get("total_available", len(items))
    if total_available > len(items):
        limitations.append(
            f"inbox.list 仅取前 {len(items)}/{total_available} 条,分组为截断统计"
        )
    groups = _empty_proactive_groups()
    unscored = 0
    for item in items:
        card = {key: item.get(key) for key in _PROACTIVE_CARD_KEYS}
        # controls.status 是同一 proactive authority 的只读 overlay；把既有
        # append-only history 带到卡片，避免页面用本地状态猜测 suppression/restore。
        try:
            control = ProactiveIntelligenceService(db_path).invoke(
                "controls.status", candidate_id=str(item.get("candidate_id") or "")
            )
        except Exception:  # noqa: BLE001 — detail read failure is section-local
            control = {"ok": False}
        if control.get("ok"):
            control_data = control.get("data") or {}
            card["control_as_of"] = control_data.get("as_of")
            card["control_history"] = list(control_data.get("history") or [])
            card["control_frontier_checksum"] = control_data.get("frontier_checksum")
        else:
            card["control_as_of"] = None
            card["control_history"] = []
            card["control_frontier_checksum"] = None
            limitations.append("部分候选 control history 暂不可用")
        score = (item.get("importance") or {}).get("final_score")
        # importance 结构不确定时保守归 deferrable + limitation,不臆造分组
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            unscored += 1
            groups["deferrable"].append(card)
        elif score >= DEFAULT_RANKING_POLICY.threshold:
            groups["now"].append(card)
        else:
            groups["deferrable"].append(card)
    if unscored:
        limitations.append(
            f"{unscored} 条候选的 importance.final_score 缺失/非数值,保守归入 deferrable"
        )
    return {"total_available": total_available, "groups": groups}


def _proactive_metrics_section(db_path: Path) -> dict[str, Any]:
    result = ProactiveIntelligenceService(db_path).invoke("metrics.get")
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "proactive_read_failed"))
    # metrics.get 的 data 全为 metadata-only 计数字段,原样保留
    return dict(result["data"])
