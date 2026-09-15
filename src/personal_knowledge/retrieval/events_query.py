"""Auto-split from unified_search.py — see facade for the public API."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import csv
import io
import time
from pathlib import Path
from typing import Any, Optional

import sys  # noqa: E402
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR

# search_vectors is used by semantic_search only, but harmless elsewhere.
from personal_knowledge.retrieval.search_vectors import search as _semantic_search, search_all as _semantic_search_all  # noqa: E402
import personal_knowledge.retrieval._constants as _C  # noqa: E402
from personal_knowledge.retrieval._constants import (  # noqa: E402
    DEFAULT_MEMORY_GRAPH_LIMIT, MAX_MEMORY_GRAPH_LIMIT,
    DEFAULT_RELATION_REVIEW_LIMIT, MAX_RELATION_REVIEW_LIMIT,
    RELATION_REVIEW_STATUSES, DEFAULT_DATA_LIMIT, MAX_DATA_LIMIT, MAX_EXPORT_LIMIT,
    DEFAULT_EVENT_FIELDS, EVENT_FIELD_SQL, AGGREGATE_GROUP_SQL,
    _KU_SLOTS, _RAW_SLOTS_DEFAULT, _KU_PORT,
    FALLBACK_POLICIES, DEFAULT_FALLBACK_POLICY,
    CONVERSATION_TURNS_COLLECTION, CANONICAL_MESSAGES_COLLECTION,
    _NON_DIALOGUE_PREFERRED_SOURCE,
)

from personal_knowledge.retrieval._db_utils import (  # noqa: E402
    _bounded_int, _split_csv, _normalize_event_fields, _event_from_clause, _event_filter_sql, _memory_layer_ready, _table_exists, _parse_metadata, _parse_json_list, _memory_row_to_dict,
)
from personal_knowledge.retrieval.merge_cluster import _merge_layer_ready, _dedup_event_ids  # noqa: E402

def query_events(
    source: Optional[str] = None,
    month: Optional[str] = None,
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 50,
    dedup: bool = False,
) -> list[dict]:
    """精确查询:按结构化条件过滤原始 sqlite 库。

    所有参数都是可选的 AND 过滤:
    source:   "Google"/"GPT"/"Agent"
    month:    "2025-03" 或 "2025"(前缀匹配)
    category: category_v2 子串匹配(如"编程")
    keyword:  title + content_rich + content 的子串匹配
    limit:    最多返回条数(默认 50,上限 200)
    dedup:    True=按合并层折叠(L1/L2 同簇只留代表,代表保留首次命中),
              结果含 merged_count 字段。折叠后条数可能少于 limit。
    返回: list[dict],含 event_id/source/event_time/service/category_v2/title/content_rich
    """
    limit = max(1, min(int(limit), 200))
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    # dedup 模式:多拉再折叠。内层 fetch_limit 放大,折叠后裁到 limit
    fetch_limit = limit * 4 if dedup else limit
    sql = (
        "SELECT ue.event_id, ue.source, ue.service, ue.event_time, ue.month, "
        "ue.title, (r.content_rich IS NOT NULL) AS has_rich, "
        "COALESCE(r.content_rich, ue.content) AS content_rich, "
        "c.category_v2 "
        "FROM unified_events ue "
        "LEFT JOIN unified_events_rich r ON r.event_id = ue.event_id "
        "LEFT JOIN event_categories_v2 c ON c.event_id = ue.event_id "
        "WHERE 1=1"
    )
    params: list = []
    if source:
        sql += " AND ue.source = ?"
        params.append(source)
    if month:
        sql += " AND substr(ue.month, 1, ?) = ?"
        params.extend([len(month), month])
    if category:
        sql += " AND c.category_v2 LIKE ?"
        params.append(f"%{category}%")
    if keyword:
        sql += " AND (ue.title LIKE ? OR COALESCE(r.content_rich, ue.content) LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw])
    sql += " ORDER BY ue.event_time DESC LIMIT ?"
    params.append(fetch_limit)
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()

    if not dedup or not rows or not _merge_layer_ready():
        return rows

    kept_ids, dup_map = _dedup_event_ids([r["event_id"] for r in rows])
    # 保留首次出现的代表行,附 merged_count(该代表所属簇的总成员数)
    con = sqlite3.connect(_C.UNIFIED_DB)
    seen_rep: set[str] = set()
    out: list[dict] = []
    for r in rows:
        rep = dup_map.get(r["event_id"], r["event_id"])
        if rep in seen_rep:
            continue
        seen_rep.add(rep)
        r2 = dict(r)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM merge_members WHERE cluster_id IN "
                "(SELECT cluster_id FROM merge_members WHERE event_id=?)",
                (rep,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        r2["merged_count"] = n if n > 0 else 1  # 独立事件代表自身,计 1
        out.append(r2)
        if len(out) >= limit:
            break
    con.close()
    return out


def get_event_detail(event_id: str) -> Optional[dict]:
    """按 event_id 取单条事件全字段(含增强内容)。给"点开看详情"用。"""
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT ue.*, r.content_rich, c.category_v2 "
        "FROM unified_events ue "
        "LEFT JOIN unified_events_rich r ON r.event_id = ue.event_id "
        "LEFT JOIN event_categories_v2 c ON c.event_id = ue.event_id "
        "WHERE ue.event_id = ?",
        (event_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def stats() -> dict:
    """返回数据库+向量库+知识索引的统计概览(给 AI 快速建立认知用)。"""
    out: dict = {
        "total_events": 0,
        "by_source": {},
        "active_months": 0,
    }
    if _C.UNIFIED_DB.exists():
        con = sqlite3.connect(_C.UNIFIED_DB)
        con.row_factory = sqlite3.Row
        out["total_events"] = con.execute("SELECT COUNT(*) FROM unified_events").fetchone()[0]
        out["by_source"] = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT source, COUNT(*) FROM unified_events GROUP BY source ORDER BY 2 DESC"
            )
        }
        out["active_months"] = con.execute(
            "SELECT COUNT(DISTINCT substr(month,1,7)) FROM unified_events WHERE length(month)>=7"
        ).fetchone()[0]
        con.close()
    # 向量库统计(失败不影响主流程)
    try:
        from personal_knowledge.core.chroma_client import ChromaClient
        client = ChromaClient()
        coll = client.get_or_create_collection("personal_events")
        out["vector_count"] = coll.count()
        out["vector_available"] = True
        # Wave 7: conversation_turns 独立 collection 统计
        try:
            turns_coll = client.get_or_create_collection("conversation_turns")
            out["conversation_turns_count"] = turns_coll.count()
            out["conversation_turns_available"] = True
        except Exception:
            out["conversation_turns_available"] = False
    except Exception as e:
        out["vector_available"] = False
        out["vector_error"] = str(e)[:120]
    # Phase 14: knowledge index（CLI/REST/MCP 语义检索共用）
    from personal_knowledge.retrieval.semantic_search import get_knowledge_status  # noqa: E402
    out["knowledge"] = get_knowledge_status(probe_chroma=True)
    return out


def list_categories(source: Optional[str] = None) -> list[dict]:
    """返回 category_v2 分布，可按 source 过滤。"""
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    sql = (
        "SELECT c.category_v2, COUNT(*) AS n "
        "FROM event_categories_v2 c "
        "JOIN unified_events ue ON ue.event_id = c.event_id "
        "WHERE c.category_v2 IS NOT NULL AND c.category_v2 != ''"
    )
    params: list = []
    if source:
        sql += " AND ue.source = ?"
        params.append(source)
    sql += " GROUP BY c.category_v2 ORDER BY n DESC"
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return rows


# === 数据访问 contract(list/export/aggregate/timeline)====================

def list_events_contract(
    source: Optional[str] = None,
    service: Optional[str] = None,
    category: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    keyword: Optional[str] = None,
    fields: str | list[str] | None = None,
    limit: int = DEFAULT_DATA_LIMIT,
    offset: int = 0,
    order: str = "desc",
) -> dict:
    """List unified events with bounded pagination and explicit field selection."""
    limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
    offset = _bounded_int(offset, 0, 0, 10**9)
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    selected_fields = _normalize_event_fields(fields)
    select_sql = ", ".join(
        f"{EVENT_FIELD_SQL[field]} AS {field}" for field in selected_fields
    )
    where_sql, params, filters = _event_filter_sql(
        source=source,
        service=service,
        category=category,
        time_from=time_from,
        time_to=time_to,
        keyword=keyword,
    )

    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        total = con.execute(
            "SELECT COUNT(DISTINCT ue.event_id) " + _event_from_clause() + where_sql,
            params,
        ).fetchone()[0]
        rows = [
            dict(row)
            for row in con.execute(
                "SELECT " + select_sql + " " + _event_from_clause() + where_sql
                + f" ORDER BY ue.event_time {order_sql}, ue.event_id {order_sql} LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
        ]
    finally:
        con.close()

    return {
        "ok": True,
        "count": len(rows),
        "total": total,
        "limit": limit,
        "offset": offset,
        "fields": selected_fields,
        "filters": filters,
        "items": rows,
        "truncated": offset + len(rows) < total,
    }


# v2 兼容投影 id 前缀 → canonical 表与主键列。ku_facts/会话卡的证据 ref
# （v2|cm|<sha>）指向投影行而非 personal_events；不认前缀时证据链下钻
# （data_get_event_by_id）恒 found=false。
_PROJECTION_REF_TABLES = {
    "v2|cm|": ("canonical_messages", "canonical_message_id"),
    "v2|cs|": ("canonical_sessions", "canonical_session_id"),
    "v2|cte|": ("canonical_tool_events", "canonical_tool_id"),
}


def _get_projection_ref(event_id: str) -> dict | None:
    """按 v2 投影前缀直查 canonical 三表（只读）；非投影前缀返回 None。"""
    target = next(
        ((t, c) for p, (t, c) in _PROJECTION_REF_TABLES.items() if event_id.startswith(p)),
        None,
    )
    if target is None:
        return None
    table, col = target
    db = _C.AGENT_CONVERSATIONS_DB
    if not db.exists():
        return {"ok": False, "found": False, "event_id": event_id, "fields": [],
                "item": None, "kind": table, "note": "canonical db not found"}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(f"SELECT * FROM {table} WHERE {col} = ?", (event_id,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    item = dict(row) if row else None
    return {
        "ok": row is not None,
        "found": row is not None,
        "event_id": event_id,
        "fields": list(item) if item else [],
        "item": item,
        "kind": table,
    }


def get_event_by_id_contract(event_id: str, fields: str | list[str] | None = None) -> dict:
    """Return one event by id using the same field policy as list_events_contract."""
    projection = _get_projection_ref(event_id)
    if projection is not None:
        return projection
    selected_fields = _normalize_event_fields(fields)
    select_sql = ", ".join(
        f"{EVENT_FIELD_SQL[field]} AS {field}" for field in selected_fields
    )
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT " + select_sql + " " + _event_from_clause() + "WHERE ue.event_id = ?",
            (event_id,),
        ).fetchone()
    finally:
        con.close()
    return {
        "ok": row is not None,
        "found": row is not None,
        "event_id": event_id,
        "fields": selected_fields,
        "item": dict(row) if row else None,
    }


def export_events_contract(
    export_format: str = "jsonl",
    source: Optional[str] = None,
    service: Optional[str] = None,
    category: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    keyword: Optional[str] = None,
    fields: str | list[str] | None = None,
    limit: int = MAX_EXPORT_LIMIT,
    offset: int = 0,
    order: str = "desc",
) -> dict:
    """Export a bounded event query as json/jsonl/csv inside a contract object."""
    export_format = (export_format or "jsonl").strip().lower()
    if export_format not in {"json", "jsonl", "csv"}:
        raise ValueError("export format must be one of: json, jsonl, csv")
    limit = _bounded_int(limit, MAX_EXPORT_LIMIT, 1, MAX_EXPORT_LIMIT)
    data = list_events_contract(
        source=source,
        service=service,
        category=category,
        time_from=time_from,
        time_to=time_to,
        keyword=keyword,
        fields=fields,
        limit=min(limit, MAX_DATA_LIMIT),
        offset=offset,
        order=order,
    )
    # list_events_contract intentionally caps at MAX_DATA_LIMIT; export has a
    # larger hard cap, so rerun the same query when the caller asked for more.
    if limit > MAX_DATA_LIMIT:
        selected_fields = _normalize_event_fields(fields)
        select_sql = ", ".join(
            f"{EVENT_FIELD_SQL[field]} AS {field}" for field in selected_fields
        )
        where_sql, params, _ = _event_filter_sql(
            source=source,
            service=service,
            category=category,
            time_from=time_from,
            time_to=time_to,
            keyword=keyword,
        )
        order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        offset_i = _bounded_int(offset, 0, 0, 10**9)
        con = sqlite3.connect(_C.UNIFIED_DB)
        con.row_factory = sqlite3.Row
        try:
            rows = [
                dict(row)
                for row in con.execute(
                    "SELECT " + select_sql + " " + _event_from_clause() + where_sql
                    + f" ORDER BY ue.event_time {order_sql}, ue.event_id {order_sql} LIMIT ? OFFSET ?",
                    params + [limit, offset_i],
                )
            ]
        finally:
            con.close()
        data["items"] = rows
        data["count"] = len(rows)
        data["limit"] = limit
        data["truncated"] = offset_i + len(rows) < data["total"]

    rows = data["items"]
    if export_format == "json":
        content: str | list[dict] = rows
    elif export_format == "jsonl":
        content = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=data["fields"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        content = buf.getvalue()

    return {
        "ok": True,
        "format": export_format,
        "count": len(rows),
        "total": data["total"],
        "limit": data["limit"],
        "offset": data["offset"],
        "fields": data["fields"],
        "filters": data["filters"],
        "content": content,
        "truncated": data["truncated"],
        "hard_cap": MAX_EXPORT_LIMIT,
    }


def export_all_contract(**kwargs) -> dict:
    """Compatibility wrapper for callers that want an explicit export-all name."""
    return export_events_contract(**kwargs)


def export_query_contract(**kwargs) -> dict:
    """Compatibility wrapper for callers that want an explicit filtered export name."""
    return export_events_contract(**kwargs)


def aggregate_contract(
    group_by: str = "source",
    source: Optional[str] = None,
    service: Optional[str] = None,
    category: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = DEFAULT_DATA_LIMIT,
) -> dict:
    """Aggregate events by one or more supported dimensions."""
    groups = _split_csv(group_by) or ["source"]
    if groups == ["memory_type"]:
        limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
        con = sqlite3.connect(_C.UNIFIED_DB)
        con.row_factory = sqlite3.Row
        try:
            rows = [
                dict(row)
                for row in con.execute(
                    "SELECT memory_type, COUNT(1) AS count FROM memory_items "
                    "GROUP BY memory_type ORDER BY count DESC LIMIT ?",
                    (limit,),
                )
            ]
        finally:
            con.close()
        return {
            "ok": True,
            "group_by": groups,
            "count": len(rows),
            "limit": limit,
            "filters": {},
            "items": rows,
        }
    if groups == ["relation_type"]:
        limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
        con = sqlite3.connect(_C.UNIFIED_DB)
        con.row_factory = sqlite3.Row
        try:
            rule_rows = [
                dict(row)
                for row in con.execute(
                    "SELECT relation AS relation_type, COUNT(1) AS count, 'rule' AS edge_source "
                    "FROM memory_relations GROUP BY relation ORDER BY count DESC LIMIT ?",
                    (limit,),
                )
            ]
            llm_rows = []
            if _table_exists(con, "memory_relation_judgments"):
                llm_rows = [
                    dict(row)
                    for row in con.execute(
                        "SELECT relation_type, COUNT(1) AS count, 'llm_judgment' AS edge_source "
                        "FROM memory_relation_judgments GROUP BY relation_type ORDER BY count DESC LIMIT ?",
                        (limit,),
                    )
                ]
            rows = (rule_rows + llm_rows)[:limit]
        finally:
            con.close()
        return {
            "ok": True,
            "group_by": groups,
            "count": len(rows),
            "limit": limit,
            "filters": {},
            "items": rows,
        }
    unknown = [name for name in groups if name not in AGGREGATE_GROUP_SQL]
    if unknown:
        raise ValueError(f"unknown group_by: {', '.join(unknown)}")
    limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
    select_parts = [
        f"{AGGREGATE_GROUP_SQL[name]} AS {name}" for name in groups
    ]
    group_exprs = [AGGREGATE_GROUP_SQL[name] for name in groups]
    where_sql, params, filters = _event_filter_sql(
        source=source,
        service=service,
        category=category,
        time_from=time_from,
        time_to=time_to,
        keyword=keyword,
    )
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in con.execute(
                "SELECT " + ", ".join(select_parts) + ", COUNT(DISTINCT ue.event_id) AS count "
                + _event_from_clause()
                + where_sql
                + " GROUP BY " + ", ".join(group_exprs)
                + " ORDER BY count DESC LIMIT ?",
                params + [limit],
            )
        ]
    finally:
        con.close()
    return {
        "ok": True,
        "group_by": groups,
        "count": len(rows),
        "limit": limit,
        "filters": filters,
        "items": rows,
    }


def timeline_contract(
    interval: str = "month",
    source: Optional[str] = None,
    service: Optional[str] = None,
    category: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = DEFAULT_DATA_LIMIT,
) -> dict:
    """Return event counts over time. interval: day/month/year."""
    interval = (interval or "month").strip().lower()
    if interval not in {"day", "month", "year"}:
        raise ValueError("interval must be one of: day, month, year")
    bucket_sql = {
        "day": "substr(ue.event_time, 1, 10)",
        "month": "substr(ue.event_time, 1, 7)",
        "year": "substr(ue.event_time, 1, 4)",
    }[interval]
    limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
    where_sql, params, filters = _event_filter_sql(
        source=source,
        service=service,
        category=category,
        time_from=time_from,
        time_to=time_to,
        keyword=keyword,
    )
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in con.execute(
                f"SELECT {bucket_sql} AS bucket, COUNT(DISTINCT ue.event_id) AS count "
                + _event_from_clause()
                + where_sql
                + " GROUP BY bucket ORDER BY bucket ASC LIMIT ?",
                params + [limit],
            )
        ]
    finally:
        con.close()
    return {
        "ok": True,
        "interval": interval,
        "count": len(rows),
        "limit": limit,
        "filters": filters,
        "items": rows,
    }


# === 记忆层(长期记忆对象 + 图谱关系)=====================================

