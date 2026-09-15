"""Shared event-generation fixtures for public integration seams."""
import pytest
from personal_knowledge.core.conversation_events import (
    AdaptedSession, EventKind, FidelityProfile, Provenance, TypedEvent, make_event_id,
)
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.application.conversation.event_repository import GenerationInput
from personal_knowledge.application.conversation.event_generations import ActivationHooks, GenerationLifecycle


def _prov(native_event_id: str, locator: str) -> Provenance:
    return Provenance(
        artifact_id="art-a", artifact_hash="h" * 8, native_locator=locator,
        native_session_id="s-1", native_event_id=native_event_id,
        contract_version="1",
    )


def _artifact() -> SourceArtifact:
    return SourceArtifact(
        artifact_id="art-a", family="codex", source_kind="file",
        content_hash="h" * 8, capture_method="sha256",
        relative_path="rollout.jsonl", byte_size=10,
    )


def _session() -> AdaptedSession:
    return AdaptedSession(
        session_id="s-1",
        provenance=_prov("s-1", "jsonl:s-1"),
        fidelity=FidelityProfile.complete(),
        native_session_id="s-1",
        started_at="2026-08-12T00:00:00Z",
        ended_at="2026-08-12T00:05:00Z",
    )


def _event(session_id: str, kind: EventKind, locator: str, *,
           native_id: str, ordinal: int, summary: str) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(
            "codex", "art-a", "1", native_id,
            kind=kind, session_id=session_id, native_locator=locator,
        ),
        session_id=session_id,
        kind=kind,
        provenance=_prov(native_id, locator),
        fidelity=FidelityProfile.complete(),
        ordinal=ordinal,
        occurred_at=f"2026-08-12T00:0{ordinal}:00Z",
        summary=summary,
    )


def _generation(dataset_digest: str, user_text: str) -> GenerationInput:
    events = [
        _event("s-1", EventKind.USER_MESSAGE, "jsonl:1", native_id="msg-1",
               ordinal=1, summary=user_text),
        _event("s-1", EventKind.ASSISTANT_MESSAGE, "jsonl:2", native_id="msg-2",
               ordinal=2, summary="assistant reply"),
        _event("s-1", EventKind.COMPACTION_SUMMARY, "jsonl:3", native_id="cmp-1",
               ordinal=3, summary="Compacted earlier turns."),
    ]
    return GenerationInput(
        family="codex",
        adapter_version="1",
        contract_version="1",
        capability_digest="cap-1",
        source_manifest_id="manifest-1",
        dataset_digest=dataset_digest,
        artifacts=(_artifact(),),
        sessions=(_session(),),
        events=tuple(events),
        relations=(),
        dispositions=(),
        warnings=(),
    )


def _activate(life: GenerationLifecycle, generation_id: str, *, digest: str,
              hooks: ActivationHooks | None = None) -> None:
    life.activate(
        generation_id,
        source_manifest_id="manifest-1",
        expected_dataset_digest=digest,
        expected_adapter_families=("codex",),
        hooks=hooks,
    )


@pytest.fixture(name="_generation")
def fixture_generation():
    return _generation


@pytest.fixture(name="_activate")
def fixture_activate():
    return _activate
