"""Phase 62-02: Pi JSONL event-stream adapter (family `pi`).

Pi exports an independent JSONL event stream (62-RESEARCH format matrix)
where compaction is a typed event carrying summary, firstKeptEntryId
and tokensBefore - never a user message. We emit compaction_summary
events plus a compacted_range relation from the compaction to the last
event in the compacted range (the stream predecessor of the first kept
entry); when that boundary cannot be located we fall back to a
retained_from relation. Session-context fields (cwd, title, git_branch,
model, stop_reason) are restored from the native conversation/session
record when present; the session model is also recovered from the
model_change records (modelId) and title/stop_reason are derived from the
first user message and the last assistant stopReason when the
conversation record leaves them unset. Structured message content blocks
are surfaced as their own events: thinking -> reasoning, toolCall ->
tool_call, and toolResult role messages -> tool_result, so real Pi exports
no longer collapse into unknown_native. Any token/usage fields surface as
usage events using standard input_tokens/output_tokens keys (the Pi
message.usage input/output/cacheRead/cacheWrite shape included); a
compaction tokensBefore is kept as tokens_before only on records that
carry no input/output token pair. Missing native IDs, unknown kinds and
malformed lines fail closed or report bounded partial fidelity.
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
    CapabilityDescriptor,
    SourceArtifact,
    SourceArtifactSet,
    artifact_bytes_path,
)
from personal_knowledge.adapters.conversation_sources.jsonl_stream import (
    iter_jsonl_lines,
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
    RelationKind,
    TypedEvent,
    make_event_id,
)

FAMILY = "pi"
ADAPTER_VERSION = "1.3.0"
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
    ("tokensBefore", "tokens_before"),
    ("tokens_before", "tokens_before"),
)
_NESTED_TOKEN_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read", "cache_read"),
    ("cache_write", "cache_write"),
)
_PI_MESSAGE_TOKEN_FIELDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cacheRead", "cache_read"),
    ("cacheWrite", "cache_write"),
)


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


def _usage_tokens(record: dict) -> str | None:
    """Any token/usage fields on a record -> machine-parsable summary or None.

    Surfaces the flat compaction `tokensBefore`/`tokens_before` field, the
    fixture-shaped nested `record["usage"]` dict, and the real Pi
    `message.usage` object (keys `input`/`output`/`cacheRead`/`cacheWrite`).
    Standard keys are used whenever input/output tokens are present; a
    `tokens_before` value only appears on records that carry no such pair.
    Fields are deduplicated by target key, preserving first-seen order.
    """
    fields: dict[str, str] = {}

    def _collect(src: str, dst: str, container) -> None:
        if src in container and container[src] is not None:
            fields.setdefault(dst, str(container[src]))

    usage = record.get("usage")
    if isinstance(usage, dict):
        for src, dst in _NESTED_TOKEN_FIELDS:
            _collect(src, dst, usage)
    message = record.get("message")
    if isinstance(message, dict):
        message_usage = message.get("usage")
        if isinstance(message_usage, dict):
            for src, dst in _PI_MESSAGE_TOKEN_FIELDS:
                _collect(src, dst, message_usage)

    # tokens_before is only meaningful absent an input/output token pair.
    has_counter = any(k in fields for k in ("input_tokens", "output_tokens"))
    if not has_counter:
        for src, dst in _FLAT_TOKEN_FIELDS:
            if record.get(src) is not None:
                fields.setdefault(dst, str(record[src]))
    return " ".join(f"{k}={v}" for k, v in fields.items()) if fields else None


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE, EventKind.SYSTEM_MESSAGE,
            EventKind.REASONING, EventKind.TOOL_CALL, EventKind.TOOL_RESULT,
            EventKind.COMPACTION_SUMMARY, EventKind.USAGE,
            EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(
            RelationKind.COMPACTED_RANGE, RelationKind.RETAINED_FROM,
        ),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "jsonl_event_stream",
            "compaction": "independent_record_with_range_metadata",
            "session_context": "cwd_model_title_stop_reason_from_records",
            "usage": "token_fields_as_usage_events_standard_keys",
            "content_blocks": "thinking_tool_calls_surfaced",
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    """Probe the first non-blank line for a Pi conversation record."""
    if not (artifact.relative_path or "").lower().endswith(".jsonl"):
        return False
    try:
        with artifact_bytes_path(artifact_root, artifact).open("r", encoding="utf-8") as h:
            for raw in h:
                line = raw.strip()
                if not line:
                    continue
                return '"type"' in line and (
                    '"conversation"' in line or '"session"' in line
                )
    except OSError:
        return False
    return False

def _event(artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
           content=None, summary=None, fidelity=None, native_session=None,
           locator_hint=None) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               native_id or locator, kind=kind, session_id=session_id,
                               native_locator=locator_hint),
        session_id=session_id, kind=kind,
        provenance=Provenance(
            artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
            native_locator=locator, native_session_id=native_session or None,
            native_event_id=native_id, contract_version=CONTRACT_VERSION,
        ),
        fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
        content=content, summary=summary,
    )


def _adapt_record(record: dict, artifact, *, session_id, locator, native_session) -> list[TypedEvent]:
    """Adapt one Pi record into one or more typed events.

    The first returned event is the primary event for ordering/compaction
    purposes; structured assistant content blocks (thinking/toolCall) are
    emitted as additional reasoning/tool_call events so real Pi exports are
    not collapsed into unknown_native.
    """
    kind = record.get("type")
    ts = record.get("timestamp")
    mid = record.get("message_id")
    if kind == "conversation":
        return [_event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                      locator=locator, native_id=record.get("id"), occurred_at=ts,
                      summary=str(record.get("title") or "")[:256] or None, native_session=native_session)]
    if kind == "session":
        return [_event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                      locator=locator, native_id=record.get("id"), occurred_at=ts,
                      native_session=native_session)]
    if kind == "message":
        return _adapt_message(record, artifact, session_id=session_id, locator=locator,
                              native_session=native_session)
    if kind == "user_message":
        raw_content = record.get("content")
        return [_event(artifact, session_id=session_id, kind=EventKind.USER_MESSAGE,
                      locator=locator, native_id=mid, occurred_at=ts,
                      content=None if raw_content is None else str(raw_content),
                      native_session=native_session)]
    if kind == "assistant_message":
        raw_content = record.get("content")
        return [_event(artifact, session_id=session_id, kind=EventKind.ASSISTANT_MESSAGE,
                      locator=locator, native_id=mid, occurred_at=ts,
                      content=None if raw_content is None else str(raw_content),
                      native_session=native_session)]
    if kind == "compaction":
        return [_event(artifact, session_id=session_id, kind=EventKind.COMPACTION_SUMMARY,
                      locator=locator, native_id=mid or record.get("id"), occurred_at=ts,
                      summary=str(record.get("summary") or "")[:2048] or None, native_session=native_session)]
    if kind == "model_change":
        # Session-model transition: surface as a lifecycle event carrying the
        # model id, and let the session record pick it up as the active model.
        model_id = record.get("modelId")
        return [_event(
            artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
            locator=locator, native_id=record.get("id"), occurred_at=ts,
            summary=str(model_id) if model_id else None,
            fidelity=_fidelity(
                STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
            ), native_session=native_session,
        )]
    if kind == "thinking_level_change":
        # A reasoning-depth control signal -> a reasoning event carrying the
        # level so it is not dropped or misclassified as a lifecycle change.
        level = record.get("thinkingLevel")
        return [_event(
            artifact, session_id=session_id, kind=EventKind.REASONING,
            locator=locator, native_id=record.get("id"), occurred_at=ts,
            summary=str(level) if level else None,
            fidelity=_fidelity(
                STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
            ), native_session=native_session,
        )]
    return [_event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                  locator=locator, native_id=mid, occurred_at=ts,
                  fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                     RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                     CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                  native_session=native_session)]


def _adapt_message(record, artifact, *, session_id, locator, native_session) -> list[TypedEvent]:
    """Adapt a Pi `message` record into its primary event plus block events.

    The primary event follows the message role (user/assistant/system/
    toolResult). For assistant messages the structured content blocks are
    additionally surfaced: `thinking` blocks become reasoning events and
    `toolCall` blocks become tool_call events, so reasoning and tool use are
    no longer buried inside the assistant text. toolResult role messages
    (the bulk of what used to collapse into unknown_native) become
    tool_result events.
    """
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    role = message.get("role")
    if role == "toolResult":
        tool_name = message.get("toolName")
        result_event = _event(
            artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
            locator=locator, native_id=record.get("id"), occurred_at=record.get("timestamp"),
            content=_message_text(message.get("content")),
            summary=f"tool_result {tool_name}" if tool_name else None,
            fidelity=_fidelity(
                STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
            ), native_session=native_session,
        )
        return [result_event]
    if role == "user":
        message_kind = EventKind.USER_MESSAGE
    elif role == "assistant":
        message_kind = EventKind.ASSISTANT_MESSAGE
    elif role == "system":
        message_kind = EventKind.SYSTEM_MESSAGE
    else:
        message_kind = EventKind.UNKNOWN_NATIVE
    primary = _event(
        artifact, session_id=session_id, kind=message_kind,
        locator=locator, native_id=record.get("id"), occurred_at=record.get("timestamp"),
        content=(_message_text(message.get("content"))
                 if message_kind is not EventKind.UNKNOWN_NATIVE else None),
        fidelity=(None if message_kind is not EventKind.UNKNOWN_NATIVE else _fidelity(
            STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
            RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
        )),
        native_session=native_session,
    )
    blocks = message.get("content")
    if message_kind not in (EventKind.ASSISTANT_MESSAGE, EventKind.USER_MESSAGE) or not isinstance(blocks, list):
        return [primary]
    block_events: list[TypedEvent] = [primary]
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        block_locator = f"{locator}#block[{index}]"
        if block_type == "thinking":
            thinking = block.get("thinking")
            if thinking is None:
                continue
            block_events.append(_event(
                artifact, session_id=session_id, kind=EventKind.REASONING,
                locator=block_locator, native_id=record.get("id"), occurred_at=record.get("timestamp"),
                content=str(thinking)[:2048] or None,
                summary=str(thinking)[:256] or None,
                fidelity=_fidelity(
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                ), native_session=native_session, locator_hint=block_locator,
            ))
        elif block_type == "toolCall":
            name = block.get("name")
            tool_text = _tool_call_text(block)
            block_events.append(_event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=block_locator, native_id=block.get("id") or record.get("id"),
                occurred_at=record.get("timestamp"), content=tool_text,
                summary=f"tool_call {name}" if name else "tool_call",
                fidelity=_fidelity(
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                ), native_session=native_session, locator_hint=block_locator,
            ))
    return block_events


def _tool_call_text(block: dict) -> str | None:
    """A readable rendering of a toolCall content block (name + arguments)."""
    name = block.get("name")
    text = str(name) if name else "tool_call"
    args = block.get("arguments")
    if args is not None:
        rendered = str(args)
        text = f"{text} {rendered[:512]}"
    return text or None


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one immutable Pi export into typed events/relations."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    records = list(iter_jsonl_lines(artifact_root / artifact.content_hash[:32]))

    session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    field_dispositions: list[FieldDispositionRecord] = []
    native_session = next(
        (r.get("conversation_id") for r in records if r.get("conversation_id")),
        next((r.get("id") for r in records if r.get("type") == "session"), None),
    )
    by_mid: dict[str, TypedEvent] = {}
    aligned: list[tuple[dict, TypedEvent]] = []

    for lineno, record in enumerate(records, start=1):
        adapted = _adapt_record(record, artifact, session_id=session_id,
                                locator=f"{artifact.relative_path}#L{lineno}", native_session=native_session)
        if not adapted:
            continue
        # The first event is the primary event for ordering/compaction; the
        # remaining events are surfaced content blocks (reasoning/tool calls).
        primary, *block_events = adapted
        events.append(primary)
        events.extend(block_events)
        aligned.append((record, primary))
        if record.get("message_id"):
            by_mid[record["message_id"]] = primary

    # Usage events from any token/usage fields on individual records.
    for record, ev in aligned:
        usage_summary = _usage_tokens(record)
        if not usage_summary:
            continue
        base_id = ev.provenance.native_event_id or ev.provenance.native_locator
        events.append(_event(
            artifact, session_id=session_id, kind=EventKind.USAGE,
            locator=f"{ev.provenance.native_locator}#usage",
            native_id=f"{base_id}#usage", occurred_at=ev.occurred_at,
            summary=usage_summary, native_session=native_session,
        ))
    event_order = [ev for _, ev in aligned]

    for record, ev in aligned:
        if record.get("type") != "compaction":
            continue
        first_kept = record.get("firstKeptEntryId")
        target = by_mid.get(first_kept) if first_kept else None
        if target is None:
            # Boundary cannot be located precisely: fall back to the nearest
            # preceding event as the retained source and say so explicitly.
            idx = event_order.index(ev)
            predecessor = event_order[idx - 1] if idx > 0 else None
            if predecessor is not None:
                relations.append(EventRelation(
                    relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                              f"rel-retain:{ev.event_id}"),
                    source_event_id=ev.event_id, target_event_id=predecessor.event_id,
                    relation_kind=RelationKind.RETAINED_FROM,
                ))
            else:
                warnings.append("compaction without a locatable bound (partial range)")
            field_dispositions.append(FieldDispositionRecord(
                field_name="firstKeptEntryId",
                disposition=FieldDisposition.UNAVAILABLE,
                reason=f"kept boundary {first_kept!r} not locatable; compacted range exact",
            ))
            continue
        # The last event in the compacted range is the stream predecessor of
        # the first kept entry.
        idx = event_order.index(target)
        if idx > 0:
            last_compacted = event_order[idx - 1]
            relations.append(EventRelation(
                relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-compact:{ev.event_id}"),
                source_event_id=ev.event_id, target_event_id=last_compacted.event_id,
                relation_kind=RelationKind.COMPACTED_RANGE,
            ))
        else:
            # Kept boundary is the first event -> nothing compacted earlier;
            # keep an explicit retained-from so the compaction stays connected.
            relations.append(EventRelation(
                relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-retain:{ev.event_id}"),
                source_event_id=ev.event_id, target_event_id=target.event_id,
                relation_kind=RelationKind.RETAINED_FROM,
            ))
    unknown = sum(1 for e in events if e.kind is EventKind.UNKNOWN_NATIVE)
    if unknown:
        warnings.append(f"{unknown} unknown native record(s) preserved")

    # Session-context fields restored from the native records: cwd/branch from
    # the conversation/session record, model from the (last) model_change
    # record, and title/stop_reason derived from the first user message and
    # the last assistant stopReason when the conversation record leaves them
    # unset.
    context = next(
        (r for r in records if r.get("type") == "conversation"),
        next((r for r in records if r.get("type") == "session"), None),
    ) or {}
    # Session-context timestamps: started_at prefers the native conversation /
    # session record timestamp; ended_at is the last event record's timestamp.
    session_ts = context.get("timestamp") or context.get("created_at")
    event_timestamps = [rec.get("timestamp") for rec in records if rec.get("timestamp")]
    model = context.get("model")
    for record in records:
        if record.get("type") == "model_change" and record.get("modelId"):
            model = record["modelId"]  # last model change is the active model
    title = context.get("title")
    stop_reason = context.get("stop_reason")
    if not title:
        for record in records:
            if record.get("type") != "message":
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if message.get("role") != "user":
                continue
            raw = _message_text(message.get("content"))
            if raw:
                title = str(raw)[:256] or None
                break
    if not stop_reason:
        for record in records:
            if record.get("type") != "message":
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if message.get("role") == "assistant" and message.get("stopReason"):
                stop_reason = str(message.get("stopReason"))  # last assistant stop

    sessions: list[AdaptedSession] = []
    if native_session:
        session_dispositions: list[FieldDispositionRecord] = []
        if not context.get("cwd"):
            session_dispositions.append(FieldDispositionRecord(
                field_name="cwd", disposition=FieldDisposition.UNAVAILABLE,
                reason="no cwd field in conversation/session record",
            ))
        if not model:
            session_dispositions.append(FieldDispositionRecord(
                field_name="model", disposition=FieldDisposition.UNAVAILABLE,
                reason="no model id on the conversation/session or model_change records",
            ))
        if not title:
            session_dispositions.append(FieldDispositionRecord(
                field_name="title", disposition=FieldDisposition.UNAVAILABLE,
                reason="no conversation title or user message content to derive one",
            ))
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=f"{artifact.relative_path}#session",
                native_session_id=native_session, native_event_id=native_session,
                contract_version=CONTRACT_VERSION,
            ),
            fidelity=_fidelity(), native_session_id=native_session,
            started_at=session_ts or (event_timestamps[0] if event_timestamps else None),
            ended_at=event_timestamps[-1] if event_timestamps else None,
            cwd=context.get("cwd"),
            git_branch=context.get("git_branch"),
            model=model,
            title=title,
            stop_reason=stop_reason,
            field_dispositions=tuple(session_dispositions),
        ))

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events),
        fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE),
        sessions=tuple(sessions), relations=tuple(relations),
        field_dispositions=tuple(field_dispositions),
        warnings=tuple(warnings),
    )


def _message_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values: list[str] = []
        saw_text = False
        for item in content:
            if isinstance(item, dict):
                if "text" in item and item.get("text") is not None:
                    value = item.get("text")
                else:
                    value = item.get("content")
                if value is not None:
                    saw_text = True
                    values.append(str(value))
        return " ".join(values) if saw_text else None
    return None
