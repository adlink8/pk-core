"""Shared parser primitive for pathless AgentsView compatibility rows.

Families retain their own detector/capability/adapter version.  This module
only maps an already row-filtered, allowlisted SQLite artifact into honest
partial-fidelity events for the owning family.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
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

_MESSAGE_KINDS = {
    "user": EventKind.USER_MESSAGE,
    "assistant": EventKind.ASSISTANT_MESSAGE,
    "developer": EventKind.DEVELOPER_MESSAGE,
    "system": EventKind.SYSTEM_MESSAGE,
}


def pathless_fidelity() -> FidelityProfile:
    return FidelityProfile.from_levels({
        FidelityDimension.SOURCE_AVAILABILITY: FidelityLevel.PARTIAL,
        FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.PARTIAL,
        FidelityDimension.ORDERING_CONFIDENCE: FidelityLevel.PARTIAL,
        FidelityDimension.RELATION_COMPLETENESS: FidelityLevel.UNAVAILABLE,
        FidelityDimension.CONTENT_AVAILABILITY: FidelityLevel.PARTIAL,
        FidelityDimension.COMPACTION_VISIBILITY: FidelityLevel.UNKNOWN,
        FidelityDimension.NATIVE_ID_STABILITY: FidelityLevel.PARTIAL,
    })


def adapt_pathless_observation(
    artifact_set: SourceArtifactSet,
    *,
    artifact_root: Path,
    family: str,
    adapter_version: str,
    contract_version: str,
) -> AdaptationResult:
    """Adapt one family-filtered AgentsView snapshot without a native claim."""

    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{family} pathless adapter requires exactly one AgentsView snapshot"
        )
    artifact = artifact_set.artifacts[0]
    blob = artifact_root / artifact.artifact_id
    if artifact.source_kind != "sqlite" or not blob.is_file():
        raise EventContractError(
            f"{family} compatibility artifact is not resolvable SQLite"
        )
    sessions, messages = _read_rows(blob, family)
    return _map_rows(
        artifact_set,
        sessions=sessions,
        messages=messages,
        family=family,
        adapter_version=adapter_version,
        contract_version=contract_version,
    )


def _read_rows(blob: Path, family: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    con = sqlite3.connect(f"file:{blob.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        sessions = con.execute(
            "SELECT id, started_at, ended_at FROM sessions "
            "WHERE lower(agent)=? AND deleted_at IS NULL ORDER BY id",
            (family,),
        ).fetchall()
        messages = con.execute(
            "SELECT m.id, m.session_id, m.ordinal, m.role, m.content, "
            "m.timestamp, m.is_system, m.is_sidechain "
            "FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE lower(s.agent)=? AND s.deleted_at IS NULL "
            "ORDER BY m.session_id, m.ordinal, m.id",
            (family,),
        ).fetchall()
        return sessions, messages
    except sqlite3.Error as exc:
        raise EventContractError(
            f"{family} compatibility snapshot violates declared schema: {exc}"
        ) from exc
    finally:
        con.close()


def _map_rows(
    artifact_set: SourceArtifactSet,
    *,
    sessions: list[sqlite3.Row],
    messages: list[sqlite3.Row],
    family: str,
    adapter_version: str,
    contract_version: str,
) -> AdaptationResult:
    artifact = artifact_set.artifacts[0]
    adapted_sessions: list[AdaptedSession] = []
    events: list[TypedEvent] = []
    canonical_session_ids: dict[str, str] = {}
    for row in sessions:
        native_session_id = str(row["id"])
        locator = f"{artifact.relative_path}#sessions:{native_session_id}"
        session_id = make_event_id(
            family, artifact.artifact_id, contract_version, native_session_id,
            kind=EventKind.SESSION_LIFECYCLE, native_locator=locator,
        )
        canonical_session_ids[native_session_id] = session_id
        provenance = _provenance(
            artifact, locator, family=family, contract_version=contract_version,
            session_id=native_session_id, event_id=native_session_id,
        )
        unavailable_path = FieldDispositionRecord(
            "native_file_path", FieldDisposition.UNAVAILABLE,
            f"AgentsView {family} native locator is absent or unresolvable",
        )
        adapted_sessions.append(AdaptedSession(
            session_id=session_id, provenance=provenance,
            fidelity=pathless_fidelity(), native_session_id=native_session_id,
            started_at=row["started_at"], ended_at=row["ended_at"],
            field_dispositions=(unavailable_path,),
        ))
        events.append(TypedEvent(
            event_id=make_event_id(
                family, artifact.artifact_id, contract_version,
                f"session:{native_session_id}", kind=EventKind.SESSION_LIFECYCLE,
                native_locator=locator,
            ),
            session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
            provenance=provenance, fidelity=pathless_fidelity(),
            occurred_at=row["started_at"],
            field_dispositions=(unavailable_path,),
        ))

    for row in messages:
        native_session_id = str(row["session_id"])
        session_id = canonical_session_ids.get(native_session_id)
        if session_id is None:
            raise EventContractError(
                f"{family} message references undeclared session {native_session_id!r}"
            )
        native_message_id = str(row["id"])
        locator = f"{artifact.relative_path}#messages:{native_message_id}"
        role = str(row["role"] or "").lower()
        kind = EventKind.SYSTEM_MESSAGE if row["is_system"] else _MESSAGE_KINDS.get(
            role, EventKind.UNKNOWN_NATIVE
        )
        content = None if row["content"] is None else str(row["content"])
        events.append(TypedEvent(
            event_id=make_event_id(
                family, artifact.artifact_id, contract_version, native_message_id,
                kind=kind, session_id=session_id, native_locator=locator,
            ),
            session_id=session_id, kind=kind,
            provenance=_provenance(
                artifact, locator, family=family,
                contract_version=contract_version,
                session_id=native_session_id, event_id=native_message_id,
            ),
            fidelity=pathless_fidelity(), occurred_at=row["timestamp"],
            ordinal=row["ordinal"],
            native_payload_ref=f"{artifact.artifact_id}:messages:{native_message_id}",
            content=content,
            field_dispositions=(FieldDispositionRecord(
                "content", FieldDisposition.MAPPED,
                "exact AgentsView compatibility observation",
            ),),
        ))

    return AdaptationResult(
        family=family, adapter_version=adapter_version,
        contract_version=contract_version, artifacts=(artifact,),
        events=tuple(events), fidelity=pathless_fidelity(),
        sessions=tuple(adapted_sessions), relations=(),
        warnings=(
            "native reconstruction unavailable; AgentsView text is a "
            "compatibility observation only",
        ),
    )


def _provenance(
    artifact,
    locator: str,
    *,
    family: str,
    contract_version: str,
    session_id: str,
    event_id: str | None,
) -> Provenance:
    del family  # family is carried by stable event/session identity.
    return Provenance(
        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
        native_locator=locator, native_session_id=session_id,
        native_event_id=event_id, contract_version=contract_version,
    )
