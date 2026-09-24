"""MCP data/检索域 handler：进程内直连 unified_search backend。

从 services/mcp_server.py 拆出的全部 data / retrieval 工具调用逻辑。
与 REST/CLI 共用同一 backend（personal_knowledge.retrieval.unified_search）。
search_semantic 与其它工具一致，进程内直连 backend.search_knowledge_units
（不再经 HTTP 回环调用常驻 REST API）。
"""

from __future__ import annotations

import json

import personal_knowledge.retrieval.semantic_cards as semantic_cards  # noqa: E402
import personal_knowledge.retrieval.unified_search as backend  # noqa: E402

from personal_knowledge.mcp_tools.handlers._format import (  # noqa: E402
    _event_contract_args,
    _format_categories,
    _format_detail,
    _format_knowledge_status,
    _format_memory_detail,
    _format_memory_profile,
    _format_query,
    _format_semantic,
    _format_semantic_cards,
    _format_stats,
    _json_contract,
)

# 本域负责的全部工具名（含历史 call 兼容别名 data_export_all/data_export_query）
TOOL_NAMES = frozenset({
    "search_semantic",
    "search_semantic_cards",
    "query_events",
    "get_event_detail",
    "stats",
    "knowledge_status",
    "list_google_assertions",
    "get_google_assertion",
    "list_categories",
    "get_memory_profile",
    "get_memory_by_subject",
    "data_list_events",
    "data_list_memories",
    "data_list_relations",
    "data_aggregate",
    "data_timeline",
    "data_export",
    "data_export_all",
    "data_export_query",
    "data_get_event_by_id",
    "data_get_memory_by_id",
    "data_quality_report",
})


