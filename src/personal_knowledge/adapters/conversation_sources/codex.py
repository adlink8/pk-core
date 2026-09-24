"""Phase 62-02: Codex JSONL event-stream adapter (family ``codex``).

Maps Codex's top-level JSONL event stream into typed events, relations and
fidelity (62-RESEARCH format matrix). Two native shapes are supported:

  - synthetic: ``{"type": "session_meta", "session_id": ..., ...}`` with flat
    fields (contract fixtures);
  - real export: fields nested under a ``payload`` object
    (``session_meta.payload.id``, ``turn_context.payload.turn_id``,
    ``response_item.payload.{role, content:[{type,text}]}``) and
    ``event_msg.payload.type`` loop hints.

Native turn IDs and call/result links survive as typed relations; compaction
boundaries become compaction events, never user messages. Unknown record
kinds stay ``unknown_native``; a missing native session never fabricates a
complete-looking session (D-04/D-11).
"""

from __future__ import annotations

from dataclasses import replace
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
    FidelityDimension,
    FieldDisposition,
    FieldDispositionRecord,
    FidelityLevel,
    FidelityProfile,
    Provenance,
    RelationKind,
    TypedEvent,
    make_event_id,
)

FAMILY = "codex"
ADAPTER_VERSION = "1.5.0"
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

# event_msg payload loop hints that act as turn boundaries. task_started and
# agent_message are classified explicitly (loop boundary / assistant message), so
# only the older turn hints remain here.
_LOOP_HINTS = ("turn_started", "agent_turn_started")


# Cap for full tool-call input / tool-result output carried as event content.
# Tool outputs routinely run to thousands of lines; storing them in full would
# bloat the dataset. We keep a generous ceiling (far above the old 2048-char
# summary truncation) and flag truncation explicitly via a field disposition.
_CONTENT_CAP = 100_000
_TOOL_OUTPUT_REASON = "tool output truncated; full text exceeds content cap"
_REASONING_ENCRYPTED_REASON = "reasoning content is encrypted; plaintext not available"


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


def _inner(record: dict) -> dict:
    """Real Codex exports nest fields under ``payload``; synthetic fixtures
    are flat. Both shapes share the top-level ``type``/``timestamp``."""
    return record.get("payload") if isinstance(record.get("payload"), dict) else record


def _text(record: dict) -> str | None:
    """Text from a content block list (real shape) or a flat string."""
    content = _inner(record).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        saw_text = False
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                "text", "input_text", "output_text",
            ):
                saw_text = True
                raw = block.get("text")
                parts.append("" if raw is None else str(raw))
        return " ".join(parts) if saw_text else None
    return None


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE,
            EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE,
            EventKind.DEVELOPER_MESSAGE,
            EventKind.REASONING,
            EventKind.TOOL_CALL,
            EventKind.TOOL_RESULT,
            EventKind.COMPACTION_SUMMARY,
            EventKind.TURN_BOUNDARY,
            EventKind.LOOP_BOUNDARY,
            EventKind.SUBAGENT_BOUNDARY,
            EventKind.USAGE,
            EventKind.FILE_CONTEXT,
            EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(
            RelationKind.CALL_RESULT,
            RelationKind.TURN_MEMBERSHIP,
        ),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "jsonl_event_stream",
            "nested_payload": "true",
            "compaction": "top_level_context_compacted",
            "call_result": "call_id_pairing",
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    """Probe the first non-blank line for the opening ``session_meta`` event."""
    if not (artifact.relative_path or "").lower().endswith(".jsonl"):
        return False
    try:
        with artifact_bytes_path(artifact_root, artifact).open("r", encoding="utf-8") as h:
            for raw in h:
                line = raw.strip()
                if not line:
                    continue
                return '"type"' in line and '"session_meta"' in line
    except OSError:
        return False
    return False


def _provenance(artifact: SourceArtifact, locator: str, *, session: str | None, native_id: str | None) -> Provenance:
    return Provenance(
        artifact_id=artifact.artifact_id,
        artifact_hash=artifact.content_hash,
        native_locator=locator,
        native_session_id=session or None,
        native_event_id=native_id,
        contract_version=CONTRACT_VERSION,
    )


