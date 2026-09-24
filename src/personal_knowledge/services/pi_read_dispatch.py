"""Pi 域只读工具的真实数据 read_handler 分发 + warehouse metadata 工厂。

v2.0 Phase 55-57 接线层。``pi_domain_gateway`` 的只读分支（未注册到 warehouse /
semantic / retrieval / snapshot / mutation 工具组的 project read operations）在
未注入 ``read_handler`` 时返回 ``{"status":"synthetic",...}`` 占位。本模块提供：

1. ``read_handler(operation, params)`` —— 按 operation 分发到既有真实数据源
   （IntelligenceService / DecisionIntelligenceReadService /
   TopicProjectionService / semantic_search / unified_db 直读 / 运行时探针）。
   单 authority 失败返回 typed error envelope，库缺失 / 服务未启动降级
   synthetic marker，绝不抛穿到 gateway。
2. ``build_real_warehouse_metadata()`` —— 从真实库统计构造 WarehouseTools 的
   per-authority metadata（记录数 / 失败批次 / 质量状态）。
3. ``make_real_warehouse_tools()`` —— 带“缺 authority_id 时默认遍历全部
   authority”行为的 WarehouseTools 门面。

隐私边界：所有返回只含契约允许字段（R0 元数据 / R1 聚合），不含原始对话正文、
provider body、凭据或路径。只读、幂等、fail-safe。

约束：本模块不修改 44 工具注册表、pi-skills.json、kernel-host.mjs 或任何引擎。
gateway 的 allowed 参数集是硬约束（本模块只转发 gateway 放行后的干净参数）。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# 让本模块可被 api_server（services/ 下的同级脚本）直接导入
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personal_knowledge.core.project_paths import (  # noqa: E402
    AGENT_CONVERSATIONS_DB,
    EXTERNAL_CONTEXT_DB,
    UNIFIED_DB,
    VAR_DB,
    WIKI_PROJECTION_DB,
)
from personal_knowledge.intelligence.service import IntelligenceService  # noqa: E402
from personal_knowledge.services.decision_intelligence_reads import (  # noqa: E402
    DecisionIntelligenceReadService,
)
from personal_knowledge.services.evidence_sqlite_tool import (  # noqa: E402
    EVIDENCE_SQLITE_OPERATION,
    EVIDENCE_SQLITE_SCHEMA,
    EvidenceSqliteError,
    EvidenceSqliteTool,
)
from personal_knowledge.services.topic_projection import TopicProjectionService  # noqa: E402
from personal_knowledge.services.warehouse_tools import (  # noqa: E402
    AUTHORITY_ADAPTERS,
    SCHEMA_VERSION as WAREHOUSE_SCHEMA_VERSION,
    WarehouseToolError,
    WarehouseTools,
)

DISPATCH_SCHEMA = "pi_read_dispatch_v1"
MAX_LIMIT = 100
_DEFAULT_LIMIT = 50

# gateway 只读分支实际路由到 read_handler 的 operation 集合
READ_DISPATCH_OPERATIONS = frozenset({
    "state.current", "state.changes",
    "decision.list", "decision.get",
    "external.list", "external.get",
    "action_outcome.list",
    "knowledge.search", "knowledge.get",
    "retrieval.search", "retrieval.status",
    "wiki.page", "wiki.directory",
    "evidence.resolve",
    "data_quality.report", "data_quality.failed_batches",
    "system.health", "system.runtime",
})

# 真实服务不可用时降级 synthetic 的权威空状态 code（其余失败一律 typed error）
_SYNTHETIC_CODES = frozenset({"database_missing"})

# 权威自身发布的真实空状态 code：不是读取失败，映射为 empty 而非 error
# （与 projection/_shared.py 的 _INTELLIGENCE_EMPTY_CODES 同一口径：当前 active
# snapshot 尚无已提交 run = “还没跑分析”，不是“读取失败”）。
_EMPTY_CODES = frozenset({"run_missing"})

# system.* 运行时探针端口
_SYSTEM_PORTS = {"api": 8000, "kernel": 8790, "mcp": 8789, "chroma": 8001}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _port_up(port: int, timeout: float = 0.4) -> bool:
    """TCP 探活：只报 up/down，不发 payload，异常即 down。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _db_readable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("SELECT 1")
        finally:
            con.close()
        return True
    except (OSError, sqlite3.Error):
        return False


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _int_param(params: Mapping[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIMIT))


