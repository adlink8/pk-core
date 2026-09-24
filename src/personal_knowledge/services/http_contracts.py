"""REST 适配函数层 —— 把 D/S/R/A 各领域服务封装成薄薄的 HTTP 契约函数。

从 api_server.py 抽出(第一阶段拆分,OC-1)。保持对外行为与名称不变:
api_server.py 仍 re-export 全部 *_rest_contract 符号,既有 `from ...api_server import X_rest_contract`
与 monkeypatch(如 `api_server.orchestration_rest_contract = fake`)照常生效。

关键约束:
- 依赖 api_server 模块命名空间的符号(backend / TopicProjectionService / WIKI_DERIVED_STORE
  等)一律在函数体内延迟解析(运行时才取值),保证测试 monkeypatch 不失效,也避免循环导入。
- 所有函数返回 dict 信封,HTTP 状态码由调用方决定。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
from personal_knowledge.services.agent_contract import compact_envelope
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService
from personal_knowledge.services.orchestration_service import GuardedOrchestrationInterface
from personal_knowledge.services.ui_projection import CockpitProjectionService


def intelligence_rest_contract(
    operation: str,
    params: dict,
    *,
    db_path: Path | None = None,
    resolver=None,
) -> dict:
    """Thin REST adapter over the shared intelligence service."""
    values = {key: value for key, value in params.items() if value not in {None, ""}}
    if "limit" in values:
        try:
            values["limit"] = int(values["limit"])
        except (TypeError, ValueError):
            return IntelligenceService._error(operation, "invalid_limit", str(values["limit"]))
    return IntelligenceService(db_path or UNIFIED_DB, resolver=resolver).invoke(
        operation, **values
    )


def decision_rest_contract(
    operation: str,
    params: dict,
    *,
    db_path: Path | None = None,
) -> dict:
    """Thin read-only REST adapter over the shared decision service."""
    values = {key: value for key, value in params.items() if value not in {None, ""}}
    if "limit" in values:
        try:
            values["limit"] = int(values["limit"])
        except (TypeError, ValueError):
            return DecisionFeedbackService._error(
                operation, "invalid_limit", str(values["limit"])
            )
    return DecisionFeedbackService(db_path or UNIFIED_DB).invoke(operation, **values)


def proactive_rest_contract(operation: str, params: dict, *, db_path: Path | None = None) -> dict:
    """Thin read-only REST adapter over proactive intelligence."""
    values = {key: value for key, value in params.items() if value not in {None, ""}}
    if "limit" in values:
        try:
            values["limit"] = int(values["limit"])
        except (TypeError, ValueError):
            return ProactiveIntelligenceService._error(operation, "invalid_limit", str(values["limit"]))
    return ProactiveIntelligenceService(db_path or UNIFIED_DB).invoke(operation, **values)


def agent_read_rest_contract(
    operation: str, params: dict, *, service: DecisionIntelligenceReadService | None = None,
) -> dict:
    """Thin REST adapter for Phase 28-31 read authorities."""
    values = {key: value for key, value in params.items() if value not in {None, ""}}
    return compact_envelope((service or DecisionIntelligenceReadService()).invoke(operation, **values))


def orchestration_rest_contract(
    operation: str, params: dict, *, service: GuardedOrchestrationInterface | None = None,
) -> dict:
    """Thin REST adapter over the shared guarded orchestration contract."""
    try:
        target = service or GuardedOrchestrationInterface()
    except Exception as exc:
        code = str(getattr(exc, "code", "") or str(exc) or "service_unavailable").split(":", 1)[0]
        return compact_envelope(GuardedOrchestrationInterface._envelope(operation, ok=False, code=code))
    return compact_envelope(target.invoke(operation, **params))


def ui_rest_contract(
    operation: str, params: dict, *, service: CockpitProjectionService | None = None,
) -> dict:
    """Thin read-only REST adapter over the cockpit UI projection."""
    return (service or CockpitProjectionService()).invoke(operation, **params)


def topic_rest_contract(
    operation: str, params: dict, *, service=None,
) -> dict:
    """Thin GET-only adapter over the Wiki topic projection service.

    TopicProjectionService / WIKI_DERIVED_STORE 延迟从 api_server 解析,
    保持测试对 api_server.TopicProjectionService 的 monkeypatch 生效。
    生产路径注入 page_reader，让 topic_get/topic.list 优先读统合页面正文
    （Phase 4 wiki-first）；缺失/损坏时由服务内部回退读时现算。
    """
    import personal_knowledge.services.api_server as api_server
    from personal_knowledge.wiki.materialization import WikiMaterializer
    from personal_knowledge.wiki.page_reader import WikiPageReader

    values = {key: value for key, value in params.items() if value not in {None, ""}}
    if "limit" in values:
        try:
            values["limit"] = int(values["limit"])
        except (TypeError, ValueError):
            return api_server.TopicProjectionService()._envelope_error(operation, "invalid_topic_key", limitations=["limit 无效"])
    if service is not None:
        target = service
    else:
        target = api_server.TopicProjectionService(
            materializer=WikiMaterializer(api_server.WIKI_DERIVED_STORE),
            page_reader=WikiPageReader(api_server.WIKI_DERIVED_STORE),
        )
    return target.invoke(operation, **values)