def _event(artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
           content=None, summary=None, fidelity=None, native_session=None,
           field_dispositions=(), ordinal=None) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(
            FAMILY, artifact.artifact_id, CONTRACT_VERSION,
            native_id or locator, kind=kind, session_id=session_id,
            native_locator=locator,
        ),
        session_id=session_id,
        kind=kind,
        provenance=_provenance(artifact, locator, session=native_session or "", native_id=native_id),
        fidelity=fidelity or _fidelity(),
        occurred_at=occurred_at,
        ordinal=ordinal,
        content=content,
        summary=summary,
        field_dispositions=tuple(field_dispositions),
    )


def _adapt_record(record: dict, artifact, *, session_id, locator, ordinal=None) -> TypedEvent | None:
    """Map one Codex record to a typed event; unknown kinds stay unknown_native.

    ``ordinal`` is the record's 1-based JSONL line number: the adapter's global
    ordering signal. JSONL records are ordered by file line and each maps to at
    most one typed event, so the line number is a total, lossless order over the
    adapted event stream. It is stamped onto the emitted event so that
    ORDERING_CONFIDENCE=complete is backed by real ordinal values rather than a
    bare claim (the source order genuinely exists, it just was never carried
    into the typed event before).
    """
    ev = _adapt_record_impl(record, artifact, session_id=session_id, locator=locator)
    if ev is None:
        return None
    return replace(ev, ordinal=ordinal)


