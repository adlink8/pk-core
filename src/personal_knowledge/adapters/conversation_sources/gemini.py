"""Phase 62-02: Gemini single-JSON adapter (family `gemini`).

Gemini exports one JSON document with ordered ``messages`` plus metadata.
The whole file is one immutable snapshot; ordered messages map to typed
user/assistant events and unknown top-level fields are preserved by
reference (never silently dropped). ``native_payload_ref`` records the
source slice for unmodeled fields (D-07). Session-context fields are
restored from the document: ``model`` (top-level or per-message) and
``title`` from the first user content (first 120 chars); token/usage
fields on any message surface as ``usage`` events.
"""

from __future__ import annotations

import json
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
    CapabilityDescriptor,
    SourceArtifact,
    SourceArtifactSet,
    artifact_bytes_path,
)
from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventContractError,
    EventKind,
    EventRelation,
    FieldDisposition,
    FieldDispositionRecord,
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    Provenance,
    TypedEvent,
    make_event_id,
)

FAMILY = "gemini"
ADAPTER_VERSION = "1.1.0"
CONTRACT_VERSION = "2"

_COMPLETE = {
    FidelityDimension.SOURCE_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.ORDERING_CONFIDENCE: FidelityLevel.COMPLETE,
    FidelityDimension.RELATION_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.CONTENT_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.COMPACTION_VISIBILITY: FidelityLevel.COMPLETE,
    FidelityDimension.NATIVE_ID_STABILITY: FidelityLevel.COMPLETE,
}

# Flat token fields surfaced as a machine-parsable USAGE summary.
_FLAT_TOKEN_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read", "cache_read"),
    ("cache_write", "cache_write"),
)
_NESTED_TOKEN_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read", "cache_read"),
    ("cache_write", "cache_write"),
)


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


def _usage_tokens(message: dict) -> str | None:
    """Any token/usage fields on a message -> machine-parsable summary."""
    parts: list[str] = []
    usage = message.get("usage")
    if isinstance(usage, dict):
        for src, dst in _NESTED_TOKEN_FIELDS:
            if src in usage and usage[src] is not None:
                parts.append(f"{dst}={usage[src]}")
    for src, dst in _FLAT_TOKEN_FIELDS:
        if message.get(src) is not None:
            parts.append(f"{dst}={message[src]}")
    return " ".join(parts) if parts else None


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE, EventKind.REASONING,
            EventKind.USAGE, EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "single_json",
            "unmodeled_fields": "preserved_by_reference",
            "session_context": "model_and_title_from_document",
            "usage": "token_fields_as_usage_events",
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    if not (artifact.relative_path or "").lower().endswith(".json"):
        return False
    try:
        with artifact_bytes_path(artifact_root, artifact).open("r", encoding="utf-8") as h:
            doc = json.load(h)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get("messages"), list)


def _provenance(artifact: SourceArtifact, locator: str, *, session: str | None, native_id: str | None) -> Provenance:
    return Provenance(
        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
        native_locator=locator, native_session_id=session or None,
        native_event_id=native_id, contract_version=CONTRACT_VERSION,
    )


def _event(artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
           content=None, summary=None, fidelity=None, native_session=None,
           payload_ref=None, field_dispositions=()) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               native_id or locator, kind=kind, session_id=session_id),
        session_id=session_id, kind=kind,
        provenance=_provenance(artifact, locator, session=native_session, native_id=native_id),
        fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
        content=content, summary=summary,
        native_payload_ref=payload_ref,
        field_dispositions=field_dispositions,
    )


