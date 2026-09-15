"""Phase 16-02: Google structure run manifest, privacy gate, promote/rollback.

Ops-only helpers shared by normalized_events + light_assertions builders.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS google_structure_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    input_hash TEXT,
    dataset_hash TEXT,
    activities_count INTEGER,
    config_hash TEXT,
    status TEXT NOT NULL,
    stats_json TEXT,
    supersedes_run_id TEXT,
    gate_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_gsr_type_status ON google_structure_runs(run_type, status);

CREATE TABLE IF NOT EXISTS google_structure_promote_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    run_id TEXT,
    previous_run_id TEXT,
    dataset_hash TEXT,
    detail_json TEXT
);
"""

# Privacy policy version — bump when keywords change.
PRIVACY_POLICY_VERSION = "service_and_category_v1"
RESTRICTED_CATEGORY_SUBSTR = (
    "支付",
    "金融",
    "卡",
    "地图",
    "地点",
    "位置",
    "导航",
)
RESTRICTED_SERVICES = {"Maps", "地图"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_lifecycle_tables(con: sqlite3.Connection) -> None:
    con.executescript(RUNS_SCHEMA)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def activity_event_id(activity_id: int) -> str:
    """Stable event_id keyed by activity id (title edits do not orphan rows)."""
    return "g|" + sha256_text(f"activity|{int(activity_id)}")[:24]


def is_restricted(service: str, category: str) -> bool:
    svc = service or ""
    cat = category or ""
    if svc in RESTRICTED_SERVICES or "地图" in svc:
        return True
    for s in RESTRICTED_CATEGORY_SUBSTR:
        if s in cat:
            return True
    return False


def input_hash_from_activities(rows: list[sqlite3.Row] | list[tuple]) -> str:
    parts: list[str] = []
    for r in rows:
        if isinstance(r, sqlite3.Row):
            aid = r["id"]
            title = r["title_or_query"] or ""
            svc = r["service"] or ""
            cat = r["category"] or ""
        else:
            aid, svc, title, cat = r[0], r[1], r[2], r[3]
        parts.append(f"{aid}|{svc}|{title}|{cat}")
    parts.sort()
    return sha256_text("\n".join(parts))


def dataset_hash_ids(ids: list[str]) -> str:
    return sha256_text("\n".join(sorted(ids)))


def make_run_id(run_type: str, input_hash: str, config_hash: str) -> str:
    return sha256_text(f"{run_type}|{input_hash}|{config_hash}")[:24]


def insert_run(
    con: sqlite3.Connection,
    *,
    run_id: str,
    run_type: str,
    input_hash: str,
    dataset_hash: str,
    activities_count: int,
    config_hash: str,
    status: str,
    stats: dict[str, Any],
    supersedes_run_id: str | None = None,
    gate: dict[str, Any] | None = None,
) -> None:
    ensure_lifecycle_tables(con)
    con.execute(
        "INSERT OR REPLACE INTO google_structure_runs "
        "(run_id, run_type, generated_at, input_hash, dataset_hash, activities_count, "
        "config_hash, status, stats_json, supersedes_run_id, gate_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            run_type,
            utc_now(),
            input_hash,
            dataset_hash,
            activities_count,
            config_hash,
            status,
            json.dumps(stats, ensure_ascii=False),
            supersedes_run_id,
            json.dumps(gate or {}, ensure_ascii=False),
        ),
    )


def current_run_id(con: sqlite3.Connection, run_type: str) -> str | None:
    ensure_lifecycle_tables(con)
    row = con.execute(
        "SELECT run_id FROM google_structure_runs WHERE run_type=? AND status='current' "
        "ORDER BY generated_at DESC LIMIT 1",
        (run_type,),
    ).fetchone()
    return row[0] if row else None


def log_promote(
    con: sqlite3.Connection,
    *,
    action: str,
    run_id: str | None,
    previous_run_id: str | None,
    dataset_hash: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    ensure_lifecycle_tables(con)
    con.execute(
        "INSERT INTO google_structure_promote_log "
        "(ts, action, run_id, previous_run_id, dataset_hash, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            utc_now(),
            action,
            run_id,
            previous_run_id,
            dataset_hash,
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )


def privacy_gate_assertions(assertions: list[dict]) -> dict[str, Any]:
    """Fail closed if restricted subjects leak into assertion set."""
    violations: list[dict] = []
    for a in assertions:
        subj = a.get("subject") or ""
        services = a.get("services_json") or "[]"
        try:
            svcs = json.loads(services) if isinstance(services, str) else list(services)
        except json.JSONDecodeError:
            svcs = []
        if any(s in RESTRICTED_SERVICES or "地图" in str(s) for s in svcs):
            violations.append({"assertion_id": a.get("assertion_id"), "reason": "restricted_service"})
            continue
        for kw in RESTRICTED_CATEGORY_SUBSTR:
            if kw in subj:
                violations.append(
                    {
                        "assertion_id": a.get("assertion_id"),
                        "reason": f"restricted_subject_keyword:{kw}",
                    }
                )
                break
        refs = a.get("evidence_refs_json") or "[]"
        try:
            ref_list = json.loads(refs) if isinstance(refs, str) else list(refs)
        except json.JSONDecodeError:
            ref_list = []
        for ref in ref_list[:5]:
            if ref and not str(ref).startswith("g|"):
                violations.append(
                    {
                        "assertion_id": a.get("assertion_id"),
                        "reason": "bad_namespace",
                        "ref": str(ref)[:40],
                    }
                )
                break
    return {
        "passed": len(violations) == 0,
        "violations": violations[:20],
        "privacy_policy_version": PRIVACY_POLICY_VERSION,
        "checked": len(assertions),
    }


def write_active_pointer(path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(run_id + "\n", encoding="utf-8")
    tmp.replace(path)
