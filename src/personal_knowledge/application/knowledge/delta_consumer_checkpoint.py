"""Consumer-owned durable checkpoint validation within a caller read/write transaction."""
import sqlite3


def read_prepare_checkpoint(con: sqlite3.Connection) -> tuple[int, str]:
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ce_delta_prepare_cursor" not in tables:
        if "ce_delta_prepare_batches" in tables:
            raise ValueError("partial consumer schema")
        return 0, ""
    if "ce_delta_prepare_batches" not in tables:
        raise ValueError("partial consumer schema")
    row = con.execute("SELECT delta_id,event_cursor FROM ce_delta_prepare_cursor WHERE singleton=1").fetchone()
    if not row:
        raise ValueError("missing checkpoint")
    delta_id, event_cursor = row
    if type(delta_id) is not int or delta_id < 1 or not isinstance(event_cursor, str):
        raise ValueError("invalid checkpoint")
    batch = con.execute(
        "SELECT delta_id,end_cursor FROM ce_delta_prepare_batches ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()
    if batch != row:
        raise ValueError("checkpoint does not match committed batch")
    if not con.execute("SELECT 1 FROM ce_generation_deltas WHERE delta_id=?", (delta_id,)).fetchone():
        raise ValueError("unknown checkpoint delta")
    if event_cursor and not con.execute(
        "SELECT 1 FROM ce_generation_delta_events WHERE delta_id=? AND event_id=?", (delta_id, event_cursor)
    ).fetchone():
        raise ValueError("unknown checkpoint event")
    return delta_id, event_cursor
