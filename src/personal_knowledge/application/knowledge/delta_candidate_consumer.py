"""Bounded zero-provider consumption of committed event changes into prepare."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from personal_knowledge.application.knowledge.delta_consumer_checkpoint import read_prepare_checkpoint

from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository
from personal_knowledge.application.conversation.extraction_policy import DEFAULT_POLICY, schedule_candidates
from personal_knowledge.application.conversation.extraction_views import build_all_views
from personal_knowledge.application.knowledge.view_candidate_prepare import (
    CandidateRunKey, CandidateRunRepository, evidence_set_digest,
)


def _next_batch(db: Path, limit: int):
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("BEGIN")
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='ce_generation_deltas'").fetchone():
            raise ValueError("generation delta history unavailable; initialize through verified activation")
        active = con.execute("SELECT generation_id FROM ce_generation_authority WHERE active=1").fetchone()
        if not active:
            raise ValueError("no active generation; cursor unchanged")
        if not con.execute("SELECT 1 FROM ce_generation_deltas WHERE generation_id=? LIMIT 1", (active[0],)).fetchone():
            raise ValueError("active generation delta history unavailable; cursor unchanged")
        cursor = read_prepare_checkpoint(con)
        repo = GenerationDeltaRepository(db)
        headers = repo.pending(after_delta=cursor[0] - 1 if cursor[1] else cursor[0], limit=1)
        if not headers:
            return None
        header = headers[0]
        events = repo.events(header["delta_id"], after=cursor[1], limit=limit)
        last = events[-1]["event_id"] if events else ""
        more = bool(last and repo.events(header["delta_id"], after=last, limit=1))
        present = []
        if events:
            placeholders = ",".join("?" for _ in events)
            present = [row[0] for row in con.execute(
                f"SELECT event_id FROM ce_events WHERE generation_id=? AND event_id IN ({placeholders}) ORDER BY event_id",
                (active[0], *(item["event_id"] for item in events)),
            )]
        return cursor, header, events, last if more else "", active[0], present
    finally:
        con.close()


def _prepare(db, generation_id, event_ids, max_context_events):
    from personal_knowledge.application.conversation.bounded_event_context import load_affected_context

    graph = load_affected_context(db, generation_id, event_ids, max_events=max_context_events)
    result = build_all_views(graph)
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        created_at = con.execute("SELECT created_at FROM ce_event_generations WHERE generation_id=?", (generation_id,)).fetchone()[0]
    finally:
        con.close()
    scheduled = schedule_candidates(DEFAULT_POLICY, result, created_at)
    if not scheduled.candidates:
        return None
    refs = {ref for item in scheduled.candidates for ref in item.evidence_event_refs}
    key = CandidateRunKey(generation_id, result.builder_version, scheduled.policy_digest,
                          "semantic-v1", "schema-v1", evidence_set_digest(refs))
    repository = CandidateRunRepository(db)
    repository.create_schema()
    return repository.prepare_view_run(key, scheduled, result).run_id


def _commit_batch(db, cursor, header, next_event, generation_id, run_id, removed, event_count):
    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute("SELECT generation_id FROM ce_generation_authority WHERE active=1").fetchone()
        if not active or active[0] != generation_id:
            raise ValueError("stale generation; cursor unchanged")
        if read_prepare_checkpoint(con) != cursor:
            raise ValueError("consumer cursor changed; retry")
        con.execute("""CREATE TABLE IF NOT EXISTS ce_delta_prepare_cursor (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), delta_id INTEGER NOT NULL, event_cursor TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS ce_delta_prepare_batches (
            batch_id INTEGER PRIMARY KEY, delta_id INTEGER NOT NULL, start_cursor TEXT NOT NULL,
            end_cursor TEXT NOT NULL, generation_id TEXT NOT NULL, run_id TEXT,
            event_count INTEGER NOT NULL, withdrawal_refs_json TEXT NOT NULL,
            UNIQUE(delta_id,start_cursor))""")
        con.execute("INSERT INTO ce_delta_prepare_batches VALUES (NULL,?,?,?,?,?,?,?)",
                    (header["delta_id"], cursor[1], next_event, generation_id, run_id, event_count, json.dumps(removed)))
        con.execute("INSERT OR REPLACE INTO ce_delta_prepare_cursor VALUES (1,?,?)", (header["delta_id"], next_event))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def consume_delta_candidates(db: Path, *, max_events: int = 100, max_context_events: int = 2000) -> dict:
    """Prepare one event page, then checkpoint; never extract/accept/promote.

    A crash after run creation but before checkpoint safely repeats the immutable
    prepare. Removed current-source references become durable withdrawal work,
    not silent success or automatic deletion of knowledge.
    """
    if type(max_events) is not int or not 1 <= max_events <= 100:
        raise ValueError("max_events must be 1..100")
    if type(max_context_events) is not int or not 1 <= max_context_events <= 10000:
        raise ValueError("max_context_events must be 1..10000")
    db = Path(db)
    if not db.exists():
        return {"status": "idle"}
    batch = _next_batch(db, max_events)
    if batch is None:
        return {"status": "idle"}
    cursor, header, events, next_event, generation_id, present = batch
    run_id = _prepare(db, generation_id, present, max_context_events) if present else None
    removed = sorted({item["event_id"] for item in events} - set(present))
    _commit_batch(db, cursor, header, next_event, generation_id, run_id, removed, len(events))
    return {"status": "prepared" if run_id else "withdrawal_required" if removed else "no_candidates",
            "run_id": run_id, "delta_id": header["delta_id"], "generation_id": generation_id,
            "source_generation_id": header["generation_id"],
            "reconciled": header["generation_id"] != generation_id,
            "event_count": len(events), "withdrawal_count": len(removed), "more_events": bool(next_event),
            "paid_calls": 0}
