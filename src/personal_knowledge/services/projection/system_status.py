"""system.status.get — 本地服务端口、知识库状态、权威库只读性与 supervisor 历史观察投影。

成功响应只证明 REST 可达;其余观察逐条绑定来源,互不继承可达性状态。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.retrieval.unified_search import get_knowledge_status
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    AUTHORITY_DB_PATHS,
    _PORT_PROBES,
    _REST_PORT,
    _SUPERVISOR_STALE_AFTER,
    _SUPERVISOR_STATE_PATH,
    _collect,
    _db_readable,
    _envelope,
    _port_up,
    _utc_now,
)


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    generated_at = _utc_now()
    limitations: list[str] = []
    loaders: dict[str, Callable[[], dict[str, Any]]] = {
        "ports": _ports_section,
        "knowledge": _knowledge_status_section,
        "authority_dbs": _authority_dbs_section,
    }
    sections, authorities = _collect(loaders, limitations, lambda _name, _section: False)
    knowledge = sections.get("knowledge") or {}
    sections["observations"] = _runtime_observations(sections, generated_at)
    sections["supervisor_state"] = _supervisor_state_section(generated_at)
    snapshot_bindings = {
        "personal": None,
        "external": None,
        "serving": knowledge.get("serving_snapshot_id"),
    }
    freshness = {
        "personal_as_of": None,
        "knowledge_unit_count": knowledge.get("unit_count"),
        "generated_at": generated_at,
    }
    return _envelope(
        "system.status.get", generated_at, sections, authorities, limitations,
        snapshot_bindings, freshness,
    )


def _ports_section() -> dict[str, Any]:
    ports = {"rest": {"up": True, "port": _REST_PORT}}
    for name, port in _PORT_PROBES.items():
        ports[name] = {"up": _port_up(port), "port": port}
    return ports


def _runtime_observations(sections: Mapping[str, Any], observed_at: str) -> list[dict[str, Any]]:
    """Return independent, source-bound runtime observations.

    A successful Cockpit request proves only REST reachability. Other rows are
    deliberately separate and never inherit REST's state.
    """
    observations: list[dict[str, Any]] = [{
        "id": "rest_request", "label": "REST 当前响应", "state": "healthy",
        "source": "Cockpit projection request", "observed_at": observed_at,
        "scope": "本次只读请求", "recovery_hint": "无需恢复；该观察不证明其它组件健康。",
    }]
    ports = sections.get("ports") or {}
    for key, label in (("mcp", "MCP listener"), ("tunnel", "Tunnel listener")):
        up = (ports.get(key) or {}).get("up") is True
        observations.append({
            "id": key, "label": label, "state": "reachable_only" if up else "unavailable",
            "source": "local TCP listener probe", "observed_at": observed_at,
            "scope": "listener only；未证明 endpoint readiness",
            "recovery_hint": "仅可达；请使用对应服务自身的健康检查。" if up else "确认对应服务是否运行；Cockpit 不提供启停操作。",
        })
    knowledge = sections.get("knowledge") or {}
    chroma_ok = knowledge.get("chroma_available") is True
    observations.append({
        "id": "chroma", "label": "Chroma collection probe",
        "state": "healthy" if chroma_ok else ("unknown" if knowledge.get("available") else "unavailable"),
        "source": "knowledge status + Chroma collection probe", "observed_at": observed_at,
        "scope": "active collection read/count", "recovery_hint": "检查 active collection 与 Chroma 服务；Cockpit 只读。",
    })
    for name, db in (sections.get("authority_dbs") or {}).items():
        readable = db.get("readable") is True
        observations.append({
            "id": f"authority:{name}", "label": f"Authority {name} readability",
            "state": "healthy" if readable else "unavailable",
            "source": "read-only SQLite probe", "observed_at": observed_at,
            "scope": "database readability only；freshness/as-of 未提供",
            "recovery_hint": "检查该 authority 的只读可用性；不在 Cockpit 中修改数据。",
        })
        observations.append({
            "id": f"authority:{name}:freshness", "label": f"Authority {name} freshness",
            "state": "unknown", "source": "authority metadata unavailable",
            "observed_at": observed_at, "scope": "snapshot/as-of freshness",
            "recovery_hint": "需要 authority 自身提供 snapshot/as-of 才能确认新鲜度。",
        })
    return observations


def _supervisor_state_section(observed_at: str) -> dict[str, Any]:
    """Expose only sanitized historical supervisor observation."""
    base = {
        "state": "unknown", "source": "supervisor saved state", "observed_at": None,
        "scope": "historical last observation; not current ownership/readiness",
        "recovery_hint": "重新执行受控健康检查；Cockpit 不读取 PID，也不控制进程。",
        "services": [],
    }
    try:
        raw = json.loads(_SUPERVISOR_STATE_PATH.read_text(encoding="utf-8"))
        updated_at = raw.get("updated_at") if isinstance(raw, dict) else None
        if not isinstance(updated_at, str):
            return base
        stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
        services = []
        for item in raw.get("services", []) if isinstance(raw, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("service"), str):
                services.append({"service": item["service"], "healthy": item.get("healthy") is True})
        return {**base, "state": "healthy" if age <= _SUPERVISOR_STALE_AFTER else "stale_observation",
                "observed_at": updated_at, "services": services}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return base


def _knowledge_status_section() -> dict[str, Any]:
    status = get_knowledge_status(probe_chroma=True)
    keys = (
        "available", "active_collection", "unit_count", "serving_snapshot_id",
        "snapshot_hash", "snapshot_drift", "pointer_exists",
    )
    section = {key: status.get(key) for key in keys}
    # chroma 实测字段(chroma 未起时以 chroma_error 形式返回)照实带上
    for key in ("chroma_available", "chroma_port", "chroma_error", "db_unit_count"):
        if key in status:
            section[key] = status.get(key)
    return section


def _authority_dbs_section() -> dict[str, Any]:
    return {
        name: {
            "path": path.name,
            "exists": path.exists(),
            "readable": _db_readable(path),
        }
        for name, path in AUTHORITY_DB_PATHS.items()
    }