def render(name: str, arguments: dict) -> str:
    """执行单个 data/检索工具并返回 AI 友好文本。未知名返回"未知工具"。"""
    if name == "search_semantic":
        # 进程内直连（与 REST /search/semantic 同款调用），替换原 HTTP 回环。
        results = backend.search_knowledge_units(
            query=arguments.get("query", ""),
            top_k=int(arguments.get("top_k", 5) or 5),
            source=arguments.get("source"),
        )
        return _format_semantic(results)

    if name == "search_semantic_cards":
        # MVP 会话卡检索：进程内直连 semantic_cards（var/db/semantic_mvp_v3.sqlite 只读）
        rows = semantic_cards.search_cards(
            query=arguments.get("query", ""),
            limit=int(arguments.get("top_k", 8) or 8),
        )
        return _format_semantic_cards(rows)

    if name == "query_events":
        results = backend.query_events(
            source=arguments.get("source"),
            month=arguments.get("month"),
            category=arguments.get("category"),
            keyword=arguments.get("keyword"),
            limit=arguments.get("limit", 50),
        )
        return _format_query(results)

    if name == "get_event_detail":
        data = backend.get_event_detail(arguments.get("event_id", ""))
        if data is None:
            return f"未找到 event_id={arguments.get('event_id', '')}"
        return _format_detail(data)

    if name == "stats":
        return _format_stats(backend.stats())

    if name == "knowledge_status":
        probe = arguments.get("probe_chroma", True)
        if isinstance(probe, str):
            probe = probe.strip().lower() not in {"0", "false", "no", "off"}
        data = backend.get_knowledge_status(probe_chroma=bool(probe))
        return _format_knowledge_status(data)

    if name == "list_google_assertions":
        data = backend.list_google_light_assertions(
            assertion_type=arguments.get("assertion_type"),
            limit=int(arguments.get("limit", 50) or 50),
            offset=int(arguments.get("offset", 0) or 0),
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    if name == "get_google_assertion":
        aid = arguments.get("assertion_id", "")
        item = backend.get_google_light_assertion(str(aid))
        if item is None:
            return f"未找到 assertion_id={aid}"
        return json.dumps(item, ensure_ascii=False, indent=2)

    if name == "list_categories":
        return _format_categories(backend.list_categories(arguments.get("source")))

    if name == "get_memory_profile":
        data = backend.get_memory_profile(
            memory_type=arguments.get("memory_type"),
            limit=arguments.get("limit", 50),
        )
        return _format_memory_profile(data)

    if name == "get_memory_by_subject":
        subject = arguments.get("subject", "")
        data = backend.get_memory_by_subject(subject)
        if data is None:
            return f"未找到 memory subject={subject}"
        neighbors = int(arguments.get("neighbors", 0) or 0)
        if neighbors:
            data["neighbors"] = backend.get_memory_neighbors(subject, neighbors)
        return _format_memory_detail(data)

    if name == "data_list_events":
        data = backend.list_events_contract(
            **_event_contract_args(arguments),
            fields=arguments.get("fields"),
            limit=arguments.get("limit", backend.DEFAULT_DATA_LIMIT),
            offset=arguments.get("offset", 0),
            order=arguments.get("order", "desc"),
        )
        return _json_contract(data)

    if name == "data_list_memories":
        data = backend.list_memories_contract(
            memory_type=arguments.get("memory_type"),
            memory_subtype=arguments.get("memory_subtype"),
            subject=arguments.get("subject") or arguments.get("subject_like"),
            limit=arguments.get("limit", backend.DEFAULT_DATA_LIMIT),
            offset=arguments.get("offset", 0),
        )
        return _json_contract(data)

    if name == "data_list_relations":
        data = backend.list_relations_contract(
            relation=arguments.get("relation") or arguments.get("relation_type"),
            from_memory_id=arguments.get("from_memory_id"),
            to_memory_id=arguments.get("to_memory_id"),
            subject=arguments.get("subject"),
            status=arguments.get("status"),
            limit=arguments.get("limit", backend.DEFAULT_DATA_LIMIT),
            offset=arguments.get("offset", 0),
        )
        return _json_contract(data)

    if name == "data_aggregate":
        group_by_fields = arguments.get("group_by_fields") or []
        group_by = ",".join(group_by_fields) if isinstance(group_by_fields, list) and group_by_fields else arguments.get("group_by", "source")
        data = backend.aggregate_contract(
            group_by=group_by,
            **_event_contract_args(arguments),
            limit=arguments.get("limit", backend.DEFAULT_DATA_LIMIT),
        )
        return _json_contract(data)

    if name == "data_timeline":
        event_args = _event_contract_args(arguments)
        subject = arguments.get("subject")
        if subject and not event_args.get("keyword"):
            event_args["keyword"] = subject
        data = backend.timeline_contract(
            interval=arguments.get("interval") or arguments.get("bucket") or "month",
            **event_args,
            limit=arguments.get("limit", backend.DEFAULT_DATA_LIMIT),
        )
        return _json_contract(data)

    if name in {"data_export", "data_export_all", "data_export_query"}:
        # 合并导出入口；保留旧名作为 call 兼容别名
        event_args = _event_contract_args(arguments)
        if arguments.get("query") and not event_args.get("keyword"):
            event_args["keyword"] = arguments.get("query")
        if event_args.get("keyword") or arguments.get("query"):
            data = backend.export_query_contract(
                export_format=arguments.get("format", "jsonl"),
                **event_args,
                fields=arguments.get("fields"),
                limit=arguments.get("limit", backend.MAX_EXPORT_LIMIT),
                offset=arguments.get("offset", 0),
                order=arguments.get("order", "desc"),
            )
        else:
            data = backend.export_all_contract(
                export_format=arguments.get("format", "jsonl"),
                **event_args,
                fields=arguments.get("fields"),
                limit=arguments.get("limit", backend.MAX_EXPORT_LIMIT),
                offset=arguments.get("offset", 0),
                order=arguments.get("order", "desc"),
            )
        return _json_contract(data)

    if name == "data_get_event_by_id":
        data = backend.get_event_by_id_contract(
            arguments.get("event_id", ""),
            fields=arguments.get("fields"),
        )
        return _json_contract(data)

    if name == "data_get_memory_by_id":
        inc = arguments.get("include_evidence", True)
        if isinstance(inc, str):
            inc = inc.strip().lower() not in {"0", "false", "no", "off"}
        data = backend.get_memory_by_id_contract(
            arguments.get("memory_id", ""),
            include_evidence=bool(inc),
        )
        return _json_contract(data)

    if name == "data_quality_report":
        return _json_contract(backend.data_quality_report_contract())

    return f"未知工具: {name}"
