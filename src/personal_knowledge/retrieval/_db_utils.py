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
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(lower, min(n, upper))


def _split_csv(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = []
        for item in value:
            raw.extend(str(item).split(","))
    else:
        raw = str(value).split(",")
    return [part.strip() for part in raw if part and part.strip()]


def _normalize_event_fields(fields: str | list[str] | None) -> list[str]:
    out = _split_csv(fields) or list(DEFAULT_EVENT_FIELDS)
    unknown = [field for field in out if field not in EVENT_FIELD_SQL]
    if unknown:
        raise ValueError(f"unknown event fields: {', '.join(unknown)}")
    # Preserve request order while removing duplicates.
    return list(dict.fromkeys(out))


def _event_from_clause() -> str:
    return (
        "FROM unified_events ue "
        "LEFT JOIN unified_events_rich r ON r.event_id = ue.event_id "
        "LEFT JOIN event_categories_v2 c ON c.event_id = ue.event_id "
    )


def _event_filter_sql(
    source: Optional[str] = None,
    service: Optional[str] = None,
    category: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    keyword: Optional[str] = None,
) -> tuple[str, list[Any], dict]:
    where = ["1=1"]
    params: list[Any] = []
    filters = {
        "source": source,
        "service": service,
        "category": category,
        "time_from": time_from,
        "time_to": time_to,
        "keyword": keyword,
    }
    if source:
        where.append("ue.source = ?")
        params.append(source)
    if service:
        where.append("ue.service = ?")
        params.append(service)
    if category:
        where.append("c.category_v2 LIKE ?")
        params.append(f"%{category}%")
    if time_from:
        where.append("ue.event_time >= ?")
        params.append(time_from)
    if time_to:
        where.append("ue.event_time <= ?")
        params.append(time_to)
    if keyword:
        where.append("(ue.title LIKE ? OR ue.content LIKE ? OR COALESCE(r.content_rich, '') LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    return " WHERE " + " AND ".join(where), params, filters


def _memory_layer_ready() -> bool:
    """记忆层是否已构建。"""
    con = sqlite3.connect(_C.UNIFIED_DB)
    try:
        n = con.execute("SELECT COUNT(1) FROM memory_items").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    con.close()
    return n > 0


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"raw": raw}


def _parse_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    return data if isinstance(data, list) else [data]


def _memory_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["metadata"] = _parse_metadata(data.get("metadata"))
    return data


