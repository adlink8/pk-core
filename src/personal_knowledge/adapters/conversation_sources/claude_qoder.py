"""Phase 62-02: Claude / Qoder JSONL DAG adapters (families ``claude``, ``qoder``).

Both families export a UUID-parent DAG as JSONL (62-RESEARCH format
matrix): ``uuid``, ``parentUuid``, ``isSidechain``, content blocks. Parent
relations are authoritative — file order alone is insufficient — so we emit
``parent_child`` / ``sidechain`` typed relations and never guess relations
from adjacency. Qoder adds an explicit ``isCompactSummary`` record, which
becomes a ``compaction_summary`` event, not a user message. Each family
keeps its own detector, schema gate and capability/fidelity outcomes.
"""

from __future__ import annotations

import json
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

ADAPTER_VERSION = "1.4.0"

# Round-4 fix: attachment payloads are projected as bounded event content.
_ATTACHMENT_CAP = 50_000
CONTRACT_VERSION = "2"

# P1-F4 content-fidelity limits for tool blocks.
# tool_result carries the full native output into ``content`` up to a high
# bound; tool_call carries its JSON-serialised input parameters. When the
# native value exceeds a limit the tail is dropped and the loss is declared
# through CONTENT_AVAILABILITY=partial plus a REDACTED field disposition, so
# truncation is never silent. ``summary`` always stays a bounded synopsis.
_TOOL_RESULT_CONTENT_LIMIT = 100_000
_TOOL_CALL_INPUT_LIMIT = 50_000
_TOOL_SUMMARY_LIMIT = 2_048

# F11b: thinking/reasoning text is projected into content up to a high bound;
# beyond it the tail is dropped and the loss is declared through
# CONTENT_AVAILABILITY=partial plus a REDACTED field disposition, mirroring the
# tool-block policy. summary stays a bounded synopsis.
_REASONING_CONTENT_LIMIT = 100_000

_COMPLETE = {
    FidelityDimension.SOURCE_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.ORDERING_CONFIDENCE: FidelityLevel.COMPLETE,
    FidelityDimension.RELATION_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.CONTENT_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.COMPACTION_VISIBILITY: FidelityLevel.COMPLETE,
    FidelityDimension.NATIVE_ID_STABILITY: FidelityLevel.COMPLETE,
}


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


# Standalone operational / system-metadata record types emitted by Claude
# Code as top-level DAG records (no message envelope).  They carry no
# user/assistant content but encode session state, provenance or tooling
# bookkeeping; we classify them as system_message and surface their fields
# through summary so the information is never lost.
_META_RECORD_TYPES = {
    "last-prompt",
    "mode",
    "permission-mode",
    "ai-title",
    "attachment",
    "queue-operation",
    "file-history-snapshot",
    "pr-link",
    "file-history-delta",
}


def _record_kind(record: dict) -> EventKind | None:
    """Typed kind for a DAG record; None means unknown native."""
    if record.get("isCompactSummary"):
        return EventKind.COMPACTION_SUMMARY
    rtype = record.get("type")
    if rtype in ("user", "human"):
        return EventKind.USER_MESSAGE
    if rtype in ("assistant", "ai"):
        return EventKind.ASSISTANT_MESSAGE
    if rtype == "tool_use":
        return EventKind.TOOL_CALL
    if rtype == "tool_result":
        return EventKind.TOOL_RESULT
    if rtype in _META_RECORD_TYPES:
        return EventKind.SYSTEM_MESSAGE
    if rtype in ("system", "system_message"):
        subtype = record.get("subtype")
        if subtype == "turn_duration":
            return EventKind.USAGE
        if subtype == "compact_boundary":
            return EventKind.COMPACTION_SUMMARY
        # api_error and any other system subtype carry operational state
        # and are surfaced as a system_message (previously unknown native).
        return EventKind.SYSTEM_MESSAGE
    return None


def _text_content(record: dict) -> str | None:
    """Text from content blocks; tool blocks are not treated as prose."""
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    blocks = record.get("content", message.get("content"))
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, list):
        parts: list[str] = []
        saw_text = False
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            saw_text = True
            raw = block.get("text")
            parts.append("" if raw is None else str(raw))
        return " ".join(parts) if saw_text else None
    return None


_TOKEN_FIELD_KEYS = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "cache_read": ("cache_read", "cache_read_input_tokens", "cache_creation_input_tokens"),
    "cache_write": ("cache_write", "cache_write_input_tokens"),
}


def _usage_summary(value) -> str | None:
    """Machine-parseable usage summary from a token-count dict.

    Maps native token fields onto the canonical USAGE summary grammar
    ``input_tokens=X output_tokens=Y [cache_read=Z cache_write=W]`` (only
    fields that are actually present).  Returns ``None`` when no token field is
    present so adapters never emit an empty usage event.
    """
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    for label, keys in _TOKEN_FIELD_KEYS.items():
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                parts.append(f"{label}={int(raw)}")
                break
    return " ".join(parts) or None


