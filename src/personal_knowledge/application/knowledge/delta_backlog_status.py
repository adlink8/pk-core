"""Read-only prepare backlog, distinct from accepted/usable memory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.application.knowledge.delta_consumer_checkpoint import read_prepare_checkpoint


def inspect_delta_backlog(db: Path) -> dict:
    """Observe authority, pending pages and withdrawal work in one snapshot."""
    state = {
        "status": "uninitialized", "reason": "conversation_database_missing",
        "active_generation_id": None, "pending_delta_count": None,
        "pending_event_count": None, "withdrawal_ref_count": None,
        "prepared_run_count": None, "prepare_caught_up": False,
        "memory_ready": False, "paid_calls": 0,
        "extraction_status": "not_automated",
    }
    db = Path(db)
    if not db.exists():
        return state
    con = None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        con.execute("BEGIN")
        _read_state(con, state)
    except (sqlite3.Error, ValueError):
        state.update(status="degraded", reason="backlog_schema_or_checkpoint_invalid",
                     prepare_caught_up=False)
    finally:
        if con is not None:
            con.close()
    return state


def _read_state(con: sqlite3.Connection, state: dict) -> None:
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ce_generation_authority" not in tables:
        state["reason"] = "event_authority_missing"
        return
    active = con.execute("SELECT generation_id FROM ce_generation_authority WHERE active=1").fetchall()
    if len(active) != 1:
        state.update(status="degraded", reason="active_generation_not_unique")
        return
    state["active_generation_id"] = active[0][0]
    if "ce_generation_deltas" not in tables:
        state["reason"] = "activation_delta_history_missing"
        return
    if not con.execute(
        "SELECT 1 FROM ce_generation_deltas WHERE generation_id=? LIMIT 1", (active[0][0],)
    ).fetchone():
        state["reason"] = "activation_delta_history_missing"
        return
    cursor, event_cursor = read_prepare_checkpoint(con)
    pending_deltas = con.execute(
        "SELECT COUNT(*) FROM ce_generation_deltas WHERE delta_id>? OR (delta_id=? AND ?<>'')",
        (cursor, cursor, event_cursor),
    ).fetchone()[0]
    pending_events = con.execute(
        "SELECT COUNT(*) FROM ce_generation_delta_events WHERE delta_id>? "
        "OR (delta_id=? AND ?<>'' AND event_id>?)",
        (cursor, cursor, event_cursor, event_cursor),
    ).fetchone()[0]
    withdrawals, runs = 0, 0
    if "ce_delta_prepare_batches" in tables:
        withdrawals = con.execute(
            "SELECT COUNT(*) FROM ce_delta_prepare_batches b, json_each(b.withdrawal_refs_json)"
        ).fetchone()[0]
        runs = con.execute("SELECT COUNT(DISTINCT run_id) FROM ce_delta_prepare_batches").fetchone()[0]
    state.update(
        status="pending" if pending_deltas else "withdrawal_required" if withdrawals else "prepared",
        reason=None, pending_delta_count=pending_deltas, pending_event_count=pending_events,
        withdrawal_ref_count=withdrawals, prepared_run_count=runs,
        prepare_caught_up=pending_deltas == 0,
    )
