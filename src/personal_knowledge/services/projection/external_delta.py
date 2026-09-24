"""external_delta.get — 外部上下文快照 sources/facts/delta 投影(canonical External DTO)。

lifecycle 与 freshness 是两套独立语义:lifecycle 为 External 权威自身发布的记录
状态;freshness 为相对 active snapshot activated_at 派生的独立到期判断。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _collect,
    _envelope,
    _parse_iso,
    _utc_now,
)

# external_delta.get 的固定词表与上限
_EXTERNAL_FACTS_LIMIT = 100
_DELTA_WINDOW_DAYS = 7

# 每 fact 的 freshness 词表(Phase 37:D-37-02 canonical External DTO)。
# lifecycle 是 External 权威自身发布的记录状态(current/stale/superseded/conflict/invalid);
# freshness 是相对当前 active snapshot 参考时刻(activated_at)派生的独立到期判断,
# 二者不得合并成同一个客户端颜色字段。
_FRESHNESS_LEVELS = frozenset({"unknown", "valid", "expiring_soon", "expired"})


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    read_service = read_service or DecisionIntelligenceReadService()
    generated_at = _utc_now()
    limitations: list[str] = []
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "external": lambda: _external_delta_section(read_service, limitations),
    }
    sections, authorities = _collect(loaders, limitations, _external_delta_empty)
    section = sections.get("external")
    snapshot_bindings = {
        "personal": None,
        "external": (section or {}).get("snapshot", {}).get("snapshot_id"),
        "serving": None,
    }
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": None,
        "generated_at": generated_at,
    }
    return _envelope(
        "external_delta.get", generated_at, section, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _external_delta_empty(_name: str, section: dict[str, Any]) -> bool:
    return not (section.get("sources") or section.get("facts"))


def _external_delta_section(
    read_service: DecisionIntelligenceReadService, limitations: list[str],
) -> dict[str, Any]:
    result = read_service.invoke("external.list", limit=_EXTERNAL_FACTS_LIMIT)
    if not result.get("ok"):
        raise ValueError(str((result.get("error") or {}).get("code") or "external_read_failed"))
    data = result["data"]
    snapshot_raw = data.get("snapshot") or {}
    sources = [
        {
            "source_id": source.get("source_id"),
            "authority_role": source.get("authority_role"),
            "source_type": source.get("source_type"),
            "topic": source.get("topic"),
            "region": source.get("region"),
            "endpoint": source.get("endpoint"),
        }
        for source in list(data.get("sources") or [])
    ]
    raw_facts = list(data.get("facts") or [])
    reference = _parse_iso(snapshot_raw.get("activated_at"))
    if reference is None:
        limitations.append(
            "active snapshot 缺少可解析的 activated_at,delta 分类与逐 fact freshness 全部保守留空/unknown"
        )
    window = timedelta(days=_DELTA_WINDOW_DAYS)
    fact_ids = [str(fact.get("fact_id")) for fact in raw_facts if fact.get("fact_id")]
    source_ids_by_fact = _external_fact_source_ids(fact_ids)
    facts: list[dict[str, Any]] = []
    for fact in raw_facts:
        lifecycle = str(fact.get("lifecycle") or "")
        fact_id = str(fact.get("fact_id") or "")
        facts.append({
            "fact_id": fact.get("fact_id"),
            # canonical External DTO(Phase 37:D-37-02):subject/predicate 命名轴 +
            # 固定 来源/地区/有效期/quality/confidence/lifecycle/conflict/freshness 字段,
            # 不再与 fact_type/observed_at/source_id 两套互相冲突的字段并存
            "fact_checksum": fact.get("fact_checksum"),
            "subject": fact.get("subject"),
            "predicate": fact.get("predicate"),
            "region": fact.get("region"),
            "valid_from": fact.get("valid_from"),
            "valid_to": fact.get("valid_to"),
            "source_quality": fact.get("source_quality"),
            "fact_confidence": fact.get("fact_confidence"),
            "source_ids": source_ids_by_fact.get(fact_id, []),
            "lifecycle": fact.get("lifecycle"),
            "conflict": lifecycle == "conflict",
            "freshness": _fact_freshness(fact.get("valid_to"), reference, window),
        })
    delta: dict[str, list[Any]] = {"new": [], "updated": [], "expiring": [], "conflicts": []}
    if reference is not None:
        for fact in facts:
            valid_from = _parse_iso(fact.get("valid_from"))
            if valid_from is not None and abs(reference - valid_from) <= window:
                delta["new"].append(fact["fact_id"])
            valid_to = _parse_iso(fact.get("valid_to"))
            if valid_to is not None and valid_to <= reference + window:
                delta["expiring"].append(fact["fact_id"])
            if fact["conflict"]:
                delta["conflicts"].append(fact["fact_id"])
    limitations.append("external.list 不暴露逐 fact 更新事件,delta.updated 恒为空数组")
    return {
        "snapshot": {
            "snapshot_id": snapshot_raw.get("snapshot_id"),
            "snapshot_hash": snapshot_raw.get("snapshot_hash"),
            "activated_at": snapshot_raw.get("activated_at"),
        },
        "sources": sources,
        "facts": facts,
        "delta": delta,
        "counts": {
            "sources": len(sources),
            "facts": len(facts),
            "conflicts": len(delta["conflicts"]),
        },
    }


def _fact_freshness(
    valid_to: Any, reference: datetime | None, window: timedelta,
) -> dict[str, Any]:
    if reference is None:
        return {"level": "unknown", "reason": "active snapshot 缺少可解析的 activated_at"}
    valid_to_dt = _parse_iso(valid_to)
    if valid_to_dt is None:
        return {"level": "valid", "reason": None}
    if valid_to_dt <= reference:
        return {"level": "expired", "reason": "valid_to 早于 snapshot 参考时间(activated_at)"}
    if valid_to_dt <= reference + window:
        return {
            "level": "expiring_soon",
            "reason": f"valid_to 距 snapshot 参考时间不足 {_DELTA_WINDOW_DAYS} 天",
        }
    return {"level": "valid", "reason": None}


def _external_fact_source_ids(fact_ids: list[str]) -> dict[str, list[str]]:
    """一次性只读聚合 fact_id → 支撑 observation 的 source_id 列表(同 _outcome_counts 先例)。

    external_facts 表本身不直接持有 source_id;来源身份需经
    external_fact_support → external_observations 关联还原,单条 explain
    (facts.get)不做该 join,故这里用一次 mode=ro 批量查询取代 N+1。
    """
    if not fact_ids:
        return {}
    con = sqlite3.connect(f"file:{EXTERNAL_CONTEXT_DB.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        placeholders = ",".join("?" for _ in fact_ids)
        rows = con.execute(
            "SELECT fs.fact_id, o.source_id FROM external_fact_support fs "
            "JOIN external_observations o ON o.observation_id = fs.observation_id "
            f"WHERE fs.fact_id IN ({placeholders})",
            fact_ids,
        ).fetchall()
        grouped: dict[str, set[str]] = {}
        for fact_id, source_id in rows:
            grouped.setdefault(str(fact_id), set()).add(str(source_id))
        return {key: sorted(values) for key, values in grouped.items()}
    finally:
        con.close()