def _str_param(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    return str(value).strip() if value is not None else ""


# === 信封构造 ================================================================

def _capability_checksum(operation: str) -> str | None:
    try:
        from personal_knowledge.services.pi_domain_gateway import (
            OPERATIONS,
            PROJECT_OPERATIONS,
        )

        spec = OPERATIONS.get(operation) or PROJECT_OPERATIONS.get(operation)
        if spec is None:
            return None
        return str(spec.get("checksum") or "")
    except Exception:  # noqa: BLE001 — checksum 只是可观测性补充
        return None


def _envelope(
    operation: str,
    *,
    status: str,
    ok: bool,
    provider: str,
    authority_db: Path | str | None = None,
    payload: Any = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": DISPATCH_SCHEMA,
        "operation": operation,
        "status": status,
        "ok": ok,
        "provider": provider,
    }
    if authority_db is not None:
        out["authority_db"] = str(authority_db)
    if payload is not None:
        out["data"] = payload
    if error is not None:
        out["error"] = dict(error)
    checksum = _capability_checksum(operation)
    if checksum:
        out["capability_checksum"] = checksum
    return out


def _success(
    operation: str,
    provider: str,
    authority_db: Path | str | None,
    payload: Any,
    *,
    status: str = "success",
) -> dict[str, Any]:
    return _envelope(
        operation, status=status, ok=True, provider=provider,
        authority_db=authority_db, payload=payload,
    )


def _error(
    operation: str,
    provider: str,
    authority_db: Path | str | None,
    code: str,
    detail: str = "",
) -> dict[str, Any]:
    return _envelope(
        operation, status="error", ok=False, provider=provider,
        authority_db=authority_db,
        error={"code": code, "detail": detail},
    )


def _synthetic(operation: str, provider: str, authority_db: Path | str | None, reason: str) -> dict[str, Any]:
    return _envelope(
        operation, status="synthetic", ok=False, provider=provider,
        authority_db=authority_db, error={"code": "synthetic", "detail": reason},
    )


def _service_result(
    operation: str, provider: str, authority_db: Path | str | None, result: Any,
    *,
    payload_key: str = "data",
    empty_codes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """把既有服务的信封规整为 dispatch envelope；失败按 code 区分 synthetic / error / empty。"""
    if not isinstance(result, Mapping):
        return _error(operation, provider, authority_db, "provider_unavailable", "服务返回非 envelope 结果")
    if result.get("ok") is not True:
        error = result.get("error") or {}
        code = str(error.get("code") or "provider_unavailable")
        detail = str(error.get("detail") or "")
        if code in _SYNTHETIC_CODES:
            return _synthetic(operation, provider, authority_db, f"{code}: {detail}")
        if code in empty_codes:
            # 权威自身的真实空状态（如尚未发布 personal_state run）
            return _success(operation, provider, authority_db,
                            {"empty": True, "code": code, "detail": detail}, status="empty")
        return _error(operation, provider, authority_db, code, detail)
    status = str(result.get("status") or "success")
    payload = result.get(payload_key) if payload_key else result
    return _success(operation, provider, authority_db, payload, status=status)


# === 各 operation 分发实现 ====================================================

def _intelligence(operation: str, params: Mapping[str, Any], service_operation: str) -> dict[str, Any]:
    db = UNIFIED_DB
    if not _db_readable(db):
        return _synthetic(operation, "intelligence", db, "database_missing: personal_system.sqlite 缺失")
    service = IntelligenceService(db)
    kwargs: dict[str, Any] = {"limit": _int_param(params, "limit", _DEFAULT_LIMIT)}
    snapshot_id = _str_param(params, "snapshot_id")
    if snapshot_id:
        kwargs["snapshot_id"] = snapshot_id
    result = service.invoke(service_operation, **kwargs)
    return _service_result(operation, "intelligence", db, result, empty_codes=_EMPTY_CODES)


def _state_current(params: Mapping[str, Any]) -> dict[str, Any]:
    return _intelligence("state.current", params, "state.current")


def _state_changes(params: Mapping[str, Any]) -> dict[str, Any]:
    # registry 里的 operation 是 state.changes；IntelligenceService 对应实现为
    # changes.recent（同样的 authority，同样的隐私 R1 聚合）。
    return _intelligence("state.changes", params, "changes.recent")


def _decision(operation: str, params: Mapping[str, Any], service_operation: str, **service_params: Any) -> dict[str, Any]:
    analysis_db = VAR_DB / "decision_analysis.sqlite"
    if not _db_readable(analysis_db):
        return _synthetic(operation, "decision_intelligence_reads", analysis_db, "database_missing: decision_analysis.sqlite 缺失")
    result = DecisionIntelligenceReadService().invoke(service_operation, **service_params)
    return _service_result(operation, "decision_intelligence_reads", analysis_db, result)


def _decision_list(params: Mapping[str, Any]) -> dict[str, Any]:
    return _decision("decision.list", params, "analysis.list", limit=_int_param(params, "limit", _DEFAULT_LIMIT))


def _decision_get(params: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _str_param(params, "record_id") or _str_param(params, "query")
    if not run_id:
        return _error("decision.get", "decision_intelligence_reads", VAR_DB / "decision_analysis.sqlite",
                      "missing_parameter", "decision.get 需要 record_id（analysis run_id）")
    return _decision("decision.get", params, "analysis.get", run_id=run_id)


def _external(operation: str, params: Mapping[str, Any], service_operation: str, **service_params: Any) -> dict[str, Any]:
    if not _db_readable(EXTERNAL_CONTEXT_DB):
        return _synthetic(operation, "decision_intelligence_reads", EXTERNAL_CONTEXT_DB, "database_missing: external_context.sqlite 缺失")
    result = DecisionIntelligenceReadService().invoke(service_operation, **service_params)
    return _service_result(operation, "decision_intelligence_reads", EXTERNAL_CONTEXT_DB, result)


def _external_list(params: Mapping[str, Any]) -> dict[str, Any]:
    return _external("external.list", params, "external.list", limit=_int_param(params, "limit", _DEFAULT_LIMIT))


def _external_get(params: Mapping[str, Any]) -> dict[str, Any]:
    resource_id = _str_param(params, "record_id")
    if not resource_id:
        return _error("external.get", "decision_intelligence_reads", EXTERNAL_CONTEXT_DB,
                      "missing_parameter", "external.get 需要 record_id（fact_id）")
    resource_type = _str_param(params, "resource_type") or "fact"
    if resource_type not in {"source", "fact", "snapshot"}:
        return _error("external.get", "decision_intelligence_reads", EXTERNAL_CONTEXT_DB,
                      "invalid_resource_type", "resource_type 必须是 source/fact/snapshot")
    return _external("external.get", params, "external.get", resource_type=resource_type, resource_id=resource_id)


def _action_outcome_list(params: Mapping[str, Any]) -> dict[str, Any]:
    db = UNIFIED_DB
    if not _db_readable(db):
        return _synthetic("action_outcome.list", "unified_db", db, "database_missing: personal_system.sqlite 缺失")
    con = _ro(db)
    try:
        actions_exist = _table_exists(con, "decision_actions")
        outcomes_exist = _table_exists(con, "decision_outcomes")
        if not actions_exist and not outcomes_exist:
            return _success("action_outcome.list", "unified_db", db,
                            {"total_actions": 0, "total_outcomes": 0, "actions_by_state": {},
                             "outcomes_by_adherence": {}, "limit": _int_param(params, "limit", _DEFAULT_LIMIT),
                             "items": []}, status="empty")
        action_rows = con.execute(
            "SELECT action_state, COUNT(*) AS n FROM decision_actions GROUP BY action_state ORDER BY n DESC"
        ).fetchall() if actions_exist else []
        outcome_rows = con.execute(
            "SELECT adherence_status, COUNT(*) AS n FROM decision_outcomes GROUP BY adherence_status ORDER BY n DESC"
        ).fetchall() if outcomes_exist else []
        limit = _int_param(params, "limit", _DEFAULT_LIMIT)
        items = con.execute(
            "SELECT action_id, recommendation_id, action_state, created_at "
            "FROM decision_actions ORDER BY created_at DESC, action_id DESC LIMIT ?", (limit,),
        ).fetchall() if actions_exist else []
    except sqlite3.Error as exc:
        return _error("action_outcome.list", "unified_db", db, "authority_unavailable", str(exc)[:160])
    finally:
        con.close()
    payload = {
        "total_actions": int(sum(row["n"] for row in action_rows)),
        "total_outcomes": int(sum(row["n"] for row in outcome_rows)),
        "actions_by_state": {str(row["action_state"]): int(row["n"]) for row in action_rows},
        "outcomes_by_adherence": {str(row["adherence_status"]): int(row["n"]) for row in outcome_rows},
        "limit": limit,
        "items": [dict(row) for row in items],
    }
    status = "success" if payload["total_actions"] or payload["total_outcomes"] else "empty"
    return _success("action_outcome.list", "unified_db", db, payload, status=status)


def _knowledge_search(params: Mapping[str, Any]) -> dict[str, Any]:
    query = _str_param(params, "query")
    if not query:
        return _error("knowledge.search", "semantic_search", UNIFIED_DB, "missing_parameter", "knowledge.search 需要 query")
    try:
        from personal_knowledge.retrieval.semantic_search import search_knowledge_units
    except Exception as exc:  # noqa: BLE001 — 向量基础设施缺失降级，不崩穿
        return _synthetic("knowledge.search", "semantic_search", UNIFIED_DB, f"semantic backend unavailable: {exc}")
    try:
        result = search_knowledge_units(
            query=query,
            top_k=_int_param(params, "limit", 5),
            include_evidence=False,
        )
    except Exception as exc:  # noqa: BLE001 — 单次检索失败隔离为 typed error
        return _error("knowledge.search", "semantic_search", UNIFIED_DB, "retrieval_unavailable", str(exc)[:160])
    return _success("knowledge.search", "semantic_search", UNIFIED_DB, result)


def _knowledge_get(params: Mapping[str, Any]) -> dict[str, Any]:
    db = UNIFIED_DB
    unit_id = _str_param(params, "record_id") or _str_param(params, "query")
    if not unit_id:
        return _error("knowledge.get", "unified_db", db, "missing_parameter", "knowledge.get 需要 record_id（knowledge unit_id）")
    if not _db_readable(db):
        return _synthetic("knowledge.get", "unified_db", db, "database_missing: personal_system.sqlite 缺失")
    con = _ro(db)
    try:
        row = con.execute(
            "SELECT * FROM canonical_knowledge_units WHERE canonical_unit_id=?",
            (unit_id,),
        ).fetchone()
        table = "canonical_knowledge_units"
        if row is None:
            row = con.execute("SELECT * FROM knowledge_units WHERE unit_id=?", (unit_id,)).fetchone()
            table = "knowledge_units"
    except sqlite3.Error as exc:
        return _error("knowledge.get", "unified_db", db, "authority_unavailable", str(exc)[:160])
    finally:
        con.close()
    if row is None:
        return _error("knowledge.get", "unified_db", db, "record_not_found", unit_id)
    payload = {
        "table": table,
        "unit_id": unit_id,
        "subject": row["subject"] if "subject" in row.keys() else None,
        "unit_type": row["unit_type"] if "unit_type" in row.keys() else None,
        "lifecycle": row["lifecycle"] if "lifecycle" in row.keys() else None,
        "status": row["status"] if "status" in row.keys() else None,
        "version": row["version"] if "version" in row.keys() else None,
        "confidence": row["confidence"] if "confidence" in row.keys() else None,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
        "supersedes_id": row["supersedes_id"] if "supersedes_id" in row.keys() else None,
        "question": str(row["question"] or "")[:500] if "question" in row.keys() else None,
        "answer": str(row["answer"] or "")[:2000] if "answer" in row.keys() else None,
    }
    return _success("knowledge.get", "unified_db", db, payload)


def _retrieval_status(params: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from personal_knowledge.retrieval.semantic_search import get_knowledge_status
    except Exception as exc:  # noqa: BLE001
        return _synthetic("retrieval.status", "semantic_search", UNIFIED_DB, f"semantic backend unavailable: {exc}")
    try:
        result = get_knowledge_status(probe_chroma=False)
    except Exception as exc:  # noqa: BLE001
        return _error("retrieval.status", "semantic_search", UNIFIED_DB, "retrieval_unavailable", str(exc)[:160])
    if isinstance(result, Mapping):
        result = dict(result)
        result["chroma_up"] = _port_up(_SYSTEM_PORTS["chroma"])
        result.setdefault("chroma_port", _SYSTEM_PORTS["chroma"])
    return _success("retrieval.status", "semantic_search", UNIFIED_DB, result)


def _retrieval_search(params: Mapping[str, Any]) -> dict[str, Any]:
    query = _str_param(params, "query")
    if not query:
        return _error("retrieval.search", "semantic_search", UNIFIED_DB, "missing_parameter", "retrieval.search 需要 query")
    try:
        from personal_knowledge.retrieval.semantic_search import search_semantic
    except Exception as exc:  # noqa: BLE001
        return _synthetic("retrieval.search", "semantic_search", UNIFIED_DB, f"semantic backend unavailable: {exc}")
    try:
        # include_turns=False：只检索 personal_events 结构化事件，不含对话正文
        results = search_semantic(query, top_k=_int_param(params, "limit", 5), include_turns=False)
    except Exception as exc:  # noqa: BLE001
        return _error("retrieval.search", "semantic_search", UNIFIED_DB, "retrieval_unavailable", str(exc)[:160])
    return _success("retrieval.search", "semantic_search", UNIFIED_DB, {"items": results, "count": len(results)})


def _wiki(operation: str, params: Mapping[str, Any], service_operation: str, **service_params: Any) -> dict[str, Any]:
    try:
        result = TopicProjectionService().invoke(service_operation, **service_params)
    except Exception as exc:  # noqa: BLE001
        return _error(operation, "topic_projection", WIKI_PROJECTION_DB, "authority_unavailable", str(exc)[:160])
    if not isinstance(result, Mapping):
        return _error(operation, "topic_projection", WIKI_PROJECTION_DB, "provider_unavailable", "topic 服务返回非 envelope 结果")
    if result.get("ok") is not True:
        error = result.get("error")
        code = error if isinstance(error, str) and error else str((result.get("status") or "authority_unavailable"))
        return _error(operation, "topic_projection", WIKI_PROJECTION_DB, code, "")
    return _success(operation, "topic_projection", WIKI_PROJECTION_DB, result.get("data"), status=str(result.get("status") or "success"))


def _wiki_directory(params: Mapping[str, Any]) -> dict[str, Any]:
    return _wiki("wiki.directory", params, "topic.list", limit=_int_param(params, "limit", _DEFAULT_LIMIT))


def _wiki_page(params: Mapping[str, Any]) -> dict[str, Any]:
    topic_key = _str_param(params, "topic_key")
    topic_type = _str_param(params, "topic_type")
    topic_id = _str_param(params, "record_id") or _str_param(params, "topic_id")
    return _wiki("wiki.page", params, "topic.get", topic_key=topic_key or None, topic_type=topic_type or None, topic_id=topic_id or None)


def _evidence_resolve(params: Mapping[str, Any]) -> dict[str, Any]:
    from personal_knowledge.services.projection.evidence_resolve import build as build_evidence_resolve

    db = UNIFIED_DB
    try:
        result = build_evidence_resolve(db, DecisionIntelligenceReadService(), dict(params))
    except Exception as exc:  # noqa: BLE001
        return _error("evidence.resolve", "evidence_resolve", db, "authority_unavailable", str(exc)[:160])
    if not isinstance(result, Mapping):
        return _error("evidence.resolve", "evidence_resolve", db, "provider_unavailable", "evidence resolve 返回非 envelope 结果")
    if result.get("ok") is not True:
        error = result.get("error") or {}
        return _error("evidence.resolve", "evidence_resolve", db,
                      str(error.get("code") or "invalid_input"), str(error.get("detail") or ""))
    return _success("evidence.resolve", "evidence_resolve", db, result.get("data"), status=str(result.get("status") or "success"))


def _data_quality_report(params: Mapping[str, Any]) -> dict[str, Any]:
    db = UNIFIED_DB
    if not _db_readable(db):
        return _synthetic("data_quality.report", "unified_db", db, "database_missing: personal_system.sqlite 缺失")
    con = _ro(db)
    try:
        events = _quality_counters(con, "unified_events", "event_id")
        categories = _quality_counters(con, "event_categories_v2", "event_id")
        memories = _quality_counters(con, "memory_items", "memory_id")
        relations = _quality_counters(con, "memory_relations", "relation_id")
        duplicates = 0
        if events["exists"]:
            try:
                duplicates = int(con.execute(
                    "SELECT COUNT(1) FROM (SELECT event_id FROM unified_events "
                    "WHERE event_id IS NOT NULL AND event_id!='' "
                    "GROUP BY event_id HAVING COUNT(1) > 1)"
                ).fetchone()[0])
            except sqlite3.Error:
                duplicates = 0
    except sqlite3.Error as exc:
        return _error("data_quality.report", "unified_db", db, "authority_unavailable", str(exc)[:160])
    finally:
        con.close()
    payload = {
        "tables": {"unified_events": events, "event_categories_v2": categories,
                   "memory_items": memories, "memory_relations": relations},
        "duplicate_event_ids": duplicates,
        "generated_at": _utc_now(),
    }
    status = "success" if any(item["exists"] and item["count"] for item in payload["tables"].values()) else "empty"
    return _success("data_quality.report", "unified_db", db, payload, status=status)


def _quality_counters(con: sqlite3.Connection, table: str, id_column: str) -> dict[str, Any]:
    if not _table_exists(con, table):
        return {"exists": False, "count": 0, "missing_key": 0}
    try:
        count = int(con.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0])
        missing = int(con.execute(
            f"SELECT COUNT(1) FROM {table} WHERE {id_column} IS NULL OR {id_column}=''"
        ).fetchone()[0])
    except sqlite3.Error:
        return {"exists": True, "count": 0, "missing_key": 0}
    return {"exists": True, "count": count, "missing_key": missing}


def _data_quality_failed_batches(params: Mapping[str, Any]) -> dict[str, Any]:
    db = UNIFIED_DB
    if not _db_readable(db):
        return _synthetic("data_quality.failed_batches", "unified_db", db, "database_missing: personal_system.sqlite 缺失")
    limit = _int_param(params, "limit", _DEFAULT_LIMIT)
    con = _ro(db)
    try:
        run_failed, run_total = _failed_counts(
            con, "knowledge_run_items",
            "status IN ('terminal_failed','retryable')", "status",
        )
        gate_failed, gate_total = _failed_counts(
            con, "knowledge_extraction_gates",
            "gate_status='failed'", "gate_status",
        )
        failed_ids = con.execute(
            "SELECT id, run_id, status, updated_at FROM knowledge_run_items "
            "WHERE status IN ('terminal_failed','retryable') "
            "ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,),
        ).fetchall() if run_total else []
    except sqlite3.Error as exc:
        return _error("data_quality.failed_batches", "unified_db", db, "authority_unavailable", str(exc)[:160])
    finally:
        con.close()
    external = {"exists": False, "count": 0}
    if _db_readable(EXTERNAL_CONTEXT_DB):
        econ = _ro(EXTERNAL_CONTEXT_DB)
        try:
            if _table_exists(econ, "external_import_runs"):
                rejected = int(econ.execute(
                    "SELECT COUNT(1) FROM external_import_runs WHERE status='rejected'"
                ).fetchone()[0])
                external = {"exists": True, "count": rejected}
        except sqlite3.Error:
            external = {"exists": False, "count": 0}
        finally:
            econ.close()
    payload = {
        "knowledge_run_items_failed": run_failed,
        "knowledge_extraction_gates_failed": gate_failed,
        "external_import_runs_rejected": external["count"],
        "total_failed": run_failed + gate_failed + external["count"],
        "limit": limit,
        "failed_items": [dict(row) for row in failed_ids],
    }
    status = "success" if payload["total_failed"] else "empty"
    return _success("data_quality.failed_batches", "unified_db", db, payload, status=status)


def _failed_counts(con: sqlite3.Connection, table: str, where: str, status_column: str) -> tuple[int, int]:
    """返回 (failed_count, table_exists) ；表缺失按 0 处理，不崩穿。"""
    if not _table_exists(con, table):
        return 0, 0
    try:
        failed = int(con.execute(f"SELECT COUNT(1) FROM {table} WHERE {where}").fetchone()[0])
    except sqlite3.Error:
        failed = 0
    return failed, 1


def _knowledge_status_probe() -> dict[str, Any]:
    out: dict[str, Any] = {
        "available": False,
        "active_collection": None,
        "unit_count": None,
        "chroma_available": False,
        "chroma_up": _port_up(_SYSTEM_PORTS["chroma"]),
        "chroma_port": _SYSTEM_PORTS["chroma"],
    }
    try:
        from personal_knowledge.retrieval.semantic_search import get_knowledge_status

        result = get_knowledge_status(probe_chroma=False)
    except Exception:  # noqa: BLE001 — 探针失败给出降级视图
        return out
    if isinstance(result, Mapping):
        out.update({
            "available": bool(result.get("available")),
            "active_collection": result.get("active_collection"),
            "unit_count": result.get("unit_count") or result.get("db_unit_count"),
        })
    return out


def _system_probe() -> dict[str, Any]:
    return {
        "services": {name: _port_up(port) for name, port in _SYSTEM_PORTS.items()},
        "databases": {
            "personal_system": _db_readable(UNIFIED_DB),
            "external_context": _db_readable(EXTERNAL_CONTEXT_DB),
            "agent_conversations": _db_readable(AGENT_CONVERSATIONS_DB),
            "wiki_projection": _db_readable(WIKI_PROJECTION_DB),
        },
        "knowledge": _knowledge_status_probe(),
    }


def _system_health(params: Mapping[str, Any]) -> dict[str, Any]:
    probe = _system_probe()
    healthy_db = sum(1 for value in probe["databases"].values() if value)
    up_services = sum(1 for value in probe["services"].values() if value)
    if healthy_db and up_services >= 2:
        verdict = "ok"
    elif healthy_db or up_services:
        verdict = "degraded"
    else:
        verdict = "error"
    return _success("system.health", "runtime_probe", None,
                    {"status": verdict, **probe, "probed_at": _utc_now()})


def _system_runtime(params: Mapping[str, Any]) -> dict[str, Any]:
    probe = _system_probe()
    probe["warehouse"] = build_real_warehouse_metadata()
    probe["probed_at"] = _utc_now()
    return _success("system.runtime", "runtime_probe", None, probe)


# === read_handler 主入口 =====================================================

_HANDLERS: dict[str, Any] = {
    "state.current": _state_current,
    "state.changes": _state_changes,
    "decision.list": _decision_list,
    "decision.get": _decision_get,
    "external.list": _external_list,
    "external.get": _external_get,
    "action_outcome.list": _action_outcome_list,
    "knowledge.search": _knowledge_search,
    "knowledge.get": _knowledge_get,
    "retrieval.status": _retrieval_status,
    "retrieval.search": _retrieval_search,
    "wiki.page": _wiki_page,
    "wiki.directory": _wiki_directory,
    "evidence.resolve": _evidence_resolve,
    "data_quality.report": _data_quality_report,
    "data_quality.failed_batches": _data_quality_failed_batches,
    "system.health": _system_health,
    "system.runtime": _system_runtime,
}


def read_handler(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """gateway read 分支的 read_handler 签名：(operation, params) -> data envelope。

    只接收 gateway ``_check`` 校验通过的干净参数。任何情况下都不抛异常：
    - 未知 operation → typed error
    - 真实服务返回失败 → typed error / synthetic 降级
    - 单 authority 读取异常 → 隔离为 error envelope
    """
    handler = _HANDLERS.get(operation)
    if handler is None:
        return _error(operation, "pi_read_dispatch", None, "unsupported_read_operation", operation)
    if not isinstance(params, Mapping):
        params = {}
    try:
        return handler(params)
    except Exception as exc:  # noqa: BLE001 — dispatch 兜底，绝不抛穿
        return _error(operation, "pi_read_dispatch", None, "dispatch_unavailable", str(exc)[:160])


# === warehouse metadata 工厂 =================================================

def _table_count_metadata(
    db: Path,
    table: str,
    *,
    stable_id: str,
    quality_status: str = "ok",
) -> dict[str, Any]:
    """单表计数 metadata；表缺失 / 库缺失返回 {}（不崩溃，不虚构）。"""
    if not db.exists():
        return {}
    try:
        con = _ro(db)
        try:
            if not _table_exists(con, table):
                return {}
            count = int(con.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0])
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return {}
    return {
        "records": count,
        "visible": count,
        "failed": 0,
        "quarantined": 0,
        "stable_id": stable_id,
        "quality_status": quality_status if count else "empty",
        "freshness_status": "fresh" if count else "unknown",
    }


def _where_count(db: Path, table: str, where: str) -> int | None:
    if not db.exists():
        return None
    try:
        con = _ro(db)
        try:
            if not _table_exists(con, table):
                return None
            return int(con.execute(f"SELECT COUNT(1) FROM {table} WHERE {where}").fetchone()[0])
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None


def _knowledge_metadata() -> dict[str, Any]:
    if not UNIFIED_DB.exists():
        return {}
    try:
        con = _ro(UNIFIED_DB)
        try:
            if not _table_exists(con, "canonical_knowledge_units"):
                return {}
            current = int(con.execute(
                "SELECT COUNT(1) FROM canonical_knowledge_units WHERE status='current'"
            ).fetchone()[0])
            rejected = int(con.execute(
                "SELECT COUNT(1) FROM canonical_knowledge_units WHERE status='rejected'"
            ).fetchone()[0])
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return {}
    return {
        "records": current,
        "visible": current,
        "failed": rejected,
        "quarantined": 0,
        "stable_id": "knowledge:canonical",
        "quality_status": "ok" if current else "empty",
        "freshness_status": "fresh" if current else "unknown",
    }


def _retrieval_metadata() -> dict[str, Any]:
    if not UNIFIED_DB.exists():
        return {}
    try:
        con = _ro(UNIFIED_DB)
        try:
            row = con.execute(
                "SELECT version_id, collection_name, unit_count, status, created_at "
                "FROM knowledge_index_versions WHERE status='active' "
                "ORDER BY created_at DESC LIMIT 1",
            ).fetchone()
            if row is None:
                return {}
            active_snapshot = con.execute(
                "SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1"
            ).fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return {}
    unit_count = int(row["unit_count"] or 0)
    return {
        "records": unit_count,
        "visible": unit_count,
        "failed": 0,
        "quarantined": 0,
        "stable_id": f"retrieval:{row['collection_name']}",
        "snapshot_id": str(active_snapshot[0]) if active_snapshot and active_snapshot[0] else None,
        "quality_status": "ok" if unit_count else "empty",
        "freshness_status": "fresh" if unit_count else "unknown",
    }


def _external_metadata() -> dict[str, Any]:
    base = _table_count_metadata(
        EXTERNAL_CONTEXT_DB, "external_facts", stable_id="external:facts",
    )
    if not base:
        return {}
    rejected = _where_count(EXTERNAL_CONTEXT_DB, "external_import_runs", "status='rejected'")
    if rejected:
        base["failed"] = rejected
    return base


def _system_metadata() -> dict[str, Any]:
    probe = _system_probe()
    healthy = sum(1 for value in probe["services"].values() if value) + sum(
        1 for value in probe["databases"].values() if value
    )
    total = len(probe["services"]) + len(probe["databases"])
    return {
        "records": healthy,
        "visible": healthy,
        "failed": max(0, total - healthy),
        "quarantined": 0,
        "stable_id": "system:runtime",
        "quality_status": "ok" if healthy else "empty",
        "freshness_status": "fresh" if healthy else "unknown",
    }


def build_real_warehouse_metadata() -> dict[str, dict[str, Any]]:
    """从真实库统计构造 WarehouseTools per-authority metadata。

    表缺失 / 库缺失的 authority 返回空 metadata（记录数 0），不崩溃。
    """
    metadata: dict[str, dict[str, Any]] = {}

    conversation = _table_count_metadata(
        AGENT_CONVERSATIONS_DB, "canonical_messages", stable_id="conversation:canonical",
    )
    if conversation:
        metadata["conversation"] = conversation

    knowledge = _knowledge_metadata()
    if knowledge:
        metadata["knowledge"] = knowledge

    retrieval = _retrieval_metadata()
    if retrieval:
        metadata["retrieval"] = retrieval

    external = _external_metadata()
    if external:
        metadata["external"] = external

    decision = _table_count_metadata(
        VAR_DB / "decision_analysis.sqlite", "analysis_runs", stable_id="decision:analysis",
    )
    if decision:
        metadata["decision"] = decision

    system = _system_metadata()
    if system:
        metadata["system"] = system

    return metadata


class _AllAuthoritiesWarehouseTools(WarehouseTools):
    """warehouse.* 缺 authority_id 时默认遍历全部 authority 的 WarehouseTools。

    逐 authority 调用既有预检+metadata 门面；所有 authority 均失败时抛出首个
    WarehouseToolError（保留原始错误 code，不伪装成 authority_unknown）。
    """

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        bound = dict(params or {})
        if bound.get("authority_id"):
            return super().invoke(operation, bound)
        merged: list[dict[str, Any]] = []
        first_error: WarehouseToolError | None = None
        for authority_id in AUTHORITY_ADAPTERS:
            try:
                merged.append(super().invoke(operation, {**bound, "authority_id": authority_id}))
            except WarehouseToolError as exc:
                first_error = first_error or exc
        if not merged:
            raise first_error or WarehouseToolError("authority_unknown")
        return {
            "schema_version": WAREHOUSE_SCHEMA_VERSION,
            "operation": operation,
            "ok": True,
            "status": "success",
            "authority_id": "all",
            "count": len(merged),
            "counts": {
                "records": sum(int(item.get("counts", {}).get("records", 0)) for item in merged),
                "failed": sum(int(item.get("counts", {}).get("failed", 0)) for item in merged),
            },
            "authorities": merged,
            "limitations": ["metadata_only", "aggregate_all_authorities"],
        }


# === evidence.sqlite_query 错误描述增强（保持 lease 契约严格） =================
#
# evidence.sqlite_query 是 lease 契约操作：skill_id / manifest_checksum /
# privacy_ceiling / database_id / query_id / version / parameters.session_id 缺一
# 即 fail-closed。gateway 的 evidence 分支在适配器抛 EvidenceSqliteError 时只回
# typed code（不带 detail），模型无法知道具体缺什么。本包装不改任何校验逻辑
# （全部委托父类，严格性不变），只在失败时把 code 转成带 detail 的 error
# envelope，明确说明缺哪个参数（例如 parameters.session_id）。

_EVIDENCE_ERROR_HINTS: dict[str, str] = {
    "database_unknown": "database_id 缺失或未注册（需要 canonical_conversation_v1）",
    "unknown_query": "query_id 不在白名单（需要 conversation.evidence_messages.v1）",
    "version_mismatch": "version 与证据查询描述符不匹配（需要 1.0.0）",
    "lease_invalid": "skill_id 缺失/不匹配（lease 契约：必须传绑定的 skill id）",
    "supporting_skill_rejected": "supporting_skills 必须为空",
    "manifest_drift": "manifest_checksum 与绑定的 skill 指令校验和不一致",
    "privacy_ceiling_mismatch": "privacy_ceiling 必须是 R1",
    "binding_required": "binding 缺失",
    "descriptor_invalid": "descriptor 结构非法（需要 database_id/query_id/version/parameters/scope 等）",
    "undeclared_input": "descriptor 含未声明字段",
    "scope_denied": "scope 缺失/越界（必须声明 session_id 且与 parameters 一致）",
    "parameter_invalid": "parameters 缺 session_id 或格式非法（session_id 必填）",
    "path_forbidden": "session_id/after 含非法路径片段",
    "sql_forbidden": "session_id/after 含 SQL 片段",
    "limit_exceeded": "limit 超出允许上限",
    "database_unavailable": "证据库不可用或缺失（agent_conversations.sqlite 只读打开失败）",
    "domain_unavailable": "证据库读取失败",
    "query_timeout": "证据查询超时",
}


class DescriptiveEvidenceTool(EvidenceSqliteTool):
    """EvidenceSqliteTool 的错误描述增强包装；成功路径原样透传，校验严格不变。"""

    def invoke(self, descriptor: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return super().invoke(descriptor)
        except EvidenceSqliteError as exc:  # 校验失败 → 带 detail 的 error envelope
            code = str(getattr(exc, "code", "") or "descriptor_invalid")
            return {
                "schema_version": EVIDENCE_SQLITE_SCHEMA,
                "operation": EVIDENCE_SQLITE_OPERATION,
                "ok": False,
                "status": "error",
                "execution": "not_run",
                "error": {
                    "code": code,
                    "detail": _EVIDENCE_ERROR_HINTS.get(code, "证据库只读查询参数校验失败"),
                },
                "limitations": ["lease 契约严格：缺参即拒绝，不降级"],
            }


def make_descriptive_evidence_tool() -> EvidenceSqliteTool:
    """生产用 evidence.sqlite_query 工具（错误描述增强、校验不变）。"""
    return DescriptiveEvidenceTool()


def make_real_warehouse_tools() -> WarehouseTools:
    """生产 WarehouseTools：真实 metadata + 缺 authority_id 时遍历全部 authority。"""
    return _AllAuthoritiesWarehouseTools(metadata=build_real_warehouse_metadata())


__all__ = [
    "DISPATCH_SCHEMA",
    "READ_DISPATCH_OPERATIONS",
    "build_real_warehouse_metadata",
    "make_descriptive_evidence_tool",
    "make_real_warehouse_tools",
    "read_handler",
]
