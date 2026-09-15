"""Read a complete, bounded session context for incremental candidates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

from personal_knowledge.application.conversation.extraction_views import EventGraph
from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    EventRelation,
    FidelityProfile,
    Provenance,
    RelationKind,
    TypedEvent,
)


def _marks(values: Sequence[str]) -> str:
    return ",".join("?" for _ in values)


def _provenance(row: sqlite3.Row, artifact_hashes: dict[str, str], native_id: str | None) -> Provenance:
    return Provenance(
        artifact_id=row["artifact_id"], artifact_hash=artifact_hashes[row["artifact_id"]],
        native_locator=row["native_locator"], native_session_id=row["session_id"] if "session_id" in row.keys() else row["native_session_id"],
        native_event_id=native_id, contract_version=row["contract_version"],
    )


def load_affected_context(
    db: Path, generation_id: str, event_ids: Sequence[str], *, max_events: int = 2000,
) -> EventGraph:
    """Load full sessions containing ``event_ids`` from an active generation.

    The database is opened read-only; message bodies are materialized only in
    the returned in-memory typed events. A relation crossing the selected
    session boundary fails closed rather than presenting a partial graph.
    """
    if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 10000:
        raise ValueError("max_events must be an int from 1 to 10000")
    if isinstance(event_ids, str):
        raise ValueError("event_ids must be a sequence of IDs")
    ids = tuple(dict.fromkeys(event_ids))
    if not 1 <= len(ids) <= 100:
        raise ValueError("event_ids must contain 1..100 IDs")
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("event_ids must be non-empty strings")
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        authority = con.execute(
            "SELECT active FROM ce_generation_authority WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if authority is None or authority["active"] != 1:
            raise ValueError("stale generation")
        requested = con.execute(
            f"SELECT event_id, session_id FROM ce_events WHERE generation_id=? AND event_id IN ({_marks(ids)})",
            (generation_id, *ids),
        ).fetchall()
        found = {row["event_id"] for row in requested}
        missing = [event_id for event_id in ids if event_id not in found]
        if missing:
            raise ValueError(f"missing event IDs: {missing}")
        session_ids = tuple(dict.fromkeys(row["session_id"] for row in requested))
        session_rows = con.execute(
            f"SELECT * FROM ce_sessions WHERE generation_id=? AND session_id IN ({_marks(session_ids)}) ORDER BY session_id",
            (generation_id, *session_ids),
        ).fetchall()
        if len(session_rows) != len(session_ids):
            raise ValueError("missing session metadata")
        event_rows = con.execute(
            f"SELECT * FROM ce_events WHERE generation_id=? AND session_id IN ({_marks(session_ids)}) "
            "ORDER BY session_id, ordinal, event_id LIMIT ?",
            (generation_id, *session_ids, max_events + 1),
        ).fetchall()
        if len(event_rows) > max_events:
            raise ValueError("max_events exceeded; context is incomplete")
        artifact_ids = tuple(dict.fromkeys(
            [row["artifact_id"] for row in event_rows]
            + [row["artifact_id"] for row in session_rows]
        ))
        artifact_rows = con.execute(
            f"SELECT artifact_id, content_hash FROM ce_source_artifacts WHERE artifact_id IN ({_marks(artifact_ids)})",
            artifact_ids,
        ).fetchall()
        artifact_hashes = {row["artifact_id"]: row["content_hash"] for row in artifact_rows}
        from personal_knowledge.core.conversation_events import FieldDisposition, FieldDispositionRecord
        disposition_rows = con.execute(
            f"SELECT event_id, field_name, disposition, reason FROM ce_field_dispositions "
            f"WHERE generation_id=? AND event_id IN ({_marks(tuple(row['event_id'] for row in event_rows))}) ORDER BY event_id, field_name",
            (generation_id, *(row["event_id"] for row in event_rows)),
        ).fetchall()
        dispositions = {}
        for row in disposition_rows:
            dispositions.setdefault(row["event_id"], []).append(
                FieldDispositionRecord(row["field_name"], FieldDisposition(row["disposition"]), row["reason"] or "")
            )
        events = tuple(
            TypedEvent(
                event_id=row["event_id"], session_id=row["session_id"], kind=EventKind(row["kind"]),
                provenance=_provenance(row, artifact_hashes, row["native_event_id"]),
                fidelity=FidelityProfile.from_dict(json.loads(row["fidelity_json"])),
                field_dispositions=tuple(dispositions.get(row["event_id"], ())),
                occurred_at=row["occurred_at"], ordinal=row["ordinal"],
                native_payload_ref=row["native_payload_ref"], content=row["content"], summary=row["summary"],
            ) for row in event_rows
        )
        event_set = {event.event_id for event in events}
        relation_rows = con.execute(
            f"SELECT * FROM ce_event_relations WHERE generation_id=? AND (source_event_id IN ({_marks(tuple(event_set))}) OR target_event_id IN ({_marks(tuple(event_set))}))",
            (generation_id, *event_set, *event_set),
        ).fetchall()
        if any(row["source_event_id"] not in event_set or row["target_event_id"] not in event_set for row in relation_rows):
            raise ValueError("context boundary")
        relations = tuple(EventRelation(row["relation_id"], row["source_event_id"], row["target_event_id"], RelationKind(row["relation_kind"])) for row in relation_rows)
        sessions = tuple(
            AdaptedSession(
                session_id=row["session_id"], provenance=Provenance(
                    artifact_id=row["artifact_id"], artifact_hash=artifact_hashes[row["artifact_id"]],
                    native_locator=row["native_locator"], native_session_id=row["native_session_id"],
                    contract_version=row["contract_version"],
                ),
                fidelity=FidelityProfile.from_dict(json.loads(row["fidelity_json"])),
                native_session_id=row["native_session_id"], started_at=row["started_at"], ended_at=row["ended_at"],
                cwd=row["cwd"], git_branch=row["git_branch"], model=row["model"], title=row["title"], stop_reason=row["stop_reason"],
            ) for row in session_rows
        )
        return EventGraph(generation_id=generation_id, events=events, relations=relations, sessions=sessions)
    finally:
        con.close()


__all__ = ["load_affected_context"]
