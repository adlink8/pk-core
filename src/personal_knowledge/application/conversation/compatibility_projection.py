"""Phase 62-04: deterministic event-to-legacy compatibility projection seam.

Phase 62 CONTEXT D-17/D-19: the legacy ``canonical_sessions`` /
``canonical_messages`` / ``canonical_tool_events`` tables become a
deterministic compatibility projection of exactly one active v2 event
generation. Existing consumers continue through ``ConversationRepository``
while new consumers use the event-aware repository seam.

This module owns ONLY event-to-legacy mapping:

  - :func:`build_compatibility_projection` reads the typed generation and
    computes the lossy session/message/tool rows plus a deterministic
    :class:`ProjectionFingerprint` (generation lineage).
  - :func:`write_compatibility_projection` persists those rows inside the
    caller's transaction; :func:`clear_compatibility_projection` restores the
    prior projection during rollback.
  - :func:`compute_projection` is the pure mapping used by both.

It never activates a generation and never touches ``ce_generation_authority``
(activation belongs to :mod:`.event_generations`). Projected message rows come
only from message-kind events; reasoning, usage, compaction summaries,
boundaries, file-context and unknown-native events are reported as excluded
and never flattened into user facts (D-23). Each event maps to at most one row,
so there is no double counting.

No I/O outside the caller-provided DB path; no network, no provider calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from personal_knowledge.core.conversation_events import EventKind

# Kinds that project to canonical_messages rows, with the legacy role mapping.
MESSAGE_KINDS: dict[EventKind, str] = {
    EventKind.USER_MESSAGE: "user",
    EventKind.ASSISTANT_MESSAGE: "assistant",
    EventKind.DEVELOPER_MESSAGE: "developer",
    EventKind.SYSTEM_MESSAGE: "system",
}

# Kinds that project to canonical_tool_events rows, with the legacy source_kind.
TOOL_KINDS: dict[EventKind, str] = {
    EventKind.TOOL_CALL: "call",
    EventKind.TOOL_RESULT: "result",
}

# Event kinds intentionally not flattened into a compatibility row.
EXCLUDED_KINDS: frozenset[EventKind] = frozenset(
    kind
    for kind in EventKind
    if kind not in MESSAGE_KINDS and kind not in TOOL_KINDS
)

PROJECTED_TABLES: tuple[str, ...] = (
    "canonical_sessions",
    "canonical_messages",
    "canonical_tool_events",
)

_SESSION_COLUMNS = (
    "canonical_session_id", "primary_source", "agent", "started_at", "ended_at",
    "message_count", "user_message_count", "file_hash", "parent_canonical_id",
    "relationship_type", "cwd", "git_branch", "model", "evidence_eligible",
    "evidence_scope", "merged", "lifecycle", "superseded_by_canonical_id",
)

_MESSAGE_COLUMNS = (
    "canonical_message_id", "canonical_session_id", "source",
    "source_message_ref", "ordinal", "role", "content", "content_length",
    "timestamp", "model", "is_system", "is_sidechain", "content_hash",
    "evidence_scope",
)

_TOOL_COLUMNS = (
    "canonical_tool_id", "canonical_session_id", "source", "source_kind",
    "tool_name", "category", "status", "call_index", "subagent_session_id",
    "content_length", "timestamp",
)


class CompatibilityProjectionError(RuntimeError):
    """A v2 generation cannot be projected deterministically."""


@dataclass(frozen=True)
class ProjectionFingerprint:
    """Deterministic generation lineage of a compatibility projection."""

    generation_id: str
    session_count: int
    message_count: int
    tool_count: int
    digest: str

    def to_dict(self) -> dict:
        return {
            "generation_id": self.generation_id,
            "session_count": self.session_count,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class CompatibilityProjectionReport:
    """The lossy compatibility rows computed for one generation."""

    generation_id: str
    sessions: tuple[dict, ...]
    messages: tuple[dict, ...]
    tools: tuple[dict, ...]
    excluded: tuple[dict, ...]
    fingerprint: ProjectionFingerprint

    def to_dict(self) -> dict:
        return {
            "generation_id": self.generation_id,
            "sessions": list(self.sessions),
            "messages": list(self.messages),
            "tools": list(self.tools),
            "excluded": list(self.excluded),
            "fingerprint": self.fingerprint.to_dict(),
        }


def _norm_hash(prefix: str, *parts: object) -> str:
    payload = "|".join(str(p) for p in parts)
    return f"{prefix}|{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _content_hash(content: str | None) -> str | None:
    if not content:
        return None
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()[:32]


def compute_projection(
    generation_id: str,
    session_rows: list[dict],
    event_rows: list[dict],
) -> CompatibilityProjectionReport:
    """Pure deterministic event-to-legacy mapping (D-17).

    ``session_rows`` are ``ce_sessions`` rows; ``event_rows`` are ``ce_events``
    rows as returned by the event repository. Neither input is mutated.
    """
    if not generation_id:
        raise CompatibilityProjectionError("projection requires a generation id")

    sessions_by_id = {s["session_id"]: s for s in session_rows}
    family_by_session = {s["session_id"]: s.get("family", "") for s in session_rows}
    messages, tools, excluded = _classify_events(
        generation_id, sessions_by_id, event_rows
    )
    projected_sessions = _project_sessions(
        sessions_by_id, family_by_session, messages
    )
    projected_messages = _project_messages(messages)
    projected_tools = _project_tools(tools)

    fingerprint = _make_fingerprint(
        generation_id, projected_sessions, projected_messages, projected_tools
    )
    return CompatibilityProjectionReport(
        generation_id=generation_id,
        sessions=tuple(projected_sessions),
        messages=tuple(projected_messages),
        tools=tuple(projected_tools),
        excluded=tuple(excluded),
        fingerprint=fingerprint,
    )


def _classify_events(
    generation_id: str,
    sessions_by_id: dict[str, dict],
    event_rows: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], list[dict]]:
    """Group events into message rows, tool rows and excluded events.

    Raises :class:`CompatibilityProjectionError` when an event references a
    session that is absent from the generation (fail closed).
    """
    messages: dict[str, list[dict]] = {sid: [] for sid in sessions_by_id}
    tools: dict[str, list[dict]] = {sid: [] for sid in sessions_by_id}
    excluded: list[dict] = []
    for event in sorted(
        event_rows, key=lambda e: (e.get("ordinal") or 0, e.get("event_id") or "")
    ):
        kind = EventKind(event["kind"])
        sid = event["session_id"]
        if sid not in sessions_by_id:
            raise CompatibilityProjectionError(
                f"event {event.get('event_id')} references a session ({sid}) "
                "that is absent from this generation"
            )
        if kind in MESSAGE_KINDS:
            messages[sid].append(event)
        elif kind in TOOL_KINDS:
            tools[sid].append(event)
        else:
            excluded.append({
                "event_id": event.get("event_id"),
                "kind": kind.value,
                "session_id": sid,
                "native_locator": event.get("native_locator"),
            })
    return messages, tools, excluded


def _project_sessions(
    sessions_by_id: dict[str, dict],
    family_by_session: dict[str, str],
    messages: dict[str, list[dict]],
) -> list[dict]:
    """Map each generation session to one lossy canonical session row."""
    projected: list[dict] = []
    for sid, srow in sorted(sessions_by_id.items()):
        msgs = messages.get(sid, [])
        user_count = sum(
            1 for m in msgs if MESSAGE_KINDS[EventKind(m["kind"])] == "user"
        )
        projected.append({
            # Ids derive ONLY from generation-independent ce_* ids (session_id
            # / event_id are deterministic across generations), so projection
            # ids survive re-activation of a newer generation — otherwise
            # every activation would invalidate downstream semantic-layer
            # artifacts keyed by these ids.
            "canonical_session_id": _norm_hash("v2|cs", sid),
            # Live canonical_sessions has CHECK(primary_source IN
            # ('agentsview','legacy')); 'v2' is not admissible, so projection
            # rows are tagged 'legacy' (the v2|cs| session-id prefix
            # keeps them distinguishable from legacy-era rows).
            "primary_source": "legacy",
            "agent": family_by_session.get(sid) or None,
            "started_at": srow.get("started_at"),
            "ended_at": srow.get("ended_at"),
            "message_count": len(msgs),
            "user_message_count": user_count,
            "file_hash": None,
            "parent_canonical_id": None,
            "relationship_type": None,
            "cwd": srow.get("cwd"),
            "git_branch": srow.get("git_branch"),
            "model": srow.get("model"),
            "evidence_eligible": 1,
            "evidence_scope": "user",
            "merged": 0,
            "lifecycle": "active",
            "superseded_by_canonical_id": None,
        })
    return projected


def _project_messages(
    messages: dict[str, list[dict]]
) -> list[dict]:
    """Map message-kind events to canonical_messages rows (documented lossy)."""
    projected: list[dict] = []
    for sid, events in sorted(messages.items()):
        for ordinal, event in enumerate(sorted(
            events, key=lambda e: (e.get("ordinal") or 0, e.get("event_id") or "")
        ), start=1):
            # ``content`` is the exact mapped source body.  ``None`` means an
            # older adapter did not emit the optional field, so the bounded
            # summary remains a backward-compatible fallback.  An explicit
            # empty string is a legitimate tool-only/empty native message and
            # must not be replaced with summary prose.
            content = event.get("content")
            if content is None:
                content = event.get("summary") or None
            role = MESSAGE_KINDS[EventKind(event["kind"])]
            projected.append({
                "canonical_message_id": _norm_hash(
                    "v2|cm", event["event_id"]
                ),
                "canonical_session_id": _norm_hash("v2|cs", sid),
                # Live CHECK(source IN ('agentsview','legacy')); 'v2' is not
                # admissible (see _project_sessions).
                "source": "legacy",
                "source_message_ref": event.get("native_locator"),
                "ordinal": ordinal,
                "role": role,
                "content": content,
                "content_length": len(content or ""),
                "timestamp": event.get("occurred_at"),
                "model": None,
                "is_system": 1 if role == "system" else 0,
                "is_sidechain": 0,
                "content_hash": _content_hash(content),
                "evidence_scope": "user",
            })
    return projected


def _project_tools(
    tools: dict[str, list[dict]]
) -> list[dict]:
    """Map tool-kind events to canonical_tool_events rows (documented lossy)."""
    projected: list[dict] = []
    for sid, events in sorted(tools.items()):
        for event in sorted(
            events, key=lambda e: (e.get("ordinal") or 0, e.get("event_id") or "")
        ):
            source_kind = TOOL_KINDS[EventKind(event["kind"])]
            summary = event.get("summary") or None
            projected.append({
                "canonical_tool_id": _norm_hash(
                    "v2|cte", event["event_id"]
                ),
                "canonical_session_id": _norm_hash("v2|cs", sid),
                # Live CHECK(source IN ('agentsview','legacy')); 'v2' is not
                # admissible (see _project_sessions).
                "source": "legacy",
                "source_kind": source_kind,
                "tool_name": summary if source_kind == "call" else None,
                "category": None,
                "status": "ok",
                "call_index": event.get("ordinal"),
                "subagent_session_id": None,
                "content_length": len(summary or ""),
                "timestamp": event.get("occurred_at"),
                "source_ref": event.get("native_locator"),
            })
    return projected


def _make_fingerprint(
    generation_id: str,
    sessions: list[dict],
    messages: list[dict],
    tools: list[dict],
) -> ProjectionFingerprint:
    """Deterministic digest over the exact projected rows (generation lineage)."""
    payload = {
        "generation_id": generation_id,
        "sessions": sorted(
            sessions, key=lambda r: r["canonical_session_id"]
        ),
        "messages": sorted(
            messages, key=lambda r: r["canonical_message_id"]
        ),
        "tools": sorted(tools, key=lambda r: r["canonical_tool_id"]),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return ProjectionFingerprint(
        generation_id=generation_id,
        session_count=len(sessions),
        message_count=len(messages),
        tool_count=len(tools),
        digest=digest,
    )


def _read_generation(db: Path, generation_id: str) -> tuple[list[dict], list[dict]]:
    if not db.exists():
        raise CompatibilityProjectionError(
            f"event database missing: {db}"
        )
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sessions = [
            dict(r) for r in con.execute(
                "SELECT session_id, family, native_session_id, started_at, "
                "ended_at, native_locator, contract_version, fidelity_json, "
                "cwd, git_branch, model, title, stop_reason "
                "FROM ce_sessions WHERE generation_id=? ORDER BY session_id",
                (generation_id,),
            )
        ]
        events = [
            dict(r) for r in con.execute(
                "SELECT event_id, session_id, kind, artifact_id, native_locator, "
                "native_event_id, occurred_at, ordinal, native_payload_ref, "
                "content, summary, contract_version, fidelity_json "
                "FROM ce_events WHERE generation_id=? "
                "ORDER BY ordinal, event_id",
                (generation_id,),
            )
        ]
    finally:
        con.close()
    return sessions, events


def build_compatibility_projection(
    db: Path, generation_id: str
) -> CompatibilityProjectionReport:
    """Compute the deterministic compatibility projection of one generation.

    Read-only: never writes the compatibility tables or the authority pointer.
    """
    sessions, events = _read_generation(db, generation_id)
    return compute_projection(generation_id, sessions, events)


def write_compatibility_projection(
    con: sqlite3.Connection, report: CompatibilityProjectionReport
) -> None:
    """Write the projected rows into the three compatibility tables.

    Runs inside the caller's transaction so an activation/rollback owner can
    commit or restore atomically with the authority pointer. Table DDL is
    ensured idempotently (the live canonical DB already has these tables).
    """
    _ensure_tables(con)
    if report.sessions:
        con.executemany(
            "INSERT OR REPLACE INTO canonical_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [_row_for_insert(r, _SESSION_COLUMNS) for r in report.sessions],
        )
    if report.messages:
        con.executemany(
            "INSERT OR REPLACE INTO canonical_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [_row_for_insert(r, _MESSAGE_COLUMNS) for r in report.messages],
        )
    if report.tools:
        con.executemany(
            "INSERT OR REPLACE INTO canonical_tool_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [_row_for_insert(r, _TOOL_COLUMNS) for r in report.tools],
        )


def clear_compatibility_projection(con: sqlite3.Connection) -> None:
    """Delete every compatibility row produced by any v2 projection.

    Used by the activation owner during rollback to restore the prior
    projection. Deletes ONLY rows whose canonical id carries the ``v2|``
    projection prefix, so pre-existing legacy-era rows (``agentsview`` /
    ``legacy`` sources) are preserved — activation must never discard the
    product's existing canonical conversation data (D-18/D-19). Never deletes
    the tables themselves (D-19).
    """
    _ensure_tables(con)
    con.execute("DELETE FROM canonical_tool_events WHERE canonical_tool_id LIKE 'v2|%'")
    con.execute("DELETE FROM canonical_messages WHERE canonical_message_id LIKE 'v2|%'")
    con.execute("DELETE FROM canonical_sessions WHERE canonical_session_id LIKE 'v2|%'")


def _row_for_insert(row: dict, columns: tuple[str, ...]) -> tuple:
    return tuple(row.get(c) for c in columns)


def _ensure_tables(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_sessions (
            canonical_session_id TEXT PRIMARY KEY, primary_source TEXT NOT NULL,
            agent TEXT, started_at TEXT, ended_at TEXT, message_count INTEGER,
            user_message_count INTEGER, file_hash TEXT, parent_canonical_id TEXT,
            relationship_type TEXT, cwd TEXT, git_branch TEXT, model TEXT,
            evidence_eligible INTEGER NOT NULL DEFAULT 1,
            evidence_scope TEXT NOT NULL DEFAULT 'user',
            merged INTEGER NOT NULL DEFAULT 0,
            lifecycle TEXT NOT NULL DEFAULT 'active',
            superseded_by_canonical_id TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_messages (
            canonical_message_id TEXT PRIMARY KEY,
            canonical_session_id TEXT NOT NULL, source TEXT NOT NULL,
            source_message_ref TEXT, ordinal INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT, content_length INTEGER, timestamp TEXT, model TEXT,
            is_system INTEGER NOT NULL DEFAULT 0,
            is_sidechain INTEGER NOT NULL DEFAULT 0, content_hash TEXT,
            evidence_scope TEXT NOT NULL DEFAULT 'user')"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_tool_events (
            canonical_tool_id TEXT PRIMARY KEY,
            canonical_session_id TEXT NOT NULL, source TEXT NOT NULL,
            source_kind TEXT NOT NULL, tool_name TEXT, category TEXT, status TEXT,
            call_index INTEGER, subagent_session_id TEXT, content_length INTEGER,
            timestamp TEXT)"""
    )


__all__ = [
    "CompatibilityProjectionError",
    "CompatibilityProjectionReport",
    "PROJECTED_TABLES",
    "ProjectionFingerprint",
    "build_compatibility_projection",
    "clear_compatibility_projection",
    "compute_projection",
    "write_compatibility_projection",
]
