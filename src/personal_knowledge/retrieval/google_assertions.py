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

def get_google_structure_status(db_path: Path | None = None) -> dict:
    """Phase 16: Google light structure status (normalized_events + light assertions)."""
    path = Path(db_path) if db_path else _C.GOOGLE_DB
    pointer = path.parent / "google_structure_active_run.txt"
    out: dict[str, Any] = {
        "available": path.exists(),
        "db_path": str(path),
        "activities": None,
        "normalized_events": None,
        "light_assertions": None,
        "assertions_by_type": {},
        "event_id_prefix": "g|",
        "active_run_id": pointer.read_text(encoding="utf-8").strip() if pointer.exists() else None,
        "privacy_policy_version": "service_and_category_v1",
        "consumer": {
            "list": "list_google_light_assertions",
            "get": "get_google_light_assertion",
            "rest": ["GET /google/assertions", "GET /google/assertions/<id>"],
            "mcp": ["list_google_assertions", "get_google_assertion"],
            "not_knowledge_unit": True,
        },
        "note": "aggregate signals only; not dialogue knowledge_units",
    }
    if not path.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "activities" in tables:
            out["activities"] = con.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        if "normalized_events" in tables:
            out["normalized_events"] = con.execute(
                "SELECT COUNT(*) FROM normalized_events"
            ).fetchone()[0]
        if "google_light_assertions" in tables:
            out["light_assertions"] = con.execute(
                "SELECT COUNT(*) FROM google_light_assertions WHERE status='current'"
            ).fetchone()[0]
            out["assertions_by_type"] = dict(
                con.execute(
                    "SELECT assertion_type, COUNT(*) FROM google_light_assertions "
                    "WHERE status='current' GROUP BY 1"
                ).fetchall()
            )
        if "google_structure_runs" in tables:
            row = con.execute(
                "SELECT run_id, dataset_hash, generated_at FROM google_structure_runs "
                "WHERE run_type='light_assertions' AND status='current' "
                "ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
            if row:
                out["active_run_id"] = out.get("active_run_id") or row[0]
                out["dataset_hash"] = (row[1] or "")[:16] + (
                    "…" if row[1] and len(row[1]) > 16 else ""
                )
                out["last_promoted_at"] = row[2]
        con.close()
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def list_google_light_assertions(
    *,
    assertion_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db_path: Path | None = None,
) -> dict:
    """Read-only list of current Google light assertions (not knowledge units).

    Only status='current'. Truncates evidence refs. Never promotes/writes.
    """
    path = Path(db_path) if db_path else _C.GOOGLE_DB
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    envelope: dict[str, Any] = {
        "kind": "google_light_assertion_list",
        "not_knowledge_unit": True,
        "event_id_prefix": "g|",
        "privacy_policy_version": "service_and_category_v1",
        "total": 0,
        "limit": limit,
        "offset": offset,
        "items": [],
    }
    if not path.exists():
        envelope["error"] = "google_db_missing"
        envelope["db_path"] = str(path)
        return envelope
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        tables = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "google_light_assertions" not in tables:
            con.close()
            envelope["error"] = "assertions_table_missing"
            return envelope
        where = "WHERE status='current'"
        params: list[Any] = []
        if assertion_type:
            where += " AND assertion_type=?"
            params.append(assertion_type)
        total = con.execute(
            f"SELECT COUNT(*) FROM google_light_assertions {where}", params
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT assertion_id, assertion_type, subject, claim, evidence_count, "
            f"evidence_refs_json, services_json, categories_json, privacy_tier, "
            f"status, created_at, run_id "
            f"FROM google_light_assertions {where} "
            f"ORDER BY evidence_count DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            try:
                refs = json.loads(r["evidence_refs_json"] or "[]")
            except json.JSONDecodeError:
                refs = []
            try:
                services = json.loads(r["services_json"] or "[]")
            except json.JSONDecodeError:
                services = []
            try:
                categories = json.loads(r["categories_json"] or "[]")
            except json.JSONDecodeError:
                categories = []
            items.append(
                {
                    "kind": "google_light_assertion",
                    "not_knowledge_unit": True,
                    "assertion_id": r["assertion_id"],
                    "assertion_type": r["assertion_type"],
                    "subject": r["subject"],
                    "claim": r["claim"],
                    "evidence_count": r["evidence_count"],
                    "evidence_refs": list(refs)[:10],
                    "services": services,
                    "categories": categories,
                    "privacy_tier": r["privacy_tier"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "run_id": r["run_id"] if "run_id" in r.keys() else None,
                }
            )
        con.close()
        envelope["total"] = total
        envelope["items"] = items
    except Exception as e:
        envelope["error"] = str(e)[:200]
    return envelope


def get_google_light_assertion(
    assertion_id: str,
    *,
    db_path: Path | None = None,
) -> dict | None:
    """Read-only fetch of one current light assertion by id."""
    if not assertion_id or not str(assertion_id).strip():
        return None
    pack = list_google_light_assertions(limit=200, offset=0, db_path=db_path)
    for item in pack.get("items") or []:
        if item.get("assertion_id") == assertion_id:
            return item
    # Direct query for ids not in first page
    path = Path(db_path) if db_path else _C.GOOGLE_DB
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT assertion_id, assertion_type, subject, claim, evidence_count, "
            "evidence_refs_json, services_json, categories_json, privacy_tier, "
            "status, created_at, run_id FROM google_light_assertions "
            "WHERE assertion_id=? AND status='current' LIMIT 1",
            (assertion_id,),
        ).fetchone()
        con.close()
        if not r:
            return None
        refs = json.loads(r["evidence_refs_json"] or "[]")
        return {
            "kind": "google_light_assertion",
            "not_knowledge_unit": True,
            "assertion_id": r["assertion_id"],
            "assertion_type": r["assertion_type"],
            "subject": r["subject"],
            "claim": r["claim"],
            "evidence_count": r["evidence_count"],
            "evidence_refs": list(refs)[:10],
            "services": json.loads(r["services_json"] or "[]"),
            "categories": json.loads(r["categories_json"] or "[]"),
            "privacy_tier": r["privacy_tier"],
            "status": r["status"],
            "created_at": r["created_at"],
            "run_id": r["run_id"] if "run_id" in r.keys() else None,
        }
    except Exception:
        return None


