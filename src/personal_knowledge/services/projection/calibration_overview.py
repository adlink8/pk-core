"""calibration_overview.get — 校准协议列表 + explain 摘要投影。

单 protocol explain 失败被隔离为内联 error(D-36-06 allowlisted 文案),其余照常。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _collect,
    _envelope,
    _safe_failure_error,
    _safe_failure_message,
    _utc_now,
)

# calibration_overview.get 的固定上限
_CALIBRATION_LIST_LIMIT = 100
_CALIBRATION_EXPLAIN_MAX = 10


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    read_service = read_service or DecisionIntelligenceReadService()
    generated_at = _utc_now()
    limitations: list[str] = []
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "calibration": lambda: _calibration_overview_section(read_service, limitations),
    }
    sections, authorities = _collect(loaders, limitations, _calibration_overview_empty)
    # data 恒为完整形状,calibration 节失败时退化为全零(authorities/limitations 表达降级)
    data = sections.get("calibration") or {"total": 0, "shown": 0, "protocols": []}
    snapshot_bindings = {"personal": None, "external": None, "serving": None}
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "calibration_overview.get", generated_at, data, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _calibration_overview_empty(_name: str, section: dict[str, Any]) -> bool:
    return not section.get("total")


def _calibration_summary(protocol_id: str, view: Mapping[str, Any]) -> dict[str, Any]:
    """从 calibration.explain 真实视图裁剪单 protocol 摘要(纯函数,便于单测)。

    真实结构(intelligence/calibration/service.py explain):{protocol, cohort, arms,
    measurements, verdicts, proposals, limitations, causal_claim, promotion_available,
    external_action_available};protocol 行含 protocol_status 列;verdict 行含
    verdict_status 列(PASS/FAIL/INCONCLUSIVE),payload.reason_codes 为判定原因词表
    (INCONCLUSIVE 时即 inconclusive 原因);sample_size 由 cohort 行数派生
    (视图无独立字段);summary_limitations 原样保留视图固定 limitations。
    """
    protocol_rows = list(view.get("protocol") or [])
    verdicts = list(view.get("verdicts") or [])
    verdict_row = verdicts[0] if verdicts else {}
    verdict_payload = verdict_row.get("payload") or {}
    return {
        "protocol_id": protocol_id,
        "status": protocol_rows[0].get("protocol_status") if protocol_rows else None,
        "verdict": verdict_row.get("verdict_status"),
        "causal_claim": view.get("causal_claim"),
        "promotion_available": view.get("promotion_available"),
        "external_action_available": view.get("external_action_available"),
        "inconclusive_reasons": list(verdict_payload.get("reason_codes") or []),
        "sample_size": len(list(view.get("cohort") or [])),
        "summary_limitations": list(view.get("limitations") or []),
    }


def _calibration_overview_section(
    read_service: DecisionIntelligenceReadService, limitations: list[str],
) -> dict[str, Any]:
    result = read_service.invoke("calibration.list", limit=_CALIBRATION_LIST_LIMIT)
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "calibration_read_failed"))
    data = result["data"]
    ids = [
        str(item.get("protocol_id") or "")
        for item in list(data.get("items") or [])
    ]
    # calibration.list 无 total_available,count 即 limit 窗口内行数
    total = data.get("count", len(ids))
    if total >= _CALIBRATION_LIST_LIMIT:
        limitations.append(
            f"calibration.list 达到上限 {_CALIBRATION_LIST_LIMIT},total 为下界"
        )
    protocols: list[dict[str, Any]] = []
    for pid in ids[:_CALIBRATION_EXPLAIN_MAX]:
        try:
            view_result = read_service.invoke("calibration.explain", protocol_id=pid)
            if not view_result.get("ok"):
                raise ValueError(str(
                    (view_result.get("error") or {}).get("code") or "calibration_read_failed"
                ))
            protocols.append(_calibration_summary(pid, view_result["data"]))
        except Exception:  # noqa: BLE001 — 单 protocol 失败必须被隔离,详情不进入公开响应
            limitations.append(
                _safe_failure_message(f"protocol {pid}", "protocol_explain_failed")
            )
            protocols.append({
                "protocol_id": pid, "status": None, "verdict": None,
                "causal_claim": None, "inconclusive_reasons": [],
                "sample_size": 0, "summary_limitations": [],
                "error": _safe_failure_error("protocol_explain_failed"),
            })
    if total > len(protocols):
        limitations.append(
            f"calibration.explain 仅覆盖前 {len(protocols)}/{total} 个 protocol,其余只计数"
        )
    return {"total": total, "shown": len(protocols), "protocols": protocols}
