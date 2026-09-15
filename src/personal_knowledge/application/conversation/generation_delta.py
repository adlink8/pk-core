"""Committed generation changes: event references, never a second fact store."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


def record_generation_delta(con: sqlite3.Connection, generation_id: str,
                            prior_generation_id: str | None) -> int | None:
    """Use the activation owner's transaction; never commit independently."""
    if not con.in_transaction:
        raise ValueError("generation delta requires an activation transaction")
    if generation_id == prior_generation_id:
        return None
    con.execute("""CREATE TABLE IF NOT EXISTS ce_generation_deltas (
        delta_id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id TEXT NOT NULL, prior_generation_id TEXT,
        source_manifest_id TEXT NOT NULL, dataset_digest TEXT NOT NULL,
        event_count INTEGER NOT NULL DEFAULT 0, event_digest TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (generation_id) REFERENCES ce_event_generations(generation_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS ce_generation_delta_events (
        delta_id INTEGER NOT NULL, event_id TEXT NOT NULL, change TEXT NOT NULL,
        PRIMARY KEY (delta_id, event_id),
        FOREIGN KEY (delta_id) REFERENCES ce_generation_deltas(delta_id),
        CHECK (change IN ('changed','removed')))""")
    row = con.execute(
        "SELECT source_manifest_id,dataset_digest FROM ce_event_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if not row or not all(row):
        raise ValueError("generation source identity missing")
    delta_id = con.execute(
        "INSERT INTO ce_generation_deltas(generation_id,prior_generation_id,source_manifest_id,dataset_digest) "
        "VALUES (?,?,?,?)", (generation_id, prior_generation_id, row[0], row[1]),
    ).lastrowid
    _record_changes(con, delta_id, generation_id, prior_generation_id)
    checksum = hashlib.sha256()
    count = 0
    for event_id, change in con.execute(
        "SELECT event_id,change FROM ce_generation_delta_events WHERE delta_id=? ORDER BY event_id",
        (delta_id,),
    ):
        checksum.update(f"{len(event_id)}:{event_id}:{change}\n".encode())
        count += 1
    con.execute("UPDATE ce_generation_deltas SET event_count=?,event_digest=? WHERE delta_id=?",
                (count, checksum.hexdigest(), delta_id))
    return delta_id


def _record_changes(con: sqlite3.Connection, delta_id: int, current: str, prior: str | None) -> None:
    # EXCEPT compares complete stored rows excluding generation identity, including
    # content, fidelity and dispositions. Changed relations/sessions affect views
    # even when their member event bodies are unchanged.
    columns = [row[1] for row in con.execute("PRAGMA table_info(ce_events)") if row[1] != "generation_id"]
    fields = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    con.execute(
        f"INSERT INTO ce_generation_delta_events SELECT ?,event_id,'changed' FROM "
        f"(SELECT {fields} FROM ce_events WHERE generation_id=? EXCEPT "
        f"SELECT {fields} FROM ce_events WHERE generation_id=?)", (delta_id, current, prior),
    )
    con.execute(
        "INSERT INTO ce_generation_delta_events SELECT ?,event_id,'removed' FROM "
        "(SELECT event_id FROM ce_events WHERE generation_id=? EXCEPT "
        "SELECT event_id FROM ce_events WHERE generation_id=?)", (delta_id, prior, current),
    )
    for table in ("ce_sessions", "ce_event_relations", "ce_field_dispositions"):
        cols = [row[1] for row in con.execute(f"PRAGMA table_info({table})") if row[1] != "generation_id"]
        fields = ",".join('"' + name.replace('"', '""') + '"' for name in cols)
        changed = (f"WITH delta AS (SELECT {fields} FROM {table} WHERE generation_id=? EXCEPT "
                   f"SELECT {fields} FROM {table} WHERE generation_id=?) ")
        for left, right in ((current, prior), (prior, current)):
            if table == "ce_sessions":
                selector = "SELECT event_id FROM ce_events WHERE generation_id=? AND session_id IN (SELECT session_id FROM delta)"
            elif table == "ce_event_relations":
                selector = "SELECT event_id FROM ce_events WHERE generation_id=? AND (event_id IN (SELECT source_event_id FROM delta) OR event_id IN (SELECT target_event_id FROM delta))"
            else:
                selector = "SELECT event_id FROM ce_events WHERE generation_id=? AND event_id IN (SELECT event_id FROM delta)"
            con.execute(changed + "INSERT OR IGNORE INTO ce_generation_delta_events "
                        f"SELECT ?,event_id,'changed' FROM ({selector})",
                        (left, right, delta_id, current))


class GenerationDeltaRepository:
    """Read-only recovery and bounded event pages from committed activations."""

    def __init__(self, db: Path):
        self.db = Path(db)

    def _connect(self):
        con = sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def latest(self) -> dict | None:
        if not self.db.exists():
            return None
        con = self._connect()
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='ce_generation_deltas'").fetchone():
                return None
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='ce_generation_authority'").fetchone():
                raise ValueError("generation delta schema is incomplete")
            row = con.execute(
                "SELECT d.* FROM ce_generation_deltas d JOIN ce_generation_authority a "
                "ON a.generation_id=d.generation_id AND a.active=1 ORDER BY d.delta_id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def pending(self, *, after_delta: int = 0, limit: int = 100) -> list[dict]:
        """Replay committed history after a consumer-owned durable cursor."""
        if type(after_delta) is not int or after_delta < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid delta cursor or page limit")
        if not self.db.exists():
            return []
        con = self._connect()
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='ce_generation_deltas'").fetchone():
                return []
            return [dict(row) for row in con.execute(
                "SELECT * FROM ce_generation_deltas WHERE delta_id>? ORDER BY delta_id LIMIT ?",
                (after_delta, limit))]
        finally:
            con.close()

    def events(self, delta_id: int, *, limit: int = 100, after: str = "") -> list[dict]:
        if type(delta_id) is not int or delta_id < 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid delta id or page limit")
        if not isinstance(after, str):
            raise ValueError("invalid event cursor")
        con = self._connect()
        try:
            return [dict(row) for row in con.execute(
                "SELECT event_id,change FROM ce_generation_delta_events "
                "WHERE delta_id=? AND event_id>? ORDER BY event_id LIMIT ?", (delta_id, after, limit))]
        finally:
            con.close()