def _adapt_record_impl(record: dict, artifact, *, session_id, locator) -> TypedEvent | None:
    """Map one Codex record to a typed event; unknown kinds stay unknown_native."""
    kind = record.get("type")
    inner = _inner(record)
    ts = record.get("timestamp") or inner.get("timestamp")
    sid = record.get("session_id") or inner.get("session_id") or inner.get("id")
    if kind == "session_meta":
        return _event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                      locator=locator, native_id=inner.get("id"), occurred_at=ts,
                      native_session=sid)
    if kind == "turn_context":
        return _event(artifact, session_id=session_id, kind=EventKind.TURN_BOUNDARY,
                      locator=locator, native_id=inner.get("turn_id"), occurred_at=ts,
                      summary=str(inner.get("prompt") or "")[:512] or None, native_session=sid)
    if kind == "response_item":
        item_type = inner.get("type")
        role = inner.get("role")
        ev = EventKind.ASSISTANT_MESSAGE if role == "assistant" else (
            EventKind.USER_MESSAGE if role == "user" else (
                EventKind.DEVELOPER_MESSAGE if role == "developer" else None))
        if ev is not None:
            return _event(artifact, session_id=session_id, kind=ev, locator=locator,
                          native_id=inner.get("id") or inner.get("item_id"), occurred_at=ts,
                          content=_text(record), native_session=sid)
        if item_type in ("function_call", "custom_tool_call", "tool_search_call"):
            # content carries the full tool input (arguments for
            # function_call, input for custom_tool_call / tool_search_call);
            # summary carries the tool name so TOOL_CALL rows stay readable.
            name = str(inner.get("name") or item_type)
            content, dispositions = _tool_input_text(inner)
            return _event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=locator, native_id=inner.get("call_id") or inner.get("id"),
                occurred_at=ts, content=content, summary=name[:256],
                field_dispositions=dispositions,
                native_session=sid,
            )
        if item_type in (
            "function_call_output", "custom_tool_call_output", "tool_search_output"
        ):
            call_id = inner.get("call_id") or inner.get("id")
            content, dispositions = _tool_result_text(inner)
            return _event(
                artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
                locator=locator, native_id=f"{call_id}#output",
                occurred_at=ts, content=content,
                summary=(content or "")[:2048] or None,
                field_dispositions=dispositions,
                native_session=sid,
            )
        if item_type == "reasoning":
            # F11a: plaintext reasoning rides in content; encrypted-only
            # content stays truly unavailable (never complete with content None).
            content, dispositions, availability = _reasoning_content(inner)
            return _event(
                artifact, session_id=session_id, kind=EventKind.REASONING,
                locator=locator, native_id=inner.get("id"), occurred_at=ts,
                content=content,
                summary=_summary_text(inner.get("summary")) or _payload_text(inner),
                fidelity=_fidelity(CONTENT_AVAILABILITY=availability),
                field_dispositions=dispositions,
                native_session=sid,
            )
        if item_type == "agent_message":
            # assistant-authored message surfaced as a response_item item.
            content = _text(record)
            dispositions = ()
            if content is None:
                content, dispositions = _payload_text_capped(inner, cap=_CONTENT_CAP)
            return _event(
                artifact, session_id=session_id, kind=EventKind.ASSISTANT_MESSAGE,
                locator=locator, native_id=inner.get("id") or inner.get("item_id"),
                occurred_at=ts, content=content,
                field_dispositions=dispositions, native_session=sid,
            )
        if item_type == "web_search_call":
            query = ""
            action = inner.get("action")
            if isinstance(action, dict):
                query = str(action.get("query") or "")[:256]
            return _event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=locator, native_id=inner.get("call_id") or inner.get("id"),
                occurred_at=ts, summary=query or "web_search_call",
                native_session=sid,
            )
    if kind == "event_msg":
        # loop hints are first-class episode hints; other event messages stay
        # unknown native records rather than guessed content.
        hint = inner.get("type")
        if hint == "context_compacted":
            return _event(
                artifact, session_id=session_id,
                kind=EventKind.COMPACTION_SUMMARY, locator=locator,
                native_id=inner.get("turn_id") or f"compact:{locator}",
                occurred_at=ts, summary=str(inner.get("summary") or "")[:2048] or None,
                native_session=sid,
            )
        if hint == "user_message":
            raw_message = inner.get("message")
            return _event(
                artifact, session_id=session_id, kind=EventKind.USER_MESSAGE,
                locator=locator, native_id=inner.get("id"), occurred_at=ts,
                content=None if raw_message is None else str(raw_message),
                native_session=sid,
            )
        if hint == "token_count":
            usage = _token_count_usage(inner)
            if usage:
                return _event(
                    artifact, session_id=session_id, kind=EventKind.USAGE,
                    locator=locator, native_id=inner.get("turn_id"), occurred_at=ts,
                    summary=usage,
                    fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                    native_session=sid,
                )
            return _event(artifact, session_id=session_id,
                          kind=EventKind.UNKNOWN_NATIVE, locator=locator,
                          native_id=inner.get("turn_id") or inner.get("type"),
                          occurred_at=ts, fidelity=_fidelity(
                              STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                              RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                              CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                          ), native_session=sid)
        if hint == "agent_message":
            # assistant-authored message delivered as an event message.
            content, dispositions = _payload_text_capped(inner, cap=_CONTENT_CAP)
            if content is None:
                content = _summary_text(inner.get("summary"))
            return _event(
                artifact, session_id=session_id, kind=EventKind.ASSISTANT_MESSAGE,
                locator=locator, native_id=inner.get("turn_id") or inner.get("id") or hint,
                occurred_at=ts,
                content=content,
                field_dispositions=dispositions, native_session=sid,
            )
        if hint in ("task_started", "task_complete"):
            # full agent-loop episodes frame turns; surface as loop boundaries.
            summary = (str(inner.get("last_agent_message") or "")[:256]
                       or str(inner.get("message") or "")[:256] or None)
            return _event(
                artifact, session_id=session_id, kind=EventKind.LOOP_BOUNDARY,
                locator=locator, native_id=inner.get("turn_id") or inner.get("id") or hint,
                occurred_at=ts, summary=summary, native_session=sid,
            )
        if hint in ("agent_reasoning", "reasoning"):
            # streamed reasoning text on the event stream; content carries the
            # plaintext part when readable, encrypted-only reasoning stays truly
            # unavailable (never complete with content None).
            content, dispositions, availability = _reasoning_content(inner)
            return _event(
                artifact, session_id=session_id, kind=EventKind.REASONING,
                locator=locator, native_id=inner.get("id") or inner.get("turn_id") or hint,
                occurred_at=ts,
                content=content,
                summary=_summary_text(inner.get("summary")) or _payload_text(inner),
                fidelity=_fidelity(CONTENT_AVAILABILITY=availability),
                field_dispositions=dispositions,
                native_session=sid,
            )
        if hint in (
            "exec_command_end", "web_search_end", "patch_apply_end",
            "mcp_tool_call_end",
        ):
            # tool executions surfaced as event messages; carry a call id so
            # the output can pair with its call and carry the output string.
            call_id = inner.get("call_id") or hint
            content, dispositions = _tool_result_text(inner)
            if content is None:
                content = _payload_text(inner)
            if content is None:
                content = str(inner.get("stderr") or "")[:2048] or None
            return _event(
                artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
                locator=locator, native_id=f"{call_id}#output",
                occurred_at=ts, content=content,
                summary=(content or "")[:2048] or None,
                field_dispositions=dispositions,
                native_session=sid,
            )
        if hint == "turn_aborted":
            # an interrupted/aborted turn is still a turn boundary.
            return _event(
                artifact, session_id=session_id, kind=EventKind.TURN_BOUNDARY,
                locator=locator, native_id=inner.get("turn_id") or hint,
                occurred_at=ts, summary=str(inner.get("reason") or "")[:256] or None,
                native_session=sid,
            )
        if hint in (
            "collab_agent_spawn_end", "collab_close_end", "sub_agent_activity",
            "collab_agent_interaction_end", "collab_waiting_end",
        ):
            # agent collaboration lifecycle events signal sub-agent boundaries.
            name = (inner.get("new_agent_nickname") or inner.get("receiver_agent_nickname")
                    or inner.get("new_agent_role") or hint)
            return _event(
                artifact, session_id=session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                locator=locator,
                native_id=inner.get("call_id") or inner.get("new_thread_id") or hint,
                occurred_at=ts, summary=str(name)[:256] or None, native_session=sid,
            )
        if hint == "thread_settings_applied":
            # thread/session configuration change is file/context signal.
            return _event(
                artifact, session_id=session_id, kind=EventKind.FILE_CONTEXT,
                locator=locator, native_id=inner.get("turn_id") or inner.get("id") or hint,
                occurred_at=ts, summary="thread_settings_applied",
                fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                native_session=sid,
            )
        if hint == "item_completed":
            # a scoped work item (e.g. a plan item) finished within the loop.
            item = inner.get("item")
            item_text = item.get("text") if isinstance(item, dict) else None
            return _event(
                artifact, session_id=session_id, kind=EventKind.LOOP_BOUNDARY,
                locator=locator, native_id=inner.get("turn_id") or inner.get("id") or hint,
                occurred_at=ts, summary=str(item_text or "")[:256] or None,
                native_session=sid,
            )
        if hint == "error":
            # keep the record as unknown_native but surface the error message.
            err = str(inner.get("message") or inner.get("error") or "")[:2048] or None
            return _event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                          locator=locator, native_id=inner.get("turn_id") or hint,
                          occurred_at=ts, summary=err, native_session=sid)
        if hint in _LOOP_HINTS:
            return _event(artifact, session_id=session_id, kind=EventKind.TURN_BOUNDARY,
                          locator=locator, native_id=inner.get("turn_id") or hint,
                          occurred_at=ts, summary=str(inner.get("message") or "")[:256] or None,
                          native_session=sid)
        return _event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                      locator=locator, native_id=inner.get("turn_id") or hint or inner.get("id"),
                      occurred_at=ts, fidelity=_fidelity(
                          STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                          RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                          CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                      ), native_session=sid)
    if kind == "function_call":
        content, dispositions = _tool_input_text(inner)
        return _event(artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                      locator=locator, native_id=inner.get("call_id") or inner.get("id"),
                      occurred_at=ts, content=content,
                      summary=str(inner.get("name") or "")[:256] or None,
                      field_dispositions=dispositions,
                      native_session=sid)
    if kind == "function_call_output":
        # Codex shares call_id between call and output; make_event_id omits
        # kind when a native id is present, so disambiguate the output's id.
        call_id = inner.get("call_id") or inner.get("id")
        content, dispositions = _tool_result_text(inner)
        return _event(artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
                      locator=locator, native_id=f"{call_id}#output",
                      occurred_at=ts, content=content,
                      summary=(content or "")[:2048] or None,
                      field_dispositions=dispositions,
                      native_session=sid)
    if kind == "context_compacted":
        return _event(artifact, session_id=session_id, kind=EventKind.COMPACTION_SUMMARY,
                      locator=locator, native_id=inner.get("turn_id"), occurred_at=ts,
                      summary=str(inner.get("summary") or "")[:2048] or None, native_session=sid)
    if kind == "world_state":
        return _event(
            artifact, session_id=session_id, kind=EventKind.FILE_CONTEXT,
            locator=locator, native_id=inner.get("id"), occurred_at=ts,
            fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
            native_session=sid,
        )
    return _event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                  locator=locator, native_id=record.get("id") or inner.get("id"),
                  occurred_at=ts, fidelity=_fidelity(
                      STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                      RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                      CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                  ), native_session=sid)


