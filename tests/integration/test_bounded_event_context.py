from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.conversation.bounded_event_context import (
    load_affected_context,
)
from personal_knowledge.application.conversation.event_repository import (
    EventRepository,
    GenerationInput,
)
from personal_knowledge.application.conversation.event_schema import create_v2_schema
from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    EventRelation,
    FidelityProfile,
    Provenance,
    RelationKind,
    TypedEvent,
)
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact


def _event(event_id: str, session_id: str, ordinal: int) -> TypedEvent:
    return TypedEvent(
        event_id=event_id,
        session_id=session_id,
        kind=EventKind.USER_MESSAGE,
        provenance=Provenance(
            artifact_id="artifact-1", artifact_hash="hash-1",
            native_locator=f"test:{event_id}", native_session_id=session_id,
            native_event_id=event_id,
        ),
        fidelity=FidelityProfile.complete(), ordinal=ordinal,
        content=f"body-{event_id}",
    )


def _seed(db: Path, *, active: bool = True) -> tuple[str, tuple[TypedEvent, ...]]:
    create_v2_schema(db)
    events = (
        _event("e-1", "s-1", 1), _event("e-2", "s-1", 2),
        _event("e-3", "s-2", 1),
    )
    sessions = (
        AdaptedSession(
            session_id="s-1",
            provenance=Provenance(
                artifact_id="artifact-session", artifact_hash="hash-session",
                native_locator="session:s-1", native_session_id="s-1",
            ), fidelity=FidelityProfile.complete(), native_session_id="s-1",
        ),
        AdaptedSession(
            session_id="s-2",
            provenance=Provenance(
                artifact_id="artifact-1", artifact_hash="hash-1",
                native_locator="session:s-2", native_session_id="s-2",
            ), fidelity=FidelityProfile.complete(), native_session_id="s-2",
        ),
    )
    graph_relations = (
        EventRelation("r-1", "e-1", "e-2", RelationKind.PARENT_CHILD),
    )
    generation_id = "gen-1"
    EventRepository(db).write_generation(
        GenerationInput(
            family="test", adapter_version="1", contract_version="1",
            capability_digest="cap", source_manifest_id="manifest",
            dataset_digest="digest",
            artifacts=(
                SourceArtifact(
                    artifact_id="artifact-1", family="test", source_kind="file",
                    content_hash="hash-1", capture_method="test",
                    relative_path="fixture", byte_size=1,
                ),
                SourceArtifact(
                    artifact_id="artifact-session", family="test", source_kind="file",
                    content_hash="hash-session", capture_method="test",
                    relative_path="session-fixture", byte_size=1,
                ),
            ), sessions=sessions, events=events, relations=graph_relations,
        ), generation_id=generation_id,
    )
    if active:
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO ce_generation_authority(generation_id, active) VALUES (?, 1)",
                (generation_id,),
            )
            con.commit()
    return generation_id, events


def test_loads_all_events_in_affected_sessions_with_metadata(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite"
    generation_id, _ = _seed(db)

    graph = load_affected_context(db, generation_id, ["e-1"])

    assert {event.event_id for event in graph.events} == {"e-1", "e-2"}
    assert {session.session_id for session in graph.sessions} == {"s-1"}
    assert graph.events[0].content == "body-e-1"
    assert graph.events[0].provenance.native_locator == "test:e-1"
    assert graph.sessions[0].provenance.native_locator == "session:s-1"
    assert graph.sessions[0].provenance.artifact_hash == "hash-session"
    assert graph.relations[0].relation_id == "r-1"


def test_rejects_invalid_ids_stale_generation_and_event_limit(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite"
    generation_id, _ = _seed(db)
    with pytest.raises(ValueError, match="missing event"):
        load_affected_context(db, generation_id, ["nope"])
    with pytest.raises(ValueError, match="max_events"):
        load_affected_context(db, generation_id, ["e-1"], max_events=1)

    stale_db = tmp_path / "stale.sqlite"
    stale_generation, _ = _seed(stale_db, active=False)
    with pytest.raises(ValueError, match="stale generation"):
        load_affected_context(stale_db, stale_generation, ["e-1"])


def test_rejects_cross_boundary_relation(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite"
    generation_id, _ = _seed(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO ce_event_relations VALUES (?, ?, ?, ?, ?)",
            (generation_id, "r-cross", "e-1", "e-3", RelationKind.BRANCH.value),
        )
        con.commit()
    with pytest.raises(ValueError, match="context boundary"):
        load_affected_context(db, generation_id, ["e-1"])
