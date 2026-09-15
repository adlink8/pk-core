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
# Canonical graph query module (domains.graph.query_graph facade is slated for removal 2026-08-13).
from personal_knowledge.application.graph.query_graph import (  # noqa: E402
    find_node_by_subject, load_graph, table_exists,
)

def _find_memory_ids_by_subject(con: sqlite3.Connection, subject: str) -> list[str]:
    """按 subject 精确优先、再模糊匹配记忆对象。"""
    subject = (subject or "").strip()
    if not subject:
        return []
    rows = con.execute(
        "SELECT memory_id FROM memory_items WHERE lower(subject)=lower(?) "
        "ORDER BY evidence_count DESC, confidence DESC",
        (subject,),
    ).fetchall()
    if rows:
        return [r[0] for r in rows]
    rows = con.execute(
        "SELECT memory_id FROM memory_items WHERE lower(subject) LIKE lower(?) "
        "ORDER BY evidence_count DESC, confidence DESC LIMIT 20",
        (f"%{subject}%",),
    ).fetchall()
    return [r[0] for r in rows]


def _get_memory_by_id(con: sqlite3.Connection, memory_id: str) -> Optional[dict]:
    row = con.execute(
        "SELECT memory_id, memory_type, memory_subtype, subject, description, "
        "confidence, evidence_count, metadata, created_at "
        "FROM memory_items WHERE memory_id=?",
        (memory_id,),
    ).fetchone()
    return _memory_row_to_dict(row) if row else None