def _summary_text(value) -> str | None:
    if isinstance(value, str):
        return value[:2048] or None
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("summary")
                if text:
                    parts.append(str(text))
        return " ".join(parts)[:2048] or None
    if isinstance(value, dict):
        for key in ("text", "summary", "message"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text[:2048]
        return None
    return None


def _payload_text(inner: dict, *, keys=("message", "text")) -> str | None:
    """Plain event-msg text from a short field name or a content-block list.

    Real Codex event messages carry authored/streamed text on a single field
    (payload.message for agent_message, payload.text for agent_reasoning).
    Some shapes nest content as a list of input/output text blocks; that is
    handled here too. Returns a bounded string or None.
    """
    text, _ = _payload_text_capped(inner, keys=keys, cap=2048)
    return text


def _payload_text_capped(
    inner: dict, *, keys=("message", "text"), cap: int,
) -> tuple[str | None, tuple]:
    """Round-4 fix: full-fidelity event-msg text with an explicit cap.

    agent_message bodies streamed on the event stream were silently truncated
    at 2048; message content now uses the generous content cap and flags
    truncation via a field disposition instead.
    """
    for key in keys:
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            if len(value) <= cap:
                return value, ()
            return value[:cap], (FieldDispositionRecord(
                field_name=key,
                disposition=FieldDisposition.MAPPED,
                reason="message truncated; full text exceeds content cap",
            ),)
        if isinstance(value, list):
            parts = []
            for block in value:
                if isinstance(block, dict) and block.get("type") in (
                    "text", "input_text", "output_text",
                ):
                    text = block.get("text")
                    if text:
                        parts.append(str(text))
            if parts:
                joined = " ".join(parts)
                if len(joined) <= cap:
                    return joined, ()
                return joined[:cap], (FieldDispositionRecord(
                    field_name=key,
                    disposition=FieldDisposition.MAPPED,
                    reason="message truncated; full text exceeds content cap",
                ),)
    return None, ()


def _tool_input_text(inner: dict) -> tuple[str | None, tuple]:
    """Full tool-call input (the script/arguments) carried as event content.\n\n    function_call puts its arguments on payload.arguments (a JSON string);
    custom_tool_call / tool_search_call put theirs on payload.input (string
    or content-block list). Returns (content, dispositions) where content is
    capped at _CONTENT_CAP and truncation is recorded via a disposition.
    """
    raw = inner.get("arguments")
    key = "arguments"
    if raw is None:
        raw = inner.get("input")
        key = "input"
    text = _coerce_text(raw)
    if text is None:
        return None, ()
    capped, truncated = _capped(text)
    dispositions = ()
    if truncated:
        dispositions = (
            FieldDispositionRecord(
                field_name=key,
                disposition=FieldDisposition.MAPPED,
                reason="tool input truncated; full text exceeds content cap",
            ),
        )
    return capped, dispositions


def _tool_result_text(inner: dict) -> tuple[str | None, tuple]:
    """Full tool-result output carried as event content.\n\n    Response-item outputs sit on payload.output (string or content-block
    list); event-msg tool-execution records carry aggregated_output / stdout.
    Returns (content, dispositions) where content is capped at _CONTENT_CAP
    and truncation is flagged via a field disposition.
    """
    for key in ("output", "aggregated_output", "stdout"):
        value = inner.get(key)
        text = _coerce_text(value)
        if text is not None:
            capped, truncated = _capped(text)
            dispositions = ()
            if truncated:
                dispositions = (
                    FieldDispositionRecord(
                        field_name=key,
                        disposition=FieldDisposition.MAPPED,
                        reason=_TOOL_OUTPUT_REASON,
                    ),
                )
            return capped, dispositions
    return None, ()


def _coerce_text(value) -> str | None:
    """Coerce a native text field (string or content-block list) to a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and block.get("type") in (
                "text", "input_text", "output_text",
            ):
                text = block.get("text")
                if text is not None:
                    parts.append(str(text))
        return " ".join(parts) if parts else None
    return None


def _capped(text: str) -> tuple[str, bool]:
    """Cap content at _CONTENT_CAP; returns (text, was_truncated)."""
    if len(text) <= _CONTENT_CAP:
        return text, False
    return text[:_CONTENT_CAP], True


def _reasoning_content(inner: dict) -> tuple[str | None, tuple, FidelityLevel]:
    """Plaintext reasoning from a reasoning response-item / event-msg payload.

    F11a: real Codex reasoning records come in three native shapes:

      (a) plaintext on payload.text (or payload.content, string or
          content-block list) - readable;
      (b) only payload.encrypted_content (+ summary) - not readable;
      (c) a mix of plaintext and encrypted content.

    Returns (content, dispositions, content_availability) where the
    fidelity level truthfully matches the returned content: COMPLETE for full
    plaintext, PARTIAL when capped, UNAVAILABLE when no plaintext exists
    (encrypted-only reasoning is not readable and never claims complete).
    """
    for key in ("content", "text"):
        text = _coerce_text(inner.get(key))
        if text is not None:
            capped, truncated = _capped(text)
            if truncated:
                return capped, (
                    FieldDispositionRecord(
                        field_name=key,
                        disposition=FieldDisposition.REDACTED,
                        reason="reasoning truncated; full text exceeds content cap",
                    ),
                ), FidelityLevel.PARTIAL
            return capped, (), FidelityLevel.COMPLETE
    if inner.get("encrypted_content") is not None:
        return None, (
            FieldDispositionRecord(
                field_name="encrypted_content",
                disposition=FieldDisposition.UNAVAILABLE,
                reason=_REASONING_ENCRYPTED_REASON,
            ),
        ), FidelityLevel.UNAVAILABLE
    return None, (), FidelityLevel.UNAVAILABLE


_TOKEN_FIELD_KEYS = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "cache_read": ("cache_read_input_tokens", "cached_input_tokens", "cache_read"),
    "cache_write": ("cache_creation_input_tokens", "cache_write_input_tokens", "cache_write"),
}


def _usage_summary(value) -> str | None:
    """Machine-parseable usage summary from a token-count dict.

    ``response_item`` blocks carry a ``usage`` object whose token counts are
    mapped onto the canonical USAGE summary grammar
    ``input_tokens=X output_tokens=Y [cache_read=Z cache_write=W]`` (only
    fields that are actually present; ``total_tokens`` is derived and omitted).
    Returns ``None`` when no token field is present so adapters never emit an
    empty usage event.
    """
    if not isinstance(value, dict):
        return None
    parts = []
    for label, keys in _TOKEN_FIELD_KEYS.items():
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                parts.append(f"{label}={int(raw)}")
                break
    return " ".join(parts) or None


def _token_count_usage(inner: dict) -> str | None:
    """Map a Codex event_msg token_count payload onto a usage summary.

    Real Codex exports carry token counts on token_count event messages
    under payload.info:

      info.last_token_usage    -> per-turn incremental token usage
      info.total_token_usage   -> cumulative usage for the whole session

    We prefer the per-turn incremental usage (last_token_usage), falling
    back to the cumulative (total_token_usage), then the whole info dict.
    Returns None when no numeric token field is present so the caller
    never emits a hollow USAGE event (degrades to unknown_native instead).
    """
    if not isinstance(inner, dict):
        return None
    info = inner.get("info")
    if not isinstance(info, dict):
        return None
    for candidate in (
        info.get("last_token_usage"),
        info.get("total_token_usage"),
        info,
    ):
        usage = _usage_summary(candidate)
        if usage:
            return usage
    return None


def _is_system_placeholder_title(text) -> bool:
    """True when a candidate title is a system-injected prompt rather than a
    real user-authored title. Detects Codex plugin/AGENTS scaffolding:
    directive blocks opening with '<' (e.g. <recommended_plugins>,
    <INSTRUCTIONS>, <AGENTS...), AGENTS.md / 'instructions for' markers, and
    the Codex replay/session preamble ('The following is the Codex agent
    history...') or bracketed system directives ('[Assistant Rules]',
    '[Skill...]')."""
    if not isinstance(text, str) or not text.strip():
        return False
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return True
    lowered = text[:180].lower()
    if "codex agent history" in lowered:
        return True
    if lowered.startswith("[assistant rules"):
        return True
    # Round-5 fix: the CLI injects a "# Files mentioned by the user" attachment
    # message that is not user-authored; skip it as a title source too.
    if lowered.startswith("# files mentioned by the user"):
        return True
    return "agents.md" in lowered or "instructions for" in lowered


def _first_user_text_default(events) -> str | None:
    """First real user-message text (bounded) as a session-title fallback.
    Skips user messages that are themselves system-injected scaffolding so a
    plugin/AGENTS prompt never becomes the session title."""
    for event in events:
        if event.kind is EventKind.USER_MESSAGE and event.content:
            text = event.content.strip()
            if _is_system_placeholder_title(text):
                continue
            return text[:120] or None
    return None


def _git_branch(meta) -> str | None:
    git = meta.get("git")
    if isinstance(git, dict) and git.get("branch"):
        return str(git["branch"])[:256] or None
    return None


def _concrete_model(records: list, meta: dict) -> str | None:
    """Extract the concrete model name that actually ran the session.

    session_meta only exposes model_provider (e.g. codex, custom,
    openai) which is too coarse to identify the model. In real Codex
    exports the specific model (e.g. gpt-5.6-luna, codex-auto-review)
    rides deeper:

      - turn_context.payload.model          (authoritative per turn)
      - event_msg thread_settings_applied payload.thread_settings.model
      - session_meta.payload.model          (newer direct field)

    The last-appearing value wins: the model can switch mid-session (e.g.
    a reviewer model on auto-review turns), and the final state is the
    most representative of the session. When no concrete model is present
    we return None so the caller falls back to the provider name.
    """
    concrete = None
    for record in records:
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        inner = _inner(record)
        if rtype == "turn_context":
            value = inner.get("model")
            if isinstance(value, str) and value.strip():
                concrete = value.strip()
        elif rtype == "session_meta":
            value = inner.get("model")
            if isinstance(value, str) and value.strip():
                concrete = value.strip()
        elif rtype == "event_msg" and inner.get("type") == "thread_settings_applied":
            settings = inner.get("thread_settings")
            value = settings.get("model") if isinstance(settings, dict) else None
            if isinstance(value, str) and value.strip():
                concrete = value.strip()
    if concrete:
        return concrete[:256]
    if isinstance(meta.get("model"), str) and meta.get("model"):
        return meta["model"][:256]
    return None


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one immutable Codex export into typed events/relations."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    records = list(iter_jsonl_lines(artifact_root / artifact.content_hash[:32]))

    # Session-context timestamps: started_at prefers the native session_meta
    # timestamp; ended_at is the last record's timestamp when one is present.
    record_timestamps = [
        ts for ts in (
            r.get("timestamp") or _inner(r).get("timestamp") for r in records
        ) if ts is not None
    ]

    session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    turn_of: dict[str, str] = {}  # event_id -> native turn id
    native_session = None
    meta: dict = {}
    for record in records:
        inner = _inner(record)
        if record.get("type") == "session_meta":
            meta = inner
        sid = record.get("session_id") or inner.get("session_id") or (
            inner.get("id") if record.get("type") == "session_meta" else None)
        if sid:
            native_session = sid
            break

    # Every record is ordered by its file line number; the line number is the
    # event stream's total order, so it doubles as the typed event ordinal
    # (stamped through _adapt_record, which maps one record to at most one
    # event; the response_item usage event inherits the same ordinate).
    for lineno, record in enumerate(records, start=1):
        locator = f"{artifact.relative_path}#L{lineno}"
        ev = _adapt_record(record, artifact, session_id=session_id,
                           locator=locator, ordinal=lineno)
        if ev is not None:
            events.append(ev)
            tid = _inner(record).get("turn_id")
            if tid:
                turn_of[ev.event_id] = tid
        if record.get("type") == "response_item":
            usage = _usage_summary(_inner(record).get("usage"))
            if usage:
                item_id = _inner(record).get("id") or _inner(record).get("item_id")
                events.append(_event(
                    artifact, session_id=session_id, kind=EventKind.USAGE,
                    locator=f"{artifact.relative_path}#usage:{lineno}",
                    native_id=f"{item_id or 'item'}#usage",
                    occurred_at=record.get("timestamp") or _inner(record).get("timestamp"),
                    summary=usage, native_session=native_session, ordinal=lineno,
                ))

    # Turn membership: events carrying a native turn id link to its boundary.
    boundaries = {e.provenance.native_event_id: e for e in events if e.kind is EventKind.TURN_BOUNDARY}
    for event_id, tid in turn_of.items():
        anchor = boundaries.get(tid)
        if anchor is not None and anchor.event_id != event_id:
            relations.append(EventRelation(
                relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-turn:{event_id}:{anchor.event_id}"),
                source_event_id=event_id, target_event_id=anchor.event_id,
                relation_kind=RelationKind.TURN_MEMBERSHIP,
            ))

    # Call/result pairing: call and output share the native call_id; the
    # output's native id carries a "#output" suffix (see _adapt_record).
    calls = {e.provenance.native_event_id: e for e in events if e.kind is EventKind.TOOL_CALL}
    results = {
        (e.provenance.native_event_id or "").removesuffix("#output"): e
        for e in events if e.kind is EventKind.TOOL_RESULT
    }
    for call_id, call_ev in calls.items():
        result_ev = results.get(call_id)
        if result_ev is None:
            warnings.append(f"tool call {call_id!r} has no result (partial)")
            continue
        relations.append(EventRelation(
            relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                      f"rel-call:{call_id}"),
            source_event_id=call_ev.event_id, target_event_id=result_ev.event_id,
            relation_kind=RelationKind.CALL_RESULT,
        ))

    unknown = sum(1 for e in events if e.kind is EventKind.UNKNOWN_NATIVE)
    if unknown:
        warnings.append(f"{unknown} unknown native record(s) preserved")

    # Sub-agent signalling: a session carrying `forked_from_id` is a forked
    # child whose parent session lives in another artifact, so the parent's
    # lifecycle event is never resolvable inside this single-session export.
    # If a matching parent lifecycle is present we link a SUBAGENT relation;
    # otherwise we emit a SUBAGENT_BOUNDARY event naming the parent source.
    forked_from = meta.get("forked_from_id")
    if isinstance(forked_from, str) and forked_from:
        parents = [
            e for e in events
            if e.kind is EventKind.SESSION_LIFECYCLE
            and e.provenance.native_event_id == forked_from
        ]
        if parents:
            child = next(
                (e for e in events
                 if e.kind is EventKind.SESSION_LIFECYCLE
                 and e.provenance.native_event_id == native_session),
                parents[0],
            )
            parent = parents[0]
            if child.event_id != parent.event_id:
                relations.append(EventRelation(
                    relation_id=make_event_id(
                        FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                        f"rel-subagent:{child.event_id}:{parent.event_id}",
                    ),
                    source_event_id=child.event_id, target_event_id=parent.event_id,
                    relation_kind=RelationKind.SUBAGENT,
                ))
            else:
                # forked_from_id resolves to this same session: a self-loop is
                # invalid, so signal the fork as a boundary event instead.
                events.append(_event(
                    artifact, session_id=session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                    locator=f"{artifact.relative_path}#fork:{forked_from}",
                    native_id=forked_from, summary=forked_from,
                    native_session=native_session,
                ))
        else:
            events.append(_event(
                artifact, session_id=session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                locator=f"{artifact.relative_path}#fork:{forked_from}",
                native_id=forked_from, summary=forked_from,
                native_session=native_session,
            ))

    sessions: list[AdaptedSession] = []
    if native_session:
        raw_summary = meta.get("summary")
        title = None
        if raw_summary and not _is_system_placeholder_title(raw_summary):
            title = raw_summary
        else:
            # summary is missing or system scaffolding -> first real user text.
            title = _first_user_text_default(events)
        # Prefer the concrete model name from records (turn_context / thread
        # settings / session_meta.model); fall back to the coarse provider name
        # only when the export carries no specific model.
        model = _concrete_model(records, meta) or meta.get("model_provider")
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=_provenance(artifact, f"{artifact.relative_path}#session",
                                   session=native_session, native_id=native_session),
            fidelity=_fidelity(),
            native_session_id=native_session,
            started_at=meta.get("timestamp") or (record_timestamps[0] if record_timestamps else None),
            ended_at=record_timestamps[-1] if record_timestamps else None,
            cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
            git_branch=_git_branch(meta),
            model=str(model) if model else None,
            title=title,
        ))

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events), fidelity=_fidelity(
            STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE,
        ),
        sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
    )