def _message_usage(record: dict):
    """Recover the token-count dict for a DAG record, if any.

    The native assistant/user envelope nests usage under ``message`` (or under
    ``message.message`` in some exports); ``message.tokens`` is also accepted.
    A top-level ``usage``/``tokens`` is honoured as a fallback.
    """
    message = record.get("message")
    if isinstance(message, dict):
        for candidate in ("usage", "tokens"):
            value = message.get(candidate)
            if isinstance(value, dict):
                return value
        nested = message.get("message")
        if isinstance(nested, dict):
            for candidate in ("usage", "tokens"):
                value = nested.get(candidate)
                if isinstance(value, dict):
                    return value
    for candidate in ("usage", "tokens"):
        value = record.get(candidate)
        if isinstance(value, dict):
            return value
    return None


def _first_user_text_default(events) -> str | None:
    """First user-message text (bounded) as a session-title fallback."""
    for event in events:
        if event.kind is EventKind.USER_MESSAGE and event.content:
            text = event.content.strip()
            return text[:120] or None
    return None

def _content_blocks(record: dict):
    message = record.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    return record.get("content")


def _nested_text(value) -> str | None:
    """Recover text from a tool/reasoning block without inventing content."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        saw_text = False
        for item in value:
            if isinstance(item, str):
                saw_text = True
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                saw_text = True
                raw = item.get("text")
                parts.append("" if raw is None else str(raw))
        return " ".join(parts) if saw_text else None
    return None


def _metadata_summary(record: dict) -> str | None:
    """Surface a non-empty summary for standalone operational/metadata records.

    Returns ``None`` for ordinary message envelopes and for metadata records
    whose identifying field is absent, so callers fall through to the normal
    message/content path.  Every returned summary is non-empty and carries at
    least one native field value, so a classified system_message never loses
    the underlying information.
    """
    rtype = record.get("type")
    if rtype == "last-prompt":
        v = record.get("lastPrompt")
        return f"last-prompt: {v}" if isinstance(v, str) and v else None
    if rtype == "mode":
        m = record.get("mode")
        return f"mode={m}" if isinstance(m, str) and m else None
    if rtype == "permission-mode":
        p = record.get("permissionMode")
        return f"permission-mode={p}" if isinstance(p, str) and p else None
    if rtype == "ai-title":
        t = record.get("aiTitle")
        return f"ai-title: {t}" if isinstance(t, str) and t else None
    if rtype == "attachment":
        att = record.get("attachment")
        if isinstance(att, list) and att:
            kinds = [
                str(b.get("type"))
                for b in att
                if isinstance(b, dict) and b.get("type")
            ]
            return "attachment: " + (", ".join(kinds) if kinds else "list")
        # Round-4 fix: file-diff style attachments carry a dict payload
        # (addedNames/addedLines/...); name them by their native type instead
        # of falling through to an empty message envelope.
        if isinstance(att, dict) and att:
            return f"attachment: {att.get('type') or 'record'}"
        return None
    if rtype == "queue-operation":
        parts: list[str] = []
        op = record.get("operation")
        if isinstance(op, str) and op:
            parts.append(f"queue-operation={op}")
        c = record.get("content")
        if isinstance(c, str) and c:
            parts.append(str(c)[:256])
        return " ".join(parts)[:512] if parts else None
    if rtype == "file-history-snapshot":
        snap = record.get("snapshot")
        n = len(snap) if isinstance(snap, list) else 1
        return f"file-history-snapshot: {n} snapshot(s)"
    if rtype == "pr-link":
        parts = []
        if record.get("prRepository"):
            parts.append("pr=" + str(record.get("prRepository"))
                         + "#" + str(record.get("prNumber", "")))
        if record.get("prUrl"):
            parts.append(str(record.get("prUrl")))
        return " ".join(parts) if parts else "pr-link"
    if rtype == "file-history-delta":
        tp = record.get("trackingPath")
        return f"file-history-delta: {tp}" if isinstance(tp, str) and tp else None
    if rtype == "system" and record.get("subtype") == "api_error":
        err = record.get("error")
        if isinstance(err, dict):
            msg = err.get("formatted") or err.get("message") or err.get("code")
            if isinstance(msg, str) and msg:
                return f"api_error: {msg}"
        elif isinstance(err, str) and err:
            return f"api_error: {err}"
        # fall back to a compact serialisation so the summary is never empty
        return f"api_error: {json.dumps(err, ensure_ascii=False)}" if err else None
    return None


def _is_synthetic_model(value: str) -> bool:
    """True when a model string is a synthetic placeholder, not a real id.

    Claude Code fills message.model with "<synthetic>" for API-error and
    short-circuit messages; such values must not shadow a real model name.
    """
    return value.strip().startswith("<") and value.strip().endswith(">")


def _record_model_name(record: dict) -> str | None:
    """Best-effort resolution of the real model id from one DAG record.

    P1-F4: the slug field on a record is a deployment codename (for example
    vivid-forging-sloth); the human-visible model id (for example
    claude-fable-5) tends to appear in message.model or a top-level
    model-like field.  This returns the real model id when present and not a
    synthetic placeholder, and None otherwise so callers can fall back to slug
    without dropping the codename.
    """
    message = record.get("message")
    if isinstance(message, dict):
        for candidate in (message, message.get("message")):
            if isinstance(candidate, dict):
                value = candidate.get("model")
                if isinstance(value, str) and value and not _is_synthetic_model(value):
                    return value
    for key in ("model", "model_id", "modelID", "modelName", "model_name"):
        value = record.get(key)
        if isinstance(value, str) and value and not _is_synthetic_model(value):
            return value
    return None


class _Family:
    def __init__(self, family: str, *, dag_shape: bool = True, markers: tuple[str, ...] = ()):
        self.family = family
        self.dag_shape = dag_shape
        self.markers = markers

    def capability(self) -> CapabilityDescriptor:
        kinds = {
            EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE, EventKind.SYSTEM_MESSAGE,
            EventKind.REASONING,
            EventKind.TOOL_CALL, EventKind.TOOL_RESULT,
            EventKind.USAGE,
            EventKind.COMPACTION_SUMMARY, EventKind.UNKNOWN_NATIVE,
        }
        relations = {
            RelationKind.PARENT_CHILD, RelationKind.SIDECHAIN,
            RelationKind.CALL_RESULT,
        }
        if self.family == "qoder":
            relations.add(RelationKind.COMPACTED_RANGE)
        return CapabilityDescriptor(
            family=self.family, adapter_version=ADAPTER_VERSION,
            contract_version=CONTRACT_VERSION,
            supported_event_kinds=tuple(sorted(kinds, key=lambda k: k.value)),
            supported_relation_kinds=tuple(sorted(relations, key=lambda r: r.value)),
            fidelity_dimensions=tuple(FidelityDimension),
            capabilities={
                "native_shape": "jsonl_uuid_dag",
                "relations": "parent_uuid_authoritative",
                "compaction": "explicit_compact_summary" if self.family == "qoder" else "content_blocks",
            },
        )

    def detect(self, artifact: SourceArtifact, *, artifact_root: Path) -> bool:
        if not (artifact.relative_path or "").lower().endswith(".jsonl"):
            return False
        try:
            lines = artifact_bytes_path(artifact_root, artifact).read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        if self.dag_shape and not any('"uuid"' in l and '"parentUuid"' in l for l in lines):
            return False
        return any(m in l for l in lines for m in self.markers) if self.markers else True

    def _event(self, artifact, *, session_id, kind, locator, native_id=None,
               occurred_at=None, content=None, summary=None, fidelity=None,
               native_session=None, ordinal=None, native_payload_ref=None,
               field_dispositions=()) -> TypedEvent:
        return TypedEvent(
            event_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                   native_id or locator, kind=kind, session_id=session_id,
                                   native_locator=locator),
            session_id=session_id, kind=kind,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=locator, native_session_id=native_session or None,
                native_event_id=native_id, contract_version=CONTRACT_VERSION,
            ),
            fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
            ordinal=ordinal, native_payload_ref=native_payload_ref,
            content=content, summary=summary,
            field_dispositions=tuple(field_dispositions),
        )

    def _adapt_record(
        self, record: dict, artifact, *, session_id, locator, ordinal_start: int,
    ) -> tuple[list[TypedEvent], list[tuple[str, str, TypedEvent]]]:
        """Map one envelope, expanding each native content block separately."""

        kind = _record_kind(record)
        ts = record.get("timestamp")
        sid = record.get("session_id") or record.get("sessionId")
        if kind is None:
            dispositions = ()
            if record.get("type") in ("system", "system_message"):
                dispositions = (FieldDispositionRecord(
                    "error", FieldDisposition.PRESERVED_BY_REFERENCE,
                    "non-text system error envelope preserved by native locator",
                ),)
            return [self._event(
                artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                locator=locator, native_id=record.get("uuid"), occurred_at=ts,
                ordinal=ordinal_start,
                fidelity=_fidelity(
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                ),
                native_session=sid, native_payload_ref=locator,
                field_dispositions=dispositions,
            )], []
        is_message = kind in {
            EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE,
            EventKind.SYSTEM_MESSAGE,
        }
        # Standalone operational/metadata records (mode, ai-title, pr-link,
        # api_error, ...) map to system_message and carry a non-empty summary
        # recovered from their own fields rather than a message envelope.
        meta_summary = _metadata_summary(record) if kind is EventKind.SYSTEM_MESSAGE else None
        if meta_summary is not None:
            # Round-4 fix: attachment records carry their payload (file diff
            # blocks etc.) on record.attachment; project it as bounded content
            # instead of a type-list-only summary.
            meta_content = None
            meta_disp = (FieldDispositionRecord(
                f"type:{record.get('type')}", FieldDisposition.MAPPED,
                "operational metadata record classified as system_message",
            ),)
            if record.get("type") == "attachment" and record.get("attachment") is not None:
                try:
                    att_text = json.dumps(record["attachment"], ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    att_text = str(record["attachment"])
                if att_text:
                    if len(att_text) > _ATTACHMENT_CAP:
                        att_text = att_text[:_ATTACHMENT_CAP]
                        meta_disp = meta_disp + (FieldDispositionRecord(
                            "attachment", FieldDisposition.MAPPED,
                            "attachment truncated; bounded preservation",
                        ),)
                    meta_content = att_text
            return [self._event(
                artifact, session_id=session_id, kind=EventKind.SYSTEM_MESSAGE,
                locator=locator, native_id=record.get("uuid"), occurred_at=ts,
                ordinal=ordinal_start, content=meta_content, summary=meta_summary,
                native_session=sid, native_payload_ref=locator,
                field_dispositions=meta_disp,
            )], []
        if not is_message:
            text = _text_content(record)
            return [self._event(
                artifact, session_id=session_id, kind=kind, locator=locator,
                native_id=record.get("uuid"), occurred_at=ts,
                ordinal=ordinal_start,
                content=None, summary=text, native_session=sid,
                native_payload_ref=locator,
            )], []

        blocks = _content_blocks(record)
        if isinstance(blocks, str) or blocks is None:
            content = blocks if isinstance(blocks, str) else None
            fidelity = (
                _fidelity()
                if blocks is not None
                else _fidelity(CONTENT_AVAILABILITY=FidelityLevel.UNAVAILABLE)
            )
            dispositions = () if blocks is not None else (
                FieldDispositionRecord(
                    "content", FieldDisposition.UNAVAILABLE,
                    "message envelope has no native content field",
                ),
            )
            return [self._event(
                artifact, session_id=session_id, kind=kind, locator=locator,
                native_id=record.get("uuid"), occurred_at=ts,
                ordinal=ordinal_start, content=content, native_session=sid,
                fidelity=fidelity, field_dispositions=dispositions,
            )], []

        if not isinstance(blocks, list):
            blocks = [blocks]
        if not blocks:
            return [self._event(
                artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                locator=f"{locator}/content", native_id=record.get("uuid"),
                occurred_at=ts, ordinal=ordinal_start, native_session=sid,
                native_payload_ref=f"{locator}/content",
                fidelity=_fidelity(
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    CONTENT_AVAILABILITY=FidelityLevel.UNAVAILABLE,
                ),
                field_dispositions=(FieldDispositionRecord(
                    "content", FieldDisposition.PRESERVED_BY_REFERENCE,
                    "empty non-text message envelope preserved by locator",
                ),),
            )], []

        events: list[TypedEvent] = []
        call_links: list[tuple[str, str, TypedEvent]] = []
        envelope_id = record.get("uuid")
        for block_index, block in enumerate(blocks):
            block_locator = f"{locator}/content/{block_index}"
            block_native_id = (
                f"{envelope_id}:content:{block_index}" if envelope_id else None
            )
            block_type = block.get("type") if isinstance(block, dict) else None
            block_kind = EventKind.UNKNOWN_NATIVE
            block_content = None
            block_summary = None
            block_fidelity = _fidelity()
            dispositions: tuple[FieldDispositionRecord, ...] = ()
            call_link: tuple[str, str] | None = None

            if block_type == "text":
                block_kind = kind
                raw = block.get("text")
                block_content = None if raw is None else str(raw)
                dispositions = (FieldDispositionRecord(
                    f"content[{block_index}].text", FieldDisposition.MAPPED,
                    "mapped exactly to message content",
                ),)
            elif block_type in ("thinking", "reasoning"):
                block_kind = EventKind.REASONING
                raw = block.get("thinking", block.get("text"))
                text = None if raw is None else str(raw)
                if text is None:
                    # no native text: content stays None and the absence is
                    # declared explicitly (never reported as complete).
                    block_fidelity = block_fidelity.with_at_least(
                        FidelityDimension.CONTENT_AVAILABILITY,
                        FidelityLevel.UNAVAILABLE,
                    )
                    dispositions = dispositions + (FieldDispositionRecord(
                        "reasoning_content", FieldDisposition.UNAVAILABLE,
                        "reasoning block has no recoverable text",
                    ),)
                elif len(text) > _REASONING_CONTENT_LIMIT:
                    block_content = text[:_REASONING_CONTENT_LIMIT]
                    block_fidelity = block_fidelity.with_at_least(
                        FidelityDimension.CONTENT_AVAILABILITY,
                        FidelityLevel.PARTIAL,
                    )
                    dispositions = dispositions + (FieldDispositionRecord(
                        "reasoning_content", FieldDisposition.REDACTED,
                        "reasoning text truncated to 100000 chars",
                    ),)
                else:
                    block_content = text
                    dispositions = dispositions + (FieldDispositionRecord(
                        "reasoning_content", FieldDisposition.MAPPED,
                        "reasoning text mapped exactly to content",
                    ),)
                block_summary = text[:2048] if text else None
            elif block_type == "tool_use":
                block_kind = EventKind.TOOL_CALL
                call_id = block.get("id")
                block_summary = str(block.get("name") or "tool_use")[:256]
                if call_id:
                    call_link = (str(call_id), "call")
                else:
                    block_fidelity = block_fidelity.with_at_least(
                        FidelityDimension.RELATION_COMPLETENESS,
                        FidelityLevel.PARTIAL,
                    )
                    dispositions = dispositions + (FieldDispositionRecord(
                        "tool_call_id", FieldDisposition.UNAVAILABLE,
                        "native tool call block has no recoverable call id",
                    ),)
                # P1-F4: carry the tool-call input parameters into content
                # (JSON-serialised and bounded) so they are never dropped.
                input_value = block.get("input")
                if input_value is not None and input_value != "":
                    serialised = json.dumps(
                        input_value, ensure_ascii=False, sort_keys=True,
                    )
                    if len(serialised) > _TOOL_CALL_INPUT_LIMIT:
                        block_content = serialised[:_TOOL_CALL_INPUT_LIMIT]
                        block_fidelity = block_fidelity.with_at_least(
                            FidelityDimension.CONTENT_AVAILABILITY,
                            FidelityLevel.PARTIAL,
                        )
                        dispositions = dispositions + (FieldDispositionRecord(
                            "tool_call_input", FieldDisposition.REDACTED,
                            f"tool call input truncated to {_TOOL_CALL_INPUT_LIMIT} chars",
                        ),)
                    else:
                        block_content = serialised
                        dispositions = dispositions + (FieldDispositionRecord(
                            "tool_call_input", FieldDisposition.MAPPED,
                            "tool call input parameters mapped to content",
                        ),)
                else:
                    dispositions = dispositions + (FieldDispositionRecord(
                        "tool_call_input", FieldDisposition.UNAVAILABLE,
                        "tool call block has no recoverable input parameters",
                    ),)
            elif block_type == "tool_result":
                block_kind = EventKind.TOOL_RESULT
                call_id = block.get("tool_use_id") or block.get("call_id")
                text = _nested_text(block.get("content"))
                block_summary = text[:_TOOL_SUMMARY_LIMIT] if text else None
                if call_id:
                    call_link = (str(call_id), "result")
                else:
                    block_fidelity = block_fidelity.with_at_least(
                        FidelityDimension.RELATION_COMPLETENESS,
                        FidelityLevel.PARTIAL,
                    )
                    dispositions = dispositions + (FieldDispositionRecord(
                        "tool_call_id", FieldDisposition.UNAVAILABLE,
                        "native tool result block has no recoverable call id",
                    ),)
                # P1-F4: map the full native output into content so tool results
                # longer than the old 2048-char summary are recoverable, not just
                # preserved-by-locator. Truncation beyond the high bound is flagged.
                if text is not None:
                    if len(text) > _TOOL_RESULT_CONTENT_LIMIT:
                        block_content = text[:_TOOL_RESULT_CONTENT_LIMIT]
                        block_fidelity = block_fidelity.with_at_least(
                            FidelityDimension.CONTENT_AVAILABILITY,
                            FidelityLevel.PARTIAL,
                        )
                        dispositions = dispositions + (FieldDispositionRecord(
                            "tool_result_content", FieldDisposition.REDACTED,
                            "tool output truncated; full output preserved by native locator",
                        ),)
                    else:
                        block_content = text
                        dispositions = dispositions + (FieldDispositionRecord(
                            "tool_result_content", FieldDisposition.MAPPED,
                            "tool output mapped exactly to content",
                        ),)
                else:
                    dispositions = dispositions + (FieldDispositionRecord(
                        "tool_result_content", FieldDisposition.UNAVAILABLE,
                        "tool result block has no recoverable content",
                    ),)
            else:
                block_fidelity = _fidelity(
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                )
                dispositions = (FieldDispositionRecord(
                    f"content[{block_index}]",
                    FieldDisposition.PRESERVED_BY_REFERENCE,
                    f"unsupported native content block type {block_type!r}",
                ),)

            event = self._event(
                artifact, session_id=session_id, kind=block_kind,
                locator=block_locator, native_id=block_native_id,
                occurred_at=ts, ordinal=ordinal_start + len(events),
                native_session=sid, native_payload_ref=block_locator,
                content=block_content, summary=block_summary,
                fidelity=block_fidelity, field_dispositions=dispositions,
            )
            events.append(event)
            if call_link is not None:
                call_links.append((call_link[0], call_link[1], event))
        return events, call_links

    def adapt(self, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
        if len(artifact_set.artifacts) != 1:
            raise EventContractError(
                f"{self.family} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
            )
        artifact = artifact_set.artifacts[0]
        records = list(iter_jsonl_lines(artifact_root / artifact.content_hash[:32]))

        session_id = make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                   None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
        events: list[TypedEvent] = []
        relations: list[EventRelation] = []
        warnings: list[str] = []
        by_uuid: dict[str, TypedEvent] = {}
        parent_links: list[tuple[TypedEvent, str, bool]] = []
        by_call_id: dict[str, dict[str, list[TypedEvent]]] = {}
        native_session = next((
            r.get("session_id") or r.get("sessionId")
            for r in records if r.get("session_id") or r.get("sessionId")
        ), Path(artifact.relative_path).stem)

        for lineno, record in enumerate(records, start=1):
            record_events, call_links = self._adapt_record(
                record, artifact, session_id=session_id,
                locator=f"{artifact.relative_path}#L{lineno}",
                ordinal_start=len(events),
            )
            if not record_events:
                continue
            events.extend(record_events)
            usage = _usage_summary(_message_usage(record))
            if usage:
                events.append(self._event(
                    artifact, session_id=session_id, kind=EventKind.USAGE,
                    locator=f"{artifact.relative_path}#usage:{lineno}",
                    native_id=f"usage:{record.get('uuid') or lineno}",
                    occurred_at=record.get("timestamp"),
                    ordinal=len(events), summary=usage, native_session=native_session,
                ))
            for call_id, role, event in call_links:
                by_call_id.setdefault(
                    call_id, {"call": [], "result": []}
                )[role].append(event)
            uuid = record.get("uuid")
            if uuid:
                by_uuid[uuid] = record_events[0]
            parent = record.get("parentUuid")
            if parent:
                parent_links.extend(
                    (event, parent, bool(record.get("isSidechain")))
                    for event in record_events
                )

        missing_parent = False
        for child, parent_uuid, sidechain in parent_links:
            parent_ev = by_uuid.get(parent_uuid)
            if parent_ev is None:
                missing_parent = True
                warnings.append(f"uuid {parent_uuid!r} has no in-file parent (partial relation)")
                continue
            relations.append(EventRelation(
                relation_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-dag:{child.event_id}:{parent_ev.event_id}"),
                source_event_id=child.event_id, target_event_id=parent_ev.event_id,
                relation_kind=RelationKind.SIDECHAIN if sidechain else RelationKind.PARENT_CHILD,
            ))

        unmatched_tool_ids: set[str] = set()
        for call_id, endpoints in by_call_id.items():
            calls = endpoints["call"]
            results = endpoints["result"]
            for pair_index, (call, result) in enumerate(zip(calls, results)):
                relations.append(EventRelation(
                    relation_id=make_event_id(
                        self.family, artifact.artifact_id, CONTRACT_VERSION,
                        f"rel-call:{call_id}:{pair_index}",
                    ),
                    source_event_id=call.event_id,
                    target_event_id=result.event_id,
                    relation_kind=RelationKind.CALL_RESULT,
                ))
            unmatched = (*calls[len(results):], *results[len(calls):])
            if unmatched:
                unmatched_tool_ids.update(event.event_id for event in unmatched)
                warnings.append(
                    f"tool call id {call_id!r} has unmatched call/result block(s)"
                )

        if unmatched_tool_ids:
            marked: list[TypedEvent] = []
            for event in events:
                if event.event_id not in unmatched_tool_ids:
                    marked.append(event)
                    continue
                missing_field = (
                    "tool_result_relation"
                    if event.kind is EventKind.TOOL_CALL
                    else "tool_call_relation"
                )
                marked.append(replace(
                    event,
                    fidelity=event.fidelity.with_at_least(
                        FidelityDimension.RELATION_COMPLETENESS,
                        FidelityLevel.PARTIAL,
                    ),
                    field_dispositions=event.field_dispositions + (
                        FieldDispositionRecord(
                            missing_field, FieldDisposition.UNAVAILABLE,
                            "native call id has no matching block in this artifact",
                        ),
                    ),
                ))
            events = marked

        if self.family == "qoder":
            for compact_ev in [e for e in events if e.kind is EventKind.COMPACTION_SUMMARY]:
                earliest = min((e for e in events if e.kind is not EventKind.COMPACTION_SUMMARY),
                               key=lambda e: e.ordinal or 0, default=None)
                if earliest is not None and earliest.event_id != compact_ev.event_id:
                    relations.append(EventRelation(
                        relation_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                                  f"rel-compact:{compact_ev.event_id}"),
                        source_event_id=compact_ev.event_id, target_event_id=earliest.event_id,
                        relation_kind=RelationKind.COMPACTED_RANGE,
                    ))

        unknown = sum(1 for e in events if e.kind is EventKind.UNKNOWN_NATIVE)
        if unknown:
            warnings.append(f"{unknown} unknown native record(s) preserved")

        # Session-context + sub-agent extraction (Agent B extension).
        # The "main" session is the shared sessionId whose records carry no
        # agentId.  Records carrying an agentId belong to a sub-agent session;
        # when a main session is resolvable we link a SUBAGENT relation from
        # the sub-session lifecycle event to the main lifecycle event, otherwise
        # we fall back to a SUBAGENT_BOUNDARY event named by the agentId.
        main_records = [r for r in records if not r.get("agentId")]
        context_records = main_records or records
        cwd = git_branch = model = title = stop_reason = None
        file_cwd = None
        file_git_branch = None
        for r in context_records:
            if cwd is None and isinstance(r.get("cwd"), str) and r.get("cwd"):
                cwd = r.get("cwd")
            if git_branch is None and isinstance(r.get("gitBranch"), str) and r.get("gitBranch"):
                git_branch = r.get("gitBranch")
            if model is None:
                real = _record_model_name(r)
                if real is not None:
                    model = real
        if model is None:  # fall back to the slug codename when no real id is recoverable
            for r in context_records:
                if isinstance(r.get("slug"), str) and r.get("slug"):
                    model = r.get("slug")
                    break
        for r in records:  # file-level fallback shared by sub-agent sessions
            if file_cwd is None and isinstance(r.get("cwd"), str) and r.get("cwd"):
                file_cwd = r.get("cwd")
            if file_git_branch is None and isinstance(r.get("gitBranch"), str) and r.get("gitBranch"):
                file_git_branch = r.get("gitBranch")
        for r in reversed(context_records):
            if _record_kind(r) is EventKind.ASSISTANT_MESSAGE:
                msg = r.get("message")
                sr = msg.get("stop_reason") if isinstance(msg, dict) else r.get("stop_reason")
                if isinstance(sr, str) and sr:
                    stop_reason = sr
                break

        # Session-context timestamps: started_at = first record timestamp,
        # ended_at = last record timestamp (native DAG order, not calendar sort).
        main_timestamps = [r.get("timestamp") for r in context_records if r.get("timestamp")]

        _MESSAGE_KINDS = {
            EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE,
            EventKind.DEVELOPER_MESSAGE, EventKind.SYSTEM_MESSAGE,
        }
        main_has_messages = any(
            _record_kind(r) in _MESSAGE_KINDS for r in main_records)
        agent_ids: list[str] = []
        seen: set[str] = set()
        for r in records:
            a = r.get("agentId")
            if isinstance(a, str) and a and a not in seen:
                seen.add(a)
                agent_ids.append(a)

        # Per-sub-agent timestamps (native DAG order) bound each sub-session.
        agent_timestamps = {
            agent: [r.get("timestamp") for r in records
                    if r.get("agentId") == agent and r.get("timestamp")]
            for agent in agent_ids
        }
        # F8: the sub-agent model id may sit on any record of the agent session
        # (not just the first). Scan every record carrying this agentId and take
        # the first real model name, mirroring the main-session logic; fall back
        # to the agent's slug codename only when no real id is recoverable.
        def _agent_model(agent: str) -> str | None:
            for r in records:
                if r.get("agentId") != agent:
                    continue
                real = _record_model_name(r)
                if real is not None:
                    return real
            for r in records:
                if r.get("agentId") != agent:
                    continue
                if isinstance(r.get("slug"), str) and r.get("slug"):
                    return r.get("slug")
            return None

        agent_models = {agent: _agent_model(agent) for agent in agent_ids}

        # Main session lifecycle event is only materialised when this artifact
        # contains sub-agent records, so the SUBAGENT relation can anchor on it;
        # the plain single-session export keeps its original event stream.
        main_lifecycle = None
        if agent_ids:
            main_lifecycle = self._event(
                artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                locator=f"{artifact.relative_path}#session", native_id=native_session,
                occurred_at=next((r.get("timestamp") for r in records if r.get("timestamp")), None),
                ordinal=len(events), native_session=native_session,
            )
            events.append(main_lifecycle)

        sessions: list[AdaptedSession] = []
        if native_session:
            title = _first_user_text_default(events)
            sessions.append(AdaptedSession(
                session_id=session_id,
                provenance=Provenance(
                    artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                    native_locator=f"{artifact.relative_path}#session",
                    native_session_id=native_session, native_event_id=native_session,
                    contract_version=CONTRACT_VERSION,
                ),
                fidelity=_fidelity(), native_session_id=native_session,
                started_at=main_timestamps[0] if main_timestamps else None,
                ended_at=main_timestamps[-1] if main_timestamps else None,
                cwd=cwd, git_branch=git_branch, model=model,
                title=title, stop_reason=stop_reason,
            ))

        for agent in agent_ids:
            # Round-4 fix: a standalone sub-agent file (main records exist but
            # none of them is a message; every message belongs to an agent)
            # already yields its own full session — an extra 1-event
            # placeholder session would duplicate it, so skip the placeholder
            # and its self-referential relation. Files with NO main records at
            # all keep the SUBAGENT_BOUNDARY fallback below.
            if main_records and not main_has_messages:
                continue
            agent_record = next(r for r in records if r.get("agentId") == agent)
            sub_session_id = make_event_id(
                self.family, artifact.artifact_id, CONTRACT_VERSION, None,
                kind=EventKind.SESSION_LIFECYCLE, session_id=session_id,
                native_locator=f"session-agent:{agent}",
            )
            agent_msg = agent_record.get("message")
            agent_sr = agent_msg.get("stop_reason") if isinstance(agent_msg, dict) else None
            if bool(main_records):
                sub_lifecycle = self._event(
                    artifact, session_id=sub_session_id, kind=EventKind.SESSION_LIFECYCLE,
                    locator=f"{artifact.relative_path}#session-agent:{agent}",
                    native_id=f"agent:{agent}", occurred_at=agent_record.get("timestamp"),
                    ordinal=len(events), native_session=native_session,
                )
                events.append(sub_lifecycle)
                relations.append(EventRelation(
                    relation_id=make_event_id(
                        self.family, artifact.artifact_id, CONTRACT_VERSION,
                        f"rel-subagent:{sub_lifecycle.event_id}:{main_lifecycle.event_id}",
                    ),
                    source_event_id=sub_lifecycle.event_id,
                    target_event_id=main_lifecycle.event_id,
                    relation_kind=RelationKind.SUBAGENT,
                ))
                sub_native_session = agent_record.get("sessionId") or agent_record.get("session_id")
                sessions.append(AdaptedSession(
                    session_id=sub_session_id,
                    provenance=Provenance(
                        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                        native_locator=f"{artifact.relative_path}#session-agent:{agent}",
                        native_session_id=sub_native_session or native_session,
                        native_event_id=f"agent:{agent}", contract_version=CONTRACT_VERSION,
                    ),
                    fidelity=_fidelity(), native_session_id=sub_native_session or native_session,
                    started_at=agent_ts[0] if (agent_ts := agent_timestamps.get(agent)) else None,
                    ended_at=agent_ts[-1] if agent_ts else None,
                    cwd=agent_record.get("cwd") if isinstance(agent_record.get("cwd"), str) else file_cwd,
                    git_branch=(
                        agent_record.get("gitBranch")
                        if isinstance(agent_record.get("gitBranch"), str) else file_git_branch
                    ),
                    model=agent_models.get(agent),
                    stop_reason=agent_sr if isinstance(agent_sr, str) else None,
                ))
            else:
                # No main session resolvable: at least signal the sub-agent start.
                events.append(self._event(
                    artifact, session_id=sub_session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                    locator=f"{artifact.relative_path}#agent:{agent}",
                    native_id=f"agent:{agent}", occurred_at=agent_record.get("timestamp"),
                    ordinal=len(events), summary=agent, native_session=native_session,
                ))
                # The boundary event references sub_session_id: the session row
                # must exist for the ce_events FK (orphan events fail staging).
                sub_native_session = agent_record.get("sessionId") or agent_record.get("session_id")
                sessions.append(AdaptedSession(
                    session_id=sub_session_id,
                    provenance=Provenance(
                        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                        native_locator=f"{artifact.relative_path}#agent:{agent}",
                        native_session_id=sub_native_session or native_session,
                        native_event_id=f"agent:{agent}", contract_version=CONTRACT_VERSION,
                    ),
                    fidelity=_fidelity(), native_session_id=sub_native_session or native_session,
                    started_at=agent_ts[0] if (agent_ts := agent_timestamps.get(agent)) else None,
                    ended_at=agent_ts[-1] if agent_ts else None,
                    cwd=agent_record.get("cwd") if isinstance(agent_record.get("cwd"), str) else file_cwd,
                    git_branch=(
                        agent_record.get("gitBranch")
                        if isinstance(agent_record.get("gitBranch"), str) else file_git_branch
                    ),
                    model=agent_models.get(agent),
                    stop_reason=agent_sr if isinstance(agent_sr, str) else None,
                ))

        return AdaptationResult(
            family=self.family, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
            artifacts=(artifact,), events=tuple(events),
            fidelity=_fidelity(
                STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE,
                RELATION_COMPLETENESS=(
                    FidelityLevel.PARTIAL
                    if missing_parent or unmatched_tool_ids or any(
                        event.kind in {EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
                        and event.fidelity.level(
                            FidelityDimension.RELATION_COMPLETENESS
                        ) is not FidelityLevel.COMPLETE
                        for event in events
                    )
                    else FidelityLevel.COMPLETE
                ),
            ),
            sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
        )


_FAMILIES = {
    "claude": _Family("claude", markers=('"stop_reason"', '"isSidechain"')),
    "qoder": _Family("qoder", markers=('"isCompactSummary"',)),
}


def capability(family: str) -> CapabilityDescriptor:
    return _FAMILIES[family].capability()


def detect(family: str, artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    return _FAMILIES[family].detect(artifact, artifact_root=artifact_root)


def adapt(family: str, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    return _FAMILIES[family].adapt(artifact_set, artifact_root=artifact_root)
