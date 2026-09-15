"""Phase 16: fill Google normalized_events from activities (idempotent).

Phase 16-02: stable activity-keyed event_id, orphan deletion, run manifest/checksum.

Does NOT run dialogue knowledge-unit extraction.

Usage::

    python src/personal_knowledge/application/build_google_normalized_events.py --dry-run
    python src/personal_knowledge/application/build_google_normalized_events.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import ROOT  # noqa: E402
from personal_knowledge.application.google_structure_lifecycle import (  # noqa: E402
    activity_event_id,
    current_run_id,
    dataset_hash_ids,
    ensure_lifecycle_tables,
    input_hash_from_activities,
    insert_run,
    log_promote,
    make_run_id,
    sha256_text,
)

GOOGLE_DB = ROOT / "Google" / "structured" / "db" / "google_data.sqlite"
BATCH_PREFIX = "phase16_norm"
CONFIG_HASH = sha256_text("normalized_events|activity_event_id_v1|delete_orphans_v1")


@dataclass
class NormStats:
    activities_total: int = 0
    written: int = 0
    skipped_empty: int = 0
    deleted_orphans: int = 0
    before_count: int = 0
    after_count: int = 0
    batch_id: str = ""
    dry_run: bool = True
    run_id: str = ""
    input_hash: str = ""
    dataset_hash: str = ""
    supersedes_run_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS normalized_events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    service TEXT NOT NULL,
    event_time TEXT,
    title TEXT,
    url TEXT,
    domain TEXT,
    content TEXT,
    raw_json TEXT,
    record_hash TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source_file_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_norm_service ON normalized_events(service);
CREATE INDEX IF NOT EXISTS idx_norm_time ON normalized_events(event_time);
CREATE INDEX IF NOT EXISTS idx_norm_hash ON normalized_events(record_hash);
CREATE INDEX IF NOT EXISTS idx_norm_source_file ON normalized_events(source_file_id);
"""


def _record_hash(service: str, event_at: str, title: str, content: str, url: str) -> str:
    raw = f"{service}|{event_at or ''}|{title or ''}|{content or ''}|{url or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build(db_path: Path = GOOGLE_DB, write: bool = False) -> NormStats:
    stats = NormStats(dry_run=not write)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    from personal_knowledge.application.google_structure_lifecycle import utc_now

    batch_id = f"{BATCH_PREFIX}_{utc_now().replace(':', '').replace('-', '')}"
    stats.batch_id = batch_id

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(ENSURE_SQL)
    ensure_lifecycle_tables(con)
    stats.before_count = con.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0]
    rows = con.execute(
        "SELECT id, service, event_at, month, action, category, title_or_query, "
        "channel_or_source, domain, url, raw_excerpt, source_dataset "
        "FROM activities ORDER BY id"
    ).fetchall()
    stats.activities_total = len(rows)
    stats.input_hash = input_hash_from_activities(rows)
    prev = current_run_id(con, "normalized_events")
    stats.supersedes_run_id = prev

    payload = []
    keep_source_ids: list[str] = []
    event_ids: list[str] = []
    for r in rows:
        title = (r["title_or_query"] or "").strip()
        content = (r["raw_excerpt"] or title or "").strip()
        if len(content) < 2 and not title:
            stats.skipped_empty += 1
            continue
        aid = int(r["id"])
        service = r["service"] or "unknown"
        event_at = r["event_at"] or ""
        url = r["url"] or ""
        domain = r["domain"] or ""
        eid = activity_event_id(aid)
        rh = _record_hash(service, event_at, title, content, url)
        raw_obj = {
            "activity_id": aid,
            "month": r["month"],
            "action": r["action"],
            "category": r["category"],
            "channel_or_source": r["channel_or_source"],
            "source_dataset": r["source_dataset"],
        }
        sid = str(aid)
        keep_source_ids.append(sid)
        event_ids.append(eid)
        payload.append(
            (
                eid,
                "Google",
                service,
                event_at,
                title[:500],
                url[:1000],
                domain[:300],
                content[:4000],
                json.dumps(raw_obj, ensure_ascii=False),
                rh,
                batch_id,
                sid,
            )
        )

    stats.written = len(payload)
    stats.dataset_hash = dataset_hash_ids(event_ids)
    # Unique per write for promote journal; content identity is dataset_hash.
    stats.run_id = make_run_id(
        "normalized_events", stats.input_hash, CONFIG_HASH + "|" + utc_now()
    )

    # Orphans: Google normalized rows whose activity id disappeared
    orphan_count = 0
    if keep_source_ids:
        placeholders = ",".join("?" * len(keep_source_ids))
        orphan_count = con.execute(
            f"SELECT COUNT(*) FROM normalized_events WHERE source='Google' "
            f"AND source_file_id NOT IN ({placeholders})",
            keep_source_ids,
        ).fetchone()[0]
    else:
        orphan_count = con.execute(
            "SELECT COUNT(*) FROM normalized_events WHERE source='Google'"
        ).fetchone()[0]
    stats.deleted_orphans = orphan_count

    if write:
        if keep_source_ids:
            placeholders = ",".join("?" * len(keep_source_ids))
            con.execute(
                f"DELETE FROM normalized_events WHERE source='Google' "
                f"AND source_file_id NOT IN ({placeholders})",
                keep_source_ids,
            )
        else:
            con.execute("DELETE FROM normalized_events WHERE source='Google'")
        if payload:
            con.executemany(
                "INSERT OR REPLACE INTO normalized_events "
                "(event_id, source, service, event_time, title, url, domain, content, "
                "raw_json, record_hash, batch_id, source_file_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        # Promote run: supersede previous current of same type
        if prev:
            con.execute(
                "UPDATE google_structure_runs SET status='superseded' "
                "WHERE run_id=? AND status='current'",
                (prev,),
            )
        insert_run(
            con,
            run_id=stats.run_id,
            run_type="normalized_events",
            input_hash=stats.input_hash,
            dataset_hash=stats.dataset_hash,
            activities_count=stats.activities_total,
            config_hash=CONFIG_HASH,
            status="current",
            stats=stats.to_dict(),
            supersedes_run_id=prev,
            gate={"orphans_deleted": orphan_count, "stable_event_id": "activity_v1"},
        )
        log_promote(
            con,
            action="promote_normalized",
            run_id=stats.run_id,
            previous_run_id=prev,
            dataset_hash=stats.dataset_hash,
            detail={"written": stats.written, "deleted_orphans": orphan_count},
        )
        con.commit()

    stats.after_count = (
        con.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0]
        if write
        else len(payload)
    )
    con.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 16: Google normalized_events")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--db", type=Path, default=GOOGLE_DB)
    args = p.parse_args(argv)
    if not args.write:
        args.dry_run = True
    if args.write and args.dry_run:
        args.dry_run = False
    stats = build(args.db, write=args.write)
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