def list_memories_contract(
    memory_type: Optional[str] = None,
    memory_subtype: Optional[str] = None,
    subject: Optional[str] = None,
    limit: int = DEFAULT_DATA_LIMIT,
    offset: int = 0,
) -> dict:
    """List memory_items with bounded pagination."""
    limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
    offset = _bounded_int(offset, 0, 0, 10**9)
    if not _memory_layer_ready():
        return {
            "ok": True,
            "available": False,
            "count": 0,
            "total": 0,
            "limit": limit,
            "offset": offset,
            "filters": {
                "memory_type": memory_type,
                "memory_subtype": memory_subtype,
                "subject": subject,
            },
            "items": [],
            "truncated": False,
        }
    where = ["1=1"]
    params: list[Any] = []
    if memory_type:
        where.append("memory_type = ?")
        params.append(memory_type)
    if memory_subtype:
        where.append("memory_subtype = ?")
        params.append(memory_subtype)
    if subject:
        where.append("subject LIKE ?")
        params.append(f"%{subject}%")
    where_sql = " WHERE " + " AND ".join(where)
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        total = con.execute(
            "SELECT COUNT(1) FROM memory_items" + where_sql,
            params,
        ).fetchone()[0]
        rows = [
            _memory_row_to_dict(row)
            for row in con.execute(
                "SELECT memory_id, memory_type, memory_subtype, subject, description, "
                "confidence, evidence_count, metadata, created_at "
                "FROM memory_items"
                + where_sql
                + " ORDER BY evidence_count DESC, confidence DESC, subject LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
        ]
    finally:
        con.close()
    return {
        "ok": True,
        "available": True,
        "count": len(rows),
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {
            "memory_type": memory_type,
            "memory_subtype": memory_subtype,
            "subject": subject,
        },
        "items": rows,
        "truncated": offset + len(rows) < total,
    }


def get_memory_by_id_contract(
    memory_id: str,
    *,
    include_evidence: bool = True,
) -> dict:
    """Return one memory item by memory_id.

    include_evidence=False skips memory_links (returns empty evidence list).
    """
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        item = _get_memory_by_id(con, memory_id)
        evidence: list[dict] = []
        if item and include_evidence:
            evidence = [
                dict(row)
                for row in con.execute(
                    "SELECT target_type, target_id, relation FROM memory_links "
                    "WHERE memory_id=? ORDER BY id LIMIT 20",
                    (memory_id,),
                )
            ]
    finally:
        con.close()
    return {
        "ok": item is not None,
        "found": item is not None,
        "memory_id": memory_id,
        "include_evidence": bool(include_evidence),
        "item": item,
        "evidence": evidence,
    }


def list_relations_contract(
    relation: Optional[str] = None,
    from_memory_id: Optional[str] = None,
    to_memory_id: Optional[str] = None,
    subject: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = DEFAULT_DATA_LIMIT,
    offset: int = 0,
) -> dict:
    """List persisted memory relations, optionally filtered to LLM judgment status."""
    limit = _bounded_int(limit, DEFAULT_DATA_LIMIT, 1, MAX_DATA_LIMIT)
    offset = _bounded_int(offset, 0, 0, 10**9)
    status = status.strip().lower() if status else None
    if status == "all":
        status = None
    if status and status not in RELATION_REVIEW_STATUSES:
        raise ValueError("status must be one of: review, accepted, rejected")
    where = ["1=1"]
    params: list[Any] = []
    relation_column = "j.relation_type" if status else "mr.relation"
    from_column = "j.source_memory_id" if status else "mr.from_memory_id"
    to_column = "j.target_memory_id" if status else "mr.to_memory_id"
    if relation:
        where.append(f"{relation_column} = ?")
        params.append(relation)
    if from_memory_id:
        where.append(f"{from_column} = ?")
        params.append(from_memory_id)
    if to_memory_id:
        where.append(f"{to_column} = ?")
        params.append(to_memory_id)
    if subject:
        where.append("(src.subject LIKE ? OR dst.subject LIKE ?)")
        kw = f"%{subject}%"
        params.extend([kw, kw])
    if status:
        where.append("j.gate_status = ?")
        params.append(status)
    where_sql = " WHERE " + " AND ".join(where)
    if status:
        table_name = "memory_relation_judgments"
        base_sql = (
            "FROM memory_relation_judgments j "
            "LEFT JOIN memory_items src ON src.memory_id = j.source_memory_id "
            "LEFT JOIN memory_items dst ON dst.memory_id = j.target_memory_id "
        )
        select_sql = (
            "SELECT j.candidate_id AS id, j.candidate_id, j.package_id, "
            "j.source_memory_id AS from_memory_id, j.target_memory_id AS to_memory_id, "
            "j.relation_type AS relation, j.confidence AS strength, j.gate_status AS status, "
            "'llm_judgment' AS edge_source, j.model, j.prompt_version, j.llm_status, j.created_at, "
            "src.subject AS from_subject, src.memory_type AS from_type, "
            "src.memory_subtype AS from_subtype, dst.subject AS to_subject, "
            "dst.memory_type AS to_type, dst.memory_subtype AS to_subtype "
        )
        order_sql = " ORDER BY j.confidence DESC, j.candidate_id LIMIT ? OFFSET ?"
    else:
        table_name = "memory_relations"
        base_sql = (
            "FROM memory_relations mr "
            "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
            "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
        )
        select_sql = (
            "SELECT mr.id, mr.from_memory_id, mr.to_memory_id, mr.relation, mr.strength, "
            "src.subject AS from_subject, src.memory_type AS from_type, "
            "src.memory_subtype AS from_subtype, dst.subject AS to_subject, "
            "dst.memory_type AS to_type, dst.memory_subtype AS to_subtype, "
            "'rule' AS edge_source, NULL AS status "
        )
        order_sql = " ORDER BY mr.strength DESC, mr.id LIMIT ? OFFSET ?"
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        if not _table_exists(con, table_name):
            total = 0
            rows = []
        else:
            total = con.execute(
                "SELECT COUNT(1) " + base_sql + where_sql,
                params,
            ).fetchone()[0]
            rows = [
                dict(row)
                for row in con.execute(
                    select_sql + base_sql + where_sql + order_sql,
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
        "filters": {
            "relation": relation,
            "from_memory_id": from_memory_id,
            "to_memory_id": to_memory_id,
            "subject": subject,
            "status": status,
        },
        "items": rows,
        "truncated": offset + len(rows) < total,
    }


def _get_memory_evidence_summary(
    con: sqlite3.Connection,
    memory_id: str,
    limit: int = 5,
) -> list[dict]:
    rows = con.execute(
        "SELECT ml.target_id, ue.source, ue.event_time, ue.title, ml.relation "
        "FROM memory_links ml "
        "JOIN unified_events ue ON ue.event_id = ml.target_id "
        "WHERE ml.memory_id=? AND ml.target_type='event' "
        "ORDER BY ue.event_time DESC, ml.id DESC LIMIT ?",
        (memory_id, limit),
    ).fetchall()
    return [
        {
            "target_id": r["target_id"],
            "source": r["source"],
            "event_time": r["event_time"],
            "title": r["title"],
            "relation": r["relation"],
        }
        for r in rows
    ]


def get_memory_profile(memory_type: Optional[str] = None, limit: int = 200) -> dict:
    """返回长期记忆概览,可按 memory_type 过滤。

    memory_type: tooling / preference / capability / fact / project / habit
    limit: 最多返回多少条明细,默认 200。
    """
    if not _memory_layer_ready():
        return {
            "ok": False,
            "available": False,
            "hint": "记忆层未构建。运行: rag-pipeline --from 5 --skip 10",
            "count": 0,
            "total": 0,
            "by_type": {},
            "items": [],
        }
    limit = max(1, min(int(limit), 500))
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    params: list = []
    where = "WHERE 1=1"
    if memory_type:
        where += " AND memory_type=?"
        params.append(memory_type)

    by_type = {
        r["memory_type"]: r["n"]
        for r in con.execute(
            "SELECT memory_type, COUNT(1) AS n FROM memory_items "
            "GROUP BY memory_type ORDER BY n DESC"
        )
    }
    total = con.execute(
        f"SELECT COUNT(1) FROM memory_items {where}",
        params,
    ).fetchone()[0]
    rows = [
        _memory_row_to_dict(r)
        for r in con.execute(
            "SELECT memory_id, memory_type, memory_subtype, subject, description, "
            "confidence, evidence_count, metadata, created_at "
            f"FROM memory_items {where} "
            "ORDER BY memory_type, evidence_count DESC, confidence DESC, subject "
            "LIMIT ?",
            params + [limit],
        )
    ]
    con.close()
    return {
        "ok": True,
        "available": True,
        "count": len(rows),
        "total": total,
        "by_type": by_type,
        "filter": {"memory_type": memory_type, "limit": limit},
        "items": rows,
    }


def get_memory_relations(subject: str) -> dict:
    """返回某个 subject 匹配记忆的所有入边/出边关系。"""
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    ids = _find_memory_ids_by_subject(con, subject)
    if not ids:
        con.close()
        return {"found": False, "subject": subject, "matches": [], "relations": []}

    placeholders = ",".join("?" * len(ids))
    rows = con.execute(
        "SELECT mr.relation, mr.strength, "
        "src.memory_id AS from_memory_id, src.memory_type AS from_type, "
        "src.memory_subtype AS from_subtype, src.subject AS from_subject, "
        "src.description AS from_description, "
        "dst.memory_id AS to_memory_id, dst.memory_type AS to_type, "
        "dst.memory_subtype AS to_subtype, dst.subject AS to_subject, "
        "dst.description AS to_description "
        "FROM memory_relations mr "
        "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
        "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
        f"WHERE mr.from_memory_id IN ({placeholders}) OR mr.to_memory_id IN ({placeholders}) "
        "ORDER BY mr.strength DESC, mr.relation",
        ids + ids,
    ).fetchall()
    matches = [_get_memory_by_id(con, mid) for mid in ids]
    con.close()
    return {
        "found": True,
        "subject": subject,
        "matches": [m for m in matches if m],
        "relations": [dict(r) for r in rows],
    }


def get_memory_by_subject(subject: str) -> Optional[dict]:
    """按主体查记忆详情,并附带证据数量和图谱关系。"""
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    ids = _find_memory_ids_by_subject(con, subject)
    if not ids:
        con.close()
        return None
    primary = _get_memory_by_id(con, ids[0])
    evidence = [
        dict(r)
        for r in con.execute(
            "SELECT target_type, target_id, relation FROM memory_links "
            "WHERE memory_id=? ORDER BY id LIMIT 20",
            (ids[0],),
        )
    ]
    evidence_summary = _get_memory_evidence_summary(con, ids[0], limit=5)
    con.close()
    rel = get_memory_relations(subject)
    return {
        "ok": True,
        "count": len(rel.get("matches", [])),
        "memory": primary,
        "items": rel.get("matches", []),
        "matches": rel.get("matches", []),
        "relations": rel.get("relations", []),
        "evidence": evidence,
        "evidence_summary": evidence_summary,
    }


def get_memory_neighbors(subject: str, hops: int = 2) -> dict:
    """按图谱关系返回 subject 的 N 跳邻居。"""
    hops = max(1, min(int(hops), 4))
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    start_ids = _find_memory_ids_by_subject(con, subject)
    if not start_ids:
        con.close()
        return {
            "ok": False,
            "found": False,
            "subject": subject,
            "hops": hops,
            "count": 0,
            "levels": [],
        }

    visited = set(start_ids)
    frontier = set(start_ids)
    levels: list[dict] = []
    for level in range(1, hops + 1):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = con.execute(
            "SELECT mr.relation, mr.strength, mr.from_memory_id, mr.to_memory_id, "
            "src.subject AS from_subject, src.memory_type AS from_type, "
            "dst.subject AS to_subject, dst.memory_type AS to_type "
            "FROM memory_relations mr "
            "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
            "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
            f"WHERE mr.from_memory_id IN ({placeholders}) OR mr.to_memory_id IN ({placeholders})",
            list(frontier) + list(frontier),
        ).fetchall()
        next_ids: set[str] = set()
        edges = []
        for r in rows:
            other = r["to_memory_id"] if r["from_memory_id"] in frontier else r["from_memory_id"]
            if other in visited:
                continue
            next_ids.add(other)
            edges.append(dict(r))
        nodes = [_get_memory_by_id(con, mid) for mid in sorted(next_ids)]
        levels.append({
            "hop": level,
            "nodes": [n for n in nodes if n],
            "relations": edges,
        })
        visited |= next_ids
        frontier = next_ids
    starts = [_get_memory_by_id(con, mid) for mid in start_ids]
    con.close()
    count = sum(len(level.get("nodes", [])) for level in levels)
    return {
        "ok": True,
        "found": True,
        "subject": subject,
        "hops": hops,
        "count": count,
        "starts": [s for s in starts if s],
        "levels": levels,
    }


def data_quality_report_contract() -> dict:
    """Return a compact read-only quality report for the public data contracts."""
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    tables = [
        "unified_events",
        "unified_events_rich",
        "event_categories_v2",
        "memory_items",
        "memory_links",
        "memory_relations",
    ]
    warnings: list[str] = []
    try:
        table_info: dict[str, dict] = {}
        for name in tables:
            exists = _table_exists(con, name)
            count = con.execute(f"SELECT COUNT(1) FROM {name}").fetchone()[0] if exists else 0
            table_info[name] = {"exists": exists, "count": count}
            if not exists:
                warnings.append(f"missing table: {name}")

        events: dict[str, Any] = {"available": table_info["unified_events"]["exists"]}
        if events["available"]:
            events.update({
                "total": table_info["unified_events"]["count"],
                "missing": {
                    "event_id": con.execute(
                        "SELECT COUNT(1) FROM unified_events WHERE event_id IS NULL OR event_id=''"
                    ).fetchone()[0],
                    "source": con.execute(
                        "SELECT COUNT(1) FROM unified_events WHERE source IS NULL OR source=''"
                    ).fetchone()[0],
                    "event_time": con.execute(
                        "SELECT COUNT(1) FROM unified_events WHERE event_time IS NULL OR event_time=''"
                    ).fetchone()[0],
                    "title": con.execute(
                        "SELECT COUNT(1) FROM unified_events WHERE title IS NULL OR title=''"
                    ).fetchone()[0],
                },
                "duplicate_event_ids": con.execute(
                    "SELECT COUNT(1) FROM ("
                    "SELECT event_id FROM unified_events WHERE event_id IS NOT NULL AND event_id!='' "
                    "GROUP BY event_id HAVING COUNT(1) > 1)"
                ).fetchone()[0],
                "event_time_range": dict(con.execute(
                    "SELECT MIN(event_time) AS min, MAX(event_time) AS max FROM unified_events"
                ).fetchone()),
                "by_source": {
                    row["source"]: row["count"]
                    for row in con.execute(
                        "SELECT COALESCE(source, '') AS source, COUNT(1) AS count "
                        "FROM unified_events GROUP BY source ORDER BY count DESC"
                    )
                },
            })

        categories: dict[str, Any] = {"available": table_info["event_categories_v2"]["exists"]}
        if categories["available"]:
            categories.update({
                "total": table_info["event_categories_v2"]["count"],
                "missing_category_v2": con.execute(
                    "SELECT COUNT(1) FROM event_categories_v2 "
                    "WHERE category_v2 IS NULL OR category_v2=''"
                ).fetchone()[0],
            })
            if events.get("available"):
                categories["events_without_category_v2"] = con.execute(
                    "SELECT COUNT(1) FROM unified_events ue "
                    "LEFT JOIN event_categories_v2 c ON c.event_id = ue.event_id "
                    "WHERE c.event_id IS NULL OR c.category_v2 IS NULL OR c.category_v2=''"
                ).fetchone()[0]

        memories: dict[str, Any] = {"available": table_info["memory_items"]["exists"]}
        if memories["available"]:
            memories.update({
                "total": table_info["memory_items"]["count"],
                "missing_subject": con.execute(
                    "SELECT COUNT(1) FROM memory_items WHERE subject IS NULL OR subject=''"
                ).fetchone()[0],
                "by_type": {
                    row["memory_type"]: row["count"]
                    for row in con.execute(
                        "SELECT COALESCE(memory_type, '') AS memory_type, COUNT(1) AS count "
                        "FROM memory_items GROUP BY memory_type ORDER BY count DESC"
                    )
                },
            })

        relations: dict[str, Any] = {"available": table_info["memory_relations"]["exists"]}
        if relations["available"]:
            relations.update({
                "total": table_info["memory_relations"]["count"],
                "by_relation": {
                    row["relation"]: row["count"]
                    for row in con.execute(
                        "SELECT COALESCE(relation, '') AS relation, COUNT(1) AS count "
                        "FROM memory_relations GROUP BY relation ORDER BY count DESC"
                    )
                },
            })
            if memories.get("available"):
                relations["dangling_relations"] = con.execute(
                    "SELECT COUNT(1) FROM memory_relations mr "
                    "LEFT JOIN memory_items src ON src.memory_id = mr.from_memory_id "
                    "LEFT JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
                    "WHERE src.memory_id IS NULL OR dst.memory_id IS NULL"
                ).fetchone()[0]

        if events.get("missing"):
            for key, value in events["missing"].items():
                if value:
                    warnings.append(f"unified_events missing {key}: {value}")
        if events.get("duplicate_event_ids"):
            warnings.append(f"duplicate event_id groups: {events['duplicate_event_ids']}")
        if relations.get("dangling_relations"):
            warnings.append(f"dangling memory relations: {relations['dangling_relations']}")

        return {
            "ok": True,
            "database": str(_C.UNIFIED_DB),
            "tables": table_info,
            "events": events,
            "categories": categories,
            "memories": memories,
            "relations": relations,
            "warnings": warnings,
        }
    finally:
        con.close()


def _bounded_memory_graph_ids(G: Any, subject: Optional[str], hops: int) -> tuple[set[str], str | None]:
    """Return node ids for whole graph or a subject-scoped weak-neighbor slice."""
    if not subject:
        return set(G.nodes), None
    start = find_node_by_subject(G, subject)
    if start is None:
        return set(), None
    selected = {start}
    frontier = {start}
    undirected = G.to_undirected()
    for _ in range(hops):
        next_level: set[str] = set()
        for node_id in frontier:
            next_level.update(undirected.neighbors(node_id))
        next_level -= selected
        selected |= next_level
        frontier = next_level
        if not frontier:
            break
    return selected, start


def _memory_node_contract(memory_id: str, data: dict) -> dict:
    description = data.get("description") or ""
    return {
        "id": memory_id,
        "memory_id": memory_id,
        "subject": data.get("subject") or "",
        "memory_type": data.get("memory_type") or "",
        "memory_subtype": data.get("memory_subtype") or "",
        "description": description,
        "description_summary": description[:240],
    }


def _memory_edge_contract(source_id: str, target_id: str, key: Any, data: dict) -> dict:
    edge_source = data.get("edge_source") or "rule"
    out = {
        "id": f"{source_id}->{target_id}:{key}",
        "source": source_id,
        "target": target_id,
        "from_memory_id": source_id,
        "to_memory_id": target_id,
        "relation": data.get("relation") or "",
        "strength": float(data.get("strength") or 0.0),
        "edge_source": edge_source,
    }
    if edge_source == "llm_judgment":
        out.update({
            "gate_status": data.get("gate_status"),
            "confidence": float(data.get("confidence") or 0.0),
            "candidate_id": data.get("candidate_id"),
            "reason": data.get("reason") or "",
        })
    return out


def get_memory_graph_contract(
    subject: Optional[str] = None,
    hops: int = 1,
    include_llm: bool = False,
    limit: int = DEFAULT_MEMORY_GRAPH_LIMIT,
) -> dict:
    """Return bounded JSON graph data for Apps SDK widgets.

    This is read-only and reuses query_graph.load_graph instead of parsing generated HTML.
    """
    limit = max(1, min(int(limit), MAX_MEMORY_GRAPH_LIMIT))
    hops = max(0, min(int(hops), 4))
    con = sqlite3.connect(_C.UNIFIED_DB)
    try:
        G, _, warnings = load_graph(con, include_llm_relations=include_llm)
        selected_ids, start_id = _bounded_memory_graph_ids(G, subject, hops)
        # Sort neighbors alphabetically, but always keep seed node first
        # so subject-scoped queries always include the queried subject
        if start_id and start_id in selected_ids:
            non_seed = sorted(
                selected_ids - {start_id},
                key=lambda node_id: (
                    str(G.nodes[node_id].get("memory_type") or ""),
                    str(G.nodes[node_id].get("subject") or ""),
                    str(node_id),
                ),
            )
            scoped_nodes = [start_id] + non_seed
        else:
            scoped_nodes = sorted(
                selected_ids,
                key=lambda node_id: (
                    str(G.nodes[node_id].get("memory_type") or ""),
                    str(G.nodes[node_id].get("subject") or ""),
                    str(node_id),
                ),
            )
        node_limit = min(limit, len(scoped_nodes))
        kept_nodes = set(scoped_nodes[:node_limit])
        edge_rows = [
            (u, v, k, data)
            for u, v, k, data in G.edges(keys=True, data=True)
            if u in selected_ids and v in selected_ids
        ]
        edge_rows.sort(key=lambda item: (
            str(item[3].get("edge_source") or "rule"),
            str(item[3].get("relation") or ""),
            str(item[0]),
            str(item[1]),
            str(item[2]),
        ))
        kept_edge_rows = [
            (u, v, k, data)
            for u, v, k, data in edge_rows
            if u in kept_nodes and v in kept_nodes
        ]
        edge_limit = min(limit, len(kept_edge_rows))
        nodes = [
            _memory_node_contract(node_id, G.nodes[node_id])
            for node_id in scoped_nodes[:node_limit]
        ]
        edges = [
            _memory_edge_contract(u, v, k, data)
            for u, v, k, data in kept_edge_rows[:edge_limit]
        ]
        total_nodes = len(scoped_nodes)
        total_edges = len(edge_rows)
        return {
            "ok": True,
            "scope": {
                "subject": subject,
                "hops": hops,
                "include_llm": include_llm,
                "limit": limit,
                "start_memory_id": start_id,
                "found": bool(selected_ids) if subject else True,
            },
            "counts": {
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "returned_nodes": len(nodes),
                "returned_edges": len(edges),
                "rule_edges": sum(1 for _, _, _, data in edge_rows if data.get("edge_source") != "llm_judgment"),
                "llm_judgment_edges": sum(1 for _, _, _, data in edge_rows if data.get("edge_source") == "llm_judgment"),
            },
            "nodes": nodes,
            "edges": edges,
            "truncated": len(nodes) < total_nodes or len(edges) < total_edges,
            "warnings": warnings,
        }
    finally:
        con.close()


def get_memory_relation_review_contract(
    limit: int = DEFAULT_RELATION_REVIEW_LIMIT,
    status: Optional[str] = None,
) -> dict:
    """Return read-only LLM relation candidates joined with judgments."""
    limit = max(1, min(int(limit), MAX_RELATION_REVIEW_LIMIT))
    if status:
        status = status.strip().lower()
        if status not in RELATION_REVIEW_STATUSES:
            raise ValueError("status must be one of: review, accepted, rejected")
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    try:
        missing = [
            name
            for name in ("memory_relation_candidates", "memory_relation_judgments")
            if not table_exists(con, name)
        ]
        if missing:
            return {
                "ok": True,
                "count": 0,
                "items": [],
                "truncated": False,
                "missing_tables": missing,
            }

        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE j.gate_status = ?"
            params.append(status)
        total = con.execute(
            "SELECT COUNT(1) FROM memory_relation_judgments j " + where,
            params,
        ).fetchone()[0]
        rows = con.execute(
            """
            SELECT
                c.candidate_id,
                c.package_id,
                c.source_memory_id,
                c.target_memory_id,
                c.relation_type AS candidate_relation_type,
                c.confidence AS candidate_confidence,
                c.candidate_reason,
                c.evidence_refs_json AS candidate_evidence_refs_json,
                c.source_refs_json AS candidate_source_refs_json,
                c.allowed_refs_json,
                c.risk_flags_json AS candidate_risk_flags_json,
                c.llm_status AS candidate_llm_status,
                c.model AS candidate_model,
                c.prompt_version AS candidate_prompt_version,
                c.created_at AS candidate_created_at,
                j.relation_type,
                j.confidence,
                j.evidence_refs_json,
                j.source_refs_json,
                j.risk_flags_json,
                j.gate_status,
                j.gate_reasons_json,
                j.model,
                j.prompt_version,
                j.llm_status,
                j.created_at,
                src.subject AS source_subject,
                src.memory_type AS source_type,
                src.memory_subtype AS source_subtype,
                src.description AS source_description,
                dst.subject AS target_subject,
                dst.memory_type AS target_type,
                dst.memory_subtype AS target_subtype,
                dst.description AS target_description
            FROM memory_relation_judgments j
            JOIN memory_relation_candidates c ON c.candidate_id = j.candidate_id
            LEFT JOIN memory_items src ON src.memory_id = c.source_memory_id
            LEFT JOIN memory_items dst ON dst.memory_id = c.target_memory_id
            {where}
            ORDER BY j.gate_status, j.confidence DESC, c.candidate_id
            LIMIT ?
            """.format(where=where),
            params + [limit],
        ).fetchall()
    finally:
        con.close()

    items = []
    for row in rows:
        data = dict(row)
        items.append({
            "candidate_id": data["candidate_id"],
            "package_id": data["package_id"],
            "source_memory_id": data["source_memory_id"],
            "target_memory_id": data["target_memory_id"],
            "source_subject": data.get("source_subject"),
            "target_subject": data.get("target_subject"),
            "source_memory": {
                "memory_id": data["source_memory_id"],
                "subject": data.get("source_subject"),
                "memory_type": data.get("source_type"),
                "memory_subtype": data.get("source_subtype"),
                "description": data.get("source_description"),
            },
            "target_memory": {
                "memory_id": data["target_memory_id"],
                "subject": data.get("target_subject"),
                "memory_type": data.get("target_type"),
                "memory_subtype": data.get("target_subtype"),
                "description": data.get("target_description"),
            },
            "relation_type": data["relation_type"],
            "candidate_relation_type": data["candidate_relation_type"],
            "confidence": float(data["confidence"]),
            "candidate_confidence": float(data["candidate_confidence"]),
            "gate_status": data["gate_status"],
            "reason": data["candidate_reason"],
            "candidate_reason": data["candidate_reason"],
            "gate_reasons": _parse_json_list(data.get("gate_reasons_json")),
            "evidence_refs": _parse_json_list(data.get("evidence_refs_json")),
            "source_refs": _parse_json_list(data.get("source_refs_json")),
            "allowed_refs": _parse_json_list(data.get("allowed_refs_json")),
            "risk_flags": _parse_json_list(data.get("risk_flags_json")),
            "model": data["model"],
            "prompt_version": data["prompt_version"],
            "llm_status": data["llm_status"],
            "created_at": data["created_at"],
        })
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "truncated": len(items) < total,
    }


# === 合并层(去重视图)====================================================

