"""Personal Decision Cockpit 只读 UI 投影层(overview / system status / personal state /
external delta / decision queue / decision workspace / actions recent /
proactive summary / calibration overview / evidence resolve)。

本文件是纯注册表:把每个 projection 的 operation 名映射到
``src/personal_knowledge/services/projection/`` 包内独立模块的 ``build`` 函数
(拆分自 CONCERNS.md OC-5)。REST 适配器见 services/api_server.py 的
``ui_rest_contract``,路由 ``/ui/overview``、``/ui/system/status``、
``/ui/personal-state``、``/ui/external/delta``、``/ui/decision-queue``、
``/ui/decision/workspace?recommendation_id=<id>``、``/ui/actions/recent``、
``/ui/proactive/summary``、``/ui/calibration/overview`` 与
``/ui/evidence/resolve?subject_type=&stable_id=&snapshot_id=&checksum=``
(personal_state 另需 ``assertion_kind/subject/domain/scope/predicate``)。

统一信封(schema_version = decision_cockpit_projection_v1):

    {schema_version, operation, ok, generated_at, snapshot_bindings,
     freshness, authorities, partial, limitations, data}

单权威失败不拖垮整体:该节 data 置 None、authorities[x]="error"、
limitations 追加中文说明,其余节照常;partial=True 标记降级,绝不伪装成功。

对外契约(硬约束,禁止变更):``CockpitProjectionService`` 类名、``invoke``
签名(``invoke(operation, **params)``)、所有 operation 名与返回 dict 结构均与
拆分前完全一致;模块级 ``AUTHORITY_DB_PATHS`` / ``INTERFACE_SCHEMA_VERSION`` 及
测试依赖的纯函数 ``_classify_stage`` / ``_build_timeline`` 仍从本模块暴露。

硬边界:
- 只读:所有 SQLite 访问一律 mode=ro + query_only,不写任何库
- 不调 provider、不做 promote、不创建 Recommendation、不改任何 lifecycle
- 保留所有 authority ID(recommendation_id / candidate_id / protocol_id / snapshot_id)
- metadata-only:personal 节只暴露 key/status/confidence/provenance_class,不含明文值
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from .projection._shared import (
    AUTHORITY_DB_PATHS,
    INTERFACE_SCHEMA_VERSION,
    _error,
)
from .projection.actions_recent import _build_timeline, build as _actions_recent_build
from .projection.calibration_overview import build as _calibration_overview_build
from .projection.decision_queue import _classify_stage, build as _decision_queue_build
from .projection.decision_workspace import build as _decision_workspace_build
from .projection.evidence_resolve import build as _evidence_resolve_build
from .projection.external_delta import build as _external_delta_build
from .projection.overview import build as _overview_build
from .projection.personal_state import build as _personal_state_build
from .projection.proactive_summary import build as _proactive_summary_build
from .projection.system_status import build as _system_status_build


class CockpitProjectionService:
    """Read-only cockpit projection over the five read authorities.

    纯注册表:operation 名 → projection 包内 ``build(db, read_service, params)``
    的映射;``invoke`` 签名与返回 envelope 结构与拆分前完全一致。
    """

    def __init__(
        self,
        db_path: Path | None = None,
        read_service: DecisionIntelligenceReadService | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else None
        self.read_service = read_service or DecisionIntelligenceReadService()

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        builder = _REGISTRY.get(operation)
        if builder is None:
            return _error(operation, "unknown_operation", operation)
        return builder(self.db_path, self.read_service, params)


# operation → projection.build 注册表(拆分自原 CockpitProjectionService 的
# 字符串反射分派;operation 名与返回 envelope 不变)。
_REGISTRY: dict[str, Callable[[Path | None, DecisionIntelligenceReadService | None, dict[str, Any]], dict[str, Any]]] = {
    "overview.get": _overview_build,
    "system.status.get": _system_status_build,
    "personal_state.get": _personal_state_build,
    "external_delta.get": _external_delta_build,
    "decision_queue.get": _decision_queue_build,
    "decision_workspace.get": _decision_workspace_build,
    "actions_recent.get": _actions_recent_build,
    "proactive_summary.get": _proactive_summary_build,
    "calibration_overview.get": _calibration_overview_build,
    "evidence_resolve.get": _evidence_resolve_build,
}


__all__ = [
    "AUTHORITY_DB_PATHS",
    "CockpitProjectionService",
    "INTERFACE_SCHEMA_VERSION",
]
