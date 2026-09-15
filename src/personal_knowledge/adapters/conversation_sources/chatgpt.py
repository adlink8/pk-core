"""ChatGPT compatibility-observation adapter for pathless AgentsView rows.

AgentsView is an explicitly declared read-only compatibility source for the
currently pathless ChatGPT sessions.  Its filtered immutable SQLite snapshot is
not labelled as a recovered native ChatGPT artifact: fidelity remains partial,
while the allowlisted session/message observations become provenance-bound
typed events (Phase 62 D-08/D-14).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
    CapabilityDescriptor,
    SourceArtifact,
    SourceArtifactSet,
)
from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventContractError,
    EventKind,
    FieldDisposition,
    FieldDispositionRecord,
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    Provenance,
    TypedEvent,
    make_event_id,
)

FAMILY = "chatgpt"
ADAPTER_VERSION = "1.1.0"
CONTRACT_VERSION = "2"

LIVE_ALLOWED_TABLES = ("sessions", "messages")
LIVE_ALLOWED_COLUMNS = {
    "sessions": (
        "id", "agent", "started_at", "ended_at", "deleted_at", "file_path",
    ),
    "messages": (
        "id", "session_id", "ordinal", "role", "content", "timestamp",
        "is_system", "is_sidechain",
    ),
}

_MESSAGE_KINDS = {
    "user": EventKind.USER_MESSAGE,
    "assistant": EventKind.ASSISTANT_MESSAGE,
    "developer": EventKind.DEVELOPER_MESSAGE,
    "system": EventKind.SYSTEM_MESSAGE,
}


def _fidelity() -> FidelityProfile:
    return FidelityProfile.from_levels({
        FidelityDimension.SOURCE_AVAILABILITY: FidelityLevel.PARTIAL,
        FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.PARTIAL,
        FidelityDimension.ORDERING_CONFIDENCE: FidelityLevel.PARTIAL,
        FidelityDimension.RELATION_COMPLETENESS: FidelityLevel.UNAVAILABLE,
        FidelityDimension.CONTENT_AVAILABILITY: FidelityLevel.PARTIAL,
        FidelityDimension.COMPACTION_VISIBILITY: FidelityLevel.UNKNOWN,
        FidelityDimension.NATIVE_ID_STABILITY: FidelityLevel.PARTIAL,
    })


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY,
        adapter_version=ADAPTER_VERSION,
        contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE,
            EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE,
            EventKind.DEVELOPER_MESSAGE,
            EventKind.SYSTEM_MESSAGE,
            EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "agentsview_pathless_compatibility_rows",
            "native_reconstruction": "unavailable",
            "observation_only": "true",
            "sqlite_tables": ",".join(LIVE_ALLOWED_TABLES),
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    relative = (artifact.relative_path or "").lower()
    return artifact.source_kind == "sqlite" and (
        "sessions.db" in relative or "agentsview" in relative
    )


def _provenance(
    artifact: SourceArtifact,
    locator: str,
    *,
    session_id: str,
    event_id: str | None = None,
) -> Provenance:
    return Provenance(
        artifact_id=artifact.artifact_id,
        artifact_hash=artifact.content_hash,
        native_locator=locator,
        native_session_id=session_id,
        native_event_id=event_id,
        contract_version=CONTRACT_VERSION,
    )


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt allowlisted pathless ChatGPT rows from one immutable snapshot."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one AgentsView snapshot artifact"
        )
    artifact = artifact_set.artifacts[0]
    blob = artifact_root / artifact.content_hash[:32]
    if artifact.source_kind != "sqlite" or not blob.is_file():
        raise EventContractError("chatgpt compatibility artifact is not resolvable SQLite")

    con = sqlite3.connect(f"file:{blob.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        sessions = con.execute(
            "SELECT id, started_at, ended_at FROM sessions "
            "WHERE lower(agent)=? AND deleted_at IS NULL "
            "AND (file_path IS NULL OR trim(file_path)='') ORDER BY id",
            (FAMILY,),
        ).fetchall()
        messages = con.execute(
            "SELECT m.id, m.session_id, m.ordinal, m.role, m.content, "
            "m.timestamp, m.is_system, m.is_sidechain "
            "FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE lower(s.agent)=? AND s.deleted_at IS NULL "
            "AND (s.file_path IS NULL OR trim(s.file_path)='') "
            "ORDER BY m.session_id, m.ordinal, m.id",
            (FAMILY,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise EventContractError(
            f"chatgpt compatibility snapshot violates declared schema: {exc}"
        ) from exc
    finally:
        con.close()

    adapted_sessions: list[AdaptedSession] = []
    events: list[TypedEvent] = []
    canonical_session_ids: dict[str, str] = {}
    for row in sessions:
        native_session_id = str(row["id"])
        locator = f"{artifact.relative_path}#sessions:{native_session_id}"
        session_id = make_event_id(
            FAMILY,
            artifact.artifact_id,
            CONTRACT_VERSION,
            native_session_id,
            kind=EventKind.SESSION_LIFECYCLE,
            native_locator=locator,
        )
        canonical_session_ids[native_session_id] = session_id
        provenance = _provenance(
            artifact, locator, session_id=native_session_id,
            event_id=native_session_id,
        )
        unavailable_path = FieldDispositionRecord(
            "native_file_path",
            FieldDisposition.UNAVAILABLE,
            "AgentsView ChatGPT session has no recoverable native path",
        )
        adapted_sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=provenance,
            fidelity=_fidelity(),
            native_session_id=native_session_id,
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            field_dispositions=(unavailable_path,),
        ))
        events.append(TypedEvent(
            event_id=make_event_id(
                FAMILY,
                artifact.artifact_id,
                CONTRACT_VERSION,
                f"session:{native_session_id}",
                kind=EventKind.SESSION_LIFECYCLE,
                native_locator=locator,
            ),
            session_id=session_id,
            kind=EventKind.SESSION_LIFECYCLE,
            provenance=provenance,
            fidelity=_fidelity(),
            occurred_at=row["started_at"],
            field_dispositions=(unavailable_path,),
        ))

    for row in messages:
        native_session_id = str(row["session_id"])
        session_id = canonical_session_ids.get(native_session_id)
        if session_id is None:
            raise EventContractError(
                f"chatgpt message references undeclared session {native_session_id!r}"
            )
        native_message_id = str(row["id"])
        locator = f"{artifact.relative_path}#messages:{native_message_id}"
        role = str(row["role"] or "").lower()
        kind = EventKind.SYSTEM_MESSAGE if row["is_system"] else _MESSAGE_KINDS.get(
            role, EventKind.UNKNOWN_NATIVE
        )
        content = None if row["content"] is None else str(row["content"])
        mapped_content = FieldDispositionRecord(
            "content",
            FieldDisposition.MAPPED,
            "exact AgentsView compatibility observation",
        )
        events.append(TypedEvent(
            event_id=make_event_id(
                FAMILY,
                artifact.artifact_id,
                CONTRACT_VERSION,
                native_message_id,
                kind=kind,
                session_id=session_id,
                native_locator=locator,
            ),
            session_id=session_id,
            kind=kind,
            provenance=_provenance(
                artifact, locator, session_id=native_session_id,
                event_id=native_message_id,
            ),
            fidelity=_fidelity(),
            occurred_at=row["timestamp"],
            ordinal=row["ordinal"],
            native_payload_ref=f"{artifact.artifact_id}:messages:{native_message_id}",
            content=content,
            field_dispositions=(mapped_content,),
        ))

    return AdaptationResult(
        family=FAMILY,
        adapter_version=ADAPTER_VERSION,
        contract_version=CONTRACT_VERSION,
        artifacts=(artifact,),
        events=tuple(events),
        fidelity=_fidelity(),
        sessions=tuple(adapted_sessions),
        relations=(),
        warnings=(
            "native reconstruction unavailable; AgentsView text is a compatibility observation only",
        ),
    )


__all__ = [
    "ADAPTER_VERSION",
    "CONTRACT_VERSION",
    "FAMILY",
    "LIVE_ALLOWED_COLUMNS",
    "LIVE_ALLOWED_TABLES",
    "adapt",
    "capability",
    "detect",
]