def _message_text(content) -> str | None:
    """User turns store ``content`` as ``[{text}]``; model turns store a string."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item:
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return str(content)


def _thoughts_text(thoughts) -> str | None:
    if thoughts is None:
        return None
    if isinstance(thoughts, str):
        return thoughts or None
    items = [thoughts] if isinstance(thoughts, dict) else thoughts
    if not isinstance(items, list):
        return str(thoughts)
    lines: list[str] = []
    for item in items:
        if isinstance(item, str) and item:
            lines.append(item)
            continue
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        description = str(item.get("description") or "").strip()
        line = " — ".join(part for part in (subject, description) if part)
        if line:
            lines.append(line)
    return "\n".join(lines) if lines else None


def _message_kind(message: dict):
    role = message.get("role") or message.get("type")
    if role in ("user", "human"):
        return EventKind.USER_MESSAGE
    if role in ("model", "assistant", "ai", "gemini"):
        return EventKind.ASSISTANT_MESSAGE
    return None


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one immutable Gemini JSON document into typed events."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    try:
        with (artifact_root / artifact.content_hash[:32]).open("r", encoding="utf-8") as h:
            doc = json.load(h)
    except (OSError, ValueError) as exc:
        raise EventContractError(f"{FAMILY} artifact is not a JSON document: {exc}") from exc

    if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
        raise EventContractError(f"{FAMILY} artifact has no ordered messages array")

    session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
    events: list[TypedEvent] = []
    warnings: list[str] = []
    native_session = doc.get("session_id") or doc.get("sessionId") or doc.get("id")

    events.append(_event(
        artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
        locator=f"{artifact.relative_path}#root", native_id=str(native_session or "root"),
        occurred_at=doc.get("created_at"),
        summary=str(doc.get("model") or "")[:128] or None,
        native_session=native_session,
    ))

    for index, message in enumerate(doc["messages"]):
        if not isinstance(message, dict):
            continue
        kind = _message_kind(message)
        locator = f"{artifact.relative_path}#messages[{index}]"
        native_id = message.get("id") or f"msg-{index}"
        text = _message_text(message.get("content"))
        if kind is None:
            native_type = message.get("type")
            if native_type is None:
                native_type = message.get("role")
            type_name = "<missing>" if native_type is None else str(native_type)
            events.append(_event(
                artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                locator=locator, native_id=native_id,
                occurred_at=message.get("timestamp"),
                content=text, summary=f"type={type_name}",
                fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                   RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                   CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                native_session=native_session, payload_ref=locator,
                field_dispositions=(FieldDispositionRecord(
                    field_name="type",
                    disposition=FieldDisposition.UNSUPPORTED,
                    reason=f"type={type_name}",
                ),),
            ))
        else:
            events.append(_event(
                artifact, session_id=session_id, kind=kind, locator=locator,
                native_id=native_id,
                occurred_at=message.get("timestamp"),
                content=text,
                native_session=native_session, payload_ref=locator,
            ))
        thoughts = _thoughts_text(message.get("thoughts")) if "thoughts" in message else None
        if thoughts:
            events.append(_event(
                artifact, session_id=session_id, kind=EventKind.REASONING,
                locator=f"{locator}#thoughts", native_id=f"{native_id}#thoughts",
                occurred_at=message.get("timestamp"),
                content=thoughts, summary=thoughts[:2048],
                native_session=native_session, payload_ref=locator,
                field_dispositions=(FieldDispositionRecord(
                    field_name="thoughts",
                    disposition=FieldDisposition.MAPPED,
                    reason="来源字段 thoughts",
                ),),
            ))

    # Usage events from any token/usage fields on individual messages.
    for index, message in enumerate(doc["messages"]):
        if not isinstance(message, dict):
            continue
        usage_summary = _usage_tokens(message)
        if not usage_summary:
            continue
        locator = f"{artifact.relative_path}#messages[{index}]"
        base_id = message.get("id") or f"msg-{index}"
        usage_occurred = message.get("timestamp") or doc.get("created_at")
        events.append(_event(
            artifact, session_id=session_id, kind=EventKind.USAGE,
            locator=f"{locator}#usage", native_id=f"{base_id}#usage",
            occurred_at=usage_occurred, summary=usage_summary,
            native_session=native_session, payload_ref=locator,
        ))
    unknown = sum(1 for e in events if e.kind is EventKind.UNKNOWN_NATIVE)

    # Session-context fields: model (top-level or first message) and title
    # from the first user content, truncated to 120 chars.
    model = doc.get("model")
    for message in doc["messages"]:
        if isinstance(message, dict) and message.get("model"):
            model = model or message.get("model")
            break
    title = None
    for message in doc["messages"]:
        if not isinstance(message, dict):
            continue
        if _message_kind(message) is not EventKind.USER_MESSAGE:
            continue
        raw = _message_text(message.get("content"))
        if raw:
            title = raw[:120]
            break

    sessions: list[AdaptedSession] = []
    if native_session:
        session_dispositions: list[FieldDispositionRecord] = []
        if not model:
            session_dispositions.append(FieldDispositionRecord(
                field_name="model", disposition=FieldDisposition.UNAVAILABLE,
                reason="no model field at top level or on messages",
            ))
        if not title:
            session_dispositions.append(FieldDispositionRecord(
                field_name="title", disposition=FieldDisposition.UNAVAILABLE,
                reason="no user message content to derive a title",
            ))
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=_provenance(artifact, f"{artifact.relative_path}#root",
                                   session=str(native_session), native_id=str(native_session)),
            fidelity=_fidelity(), native_session_id=str(native_session),
            model=model, title=title,
            field_dispositions=tuple(session_dispositions),
        ))

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events),
        fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE),
        sessions=tuple(sessions), relations=(), warnings=(
            (f"{unknown} unknown message role(s) preserved",) if unknown else ()
        ),
        field_dispositions=(FieldDispositionRecord(
            field_name="messages[*].usage", disposition=FieldDisposition.MAPPED,
            reason="token/usage fields mapped to usage events",
        ),) if _any_usage(doc["messages"]) else (),
    )


def _any_usage(messages) -> bool:
    """True if any message carries token/usage fields."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if _usage_tokens(message) is not None:
            return True
    return False
