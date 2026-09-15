"""Phase 62-02: Workbuddy / Kimi / Kimi-work JSONL adapters.

Workbuddy exports message/reasoning/function_call/function_call_result
records; reasoning and call/result survive as separate linked typed events.
Kimi/Kimi-work use a loop protocol with turn prompt, context append and
loop/task lifecycle records — loop and task boundaries become first-class
episode hints (``loop_boundary`` / ``turn_boundary``) rather than prose.
Each family keeps its own detector, capability contract and fidelity
outcomes; unknown kinds stay ``unknown_native``.
"""

from __future__ import annotations

from datetime import datetime
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

ADAPTER_VERSION = "1.3.0"

# Round-4 audit fix: tool/reasoning payloads (kimi wire loop events and
# workbuddy flat records) were classified but never carried as event content.
# They are now full-fidelity content with explicit caps; truncation is flagged
# via a field disposition instead of being silent.
_TOOL_INPUT_CAP = 50_000
_TOOL_OUTPUT_CAP = 100_000
_REASONING_CAP = 100_000
_UNKNOWN_CAP = 10_000


def _capped(text: str, cap: int, field: str, reason: str) -> tuple[str, tuple]:
    if len(text) <= cap:
        return text, ()
    return text[:cap], (FieldDispositionRecord(
        field_name=field, disposition=FieldDisposition.MAPPED, reason=reason,
    ),)


def _payload_str(value) -> str | None:
    """Serialize a native payload (str / dict / list) as stable text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    import json as _json
    try:
        return _json.dumps(value, ensure_ascii=False, sort_keys=True) or None
    except (TypeError, ValueError):
        return str(value) or None


def _block_text(value) -> str | None:
    """Joined text of a native block list (e.g. workbuddy rawContent)."""
    if not isinstance(value, list):
        return None
    parts = [b["text"] for b in value
             if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return " ".join(parts) if parts else None
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

# Workbuddy record kind -> typed event kind.
_WORKBUDDY_KINDS = {
    "message": None,  # decided by role
    "reasoning": EventKind.REASONING,
    "function_call": EventKind.TOOL_CALL,
    "function_call_result": EventKind.TOOL_RESULT,
}
# New-format Kimi/Kimi-work event stream: each wire line is a journal
# 'kind:event' record carrying an envelope with type + payload; the top-level
# has no 'type' key, so classification reads envelope.type instead.
_ENVELOPE_KINDS = {
    'tool.call.started': EventKind.TOOL_CALL,
    'tool.result': EventKind.TOOL_RESULT,
    'turn.started': EventKind.TURN_BOUNDARY,
    'turn.ended': EventKind.TURN_BOUNDARY,
    'turn.step.started': EventKind.TURN_BOUNDARY,
    'turn.step.completed': EventKind.TURN_BOUNDARY,
    'turn.step.interrupted': EventKind.TURN_BOUNDARY,
    'turn.cancel': EventKind.TURN_BOUNDARY,
    'prompt.completed': EventKind.TURN_BOUNDARY,
    'prompt.aborted': EventKind.TURN_BOUNDARY,
    'context.spliced': EventKind.FILE_CONTEXT,
    'subagent.spawned': EventKind.SUBAGENT_BOUNDARY,
    'subagent.started': EventKind.SUBAGENT_BOUNDARY,
    'subagent.completed': EventKind.SUBAGENT_BOUNDARY,
    'subagent.failed': EventKind.SUBAGENT_BOUNDARY,
    'task.started': EventKind.LOOP_BOUNDARY,
    'task.terminated': EventKind.LOOP_BOUNDARY,
    'task.notified': EventKind.LOOP_BOUNDARY,
    'background.task.started': EventKind.LOOP_BOUNDARY,
    'background.task.terminated': EventKind.LOOP_BOUNDARY,
    'compaction.started': EventKind.COMPACTION_SUMMARY,
    'compaction.completed': EventKind.COMPACTION_SUMMARY,
    'compaction.blocked': EventKind.COMPACTION_SUMMARY,
    'agent.created': EventKind.SESSION_LIFECYCLE,
    'event.session.created': EventKind.SESSION_LIFECYCLE,
    'event.session.work_changed': EventKind.SESSION_LIFECYCLE,
    'session.meta.updated': EventKind.SESSION_LIFECYCLE,
    'goal.updated': EventKind.LOOP_BOUNDARY,
    'goal.create': EventKind.LOOP_BOUNDARY,
    'skill.activated': EventKind.SESSION_LIFECYCLE,
    'error': EventKind.UNKNOWN_NATIVE,
}


def _envelope(record):
    """Return the envelope block of a new-format record, else None."""
    if record.get('kind') == 'event':
        env = record.get('envelope')
        if isinstance(env, dict):
            return env
    return None


def _spliced_text(payload: dict) -> str | None:
    """Bounded text of a context.spliced payload (its messages array)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = _text_blocks(content) if content is not None else None
        if text:
            parts.append(f"{role}: {text}" if role else text)
    joined = "\n".join(parts)
    return joined[:2048] or None



def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


def _timestamp(value) -> str | None:
    """Normalize epoch-millisecond timestamps (real exports) or passthrough."""
    if value is None:
        return None
    if isinstance(value, int) and value > 1_000_000_000_000:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return str(value)


def _record_time(record) -> str | None:
    """ISO timestamp from an old-format kimi record.

    Old-format records carry a millisecond-epoch time (some also timestamp);
    the metadata header uses created_at instead.
    """
    for key in ("time", "timestamp", "created_at"):
        value = record.get(key)
        if value is not None:
            return _timestamp(value)
    return None


def _session_time_bounds(records) -> tuple[str | None, str | None]:
    """会话起止时间：对全部记录取时间 min/max。

    不同记录的时间戳来源不一致（部分用 ``time`` 毫秒纪元、部分用
    ``created_at`` 字符串），直接取 ``records[0]`` / ``records[-1]`` 会在短会话
    里出现 ``started_at > ended_at`` 的倒挂（实测约 1.2 秒）。统一取极值可修正。
    空 records 或全 None 时返回 ``(None, None)``。
    """

    def _sort_key(stamp: str):
        try:
            return (0, datetime.fromisoformat(stamp).timestamp(), stamp)
        except (ValueError, TypeError):
            return (1, 0.0, stamp)

    stamps = [t for t in (_record_time(r) for r in records) if t is not None]
    if not stamps:
        return None, None
    return min(stamps, key=_sort_key), max(stamps, key=_sort_key)


def _text_blocks(value) -> str | None:
    """Text from a content block list or a flat string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        saw_text = False
        for block in value:
            if isinstance(block, dict) and block.get("type") in (
                "text", "input_text", "output_text",
            ):
                saw_text = True
                raw = block.get("text")
                parts.append("" if raw is None else str(raw))
        if saw_text:
            return "\n".join(parts)
    return None


def _extract_model(record: dict) -> str | None:
    """Extract the model name from a Workbuddy or Kimi record.

    Workbuddy exports name the model under providerData.model. Kimi
    old-format records name it on the top level: usage.record carries
    model (e.g. kimi-code/k3), llm.request carries both model and
    modelAlias, and config.update carries modelAlias. The nested event
    block is consulted as a fallback.
    """
    provider = record.get("providerData") if isinstance(record.get("providerData"), dict) else {}
    for value in (
        record.get("model"),
        record.get("modelAlias"),
        provider.get("model"),
    ):
        if value:
            return str(value)
    event = record.get("event") if isinstance(record.get("event"), dict) else {}
    for value in (event.get("model"), event.get("modelAlias")):
        if value:
            return str(value)
    return None


def _is_system_placeholder_title(text) -> bool:
    """True when a candidate title / user message is system-injected
    scaffolding rather than a real user-authored title."""
    if not isinstance(text, str) or not text.strip():
        return False
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return True
    lowered = text[:120].lower()
    return "agents.md" in lowered or "instructions for" in lowered


def _project_cwd(relative_path: str | None) -> str | None:
    """Restore the working directory from a Workbuddy project dir name.

    The native project directory is escaped like `c-Users-li-Desktop-novel-mind`
    (the working path with each separator escaped to a dash). When the first
    path component matches a single-letter drive prefix it is restored to a
    Windows path (`C:\\Users\\li\\Desktop\\novel-mind`); otherwise as-is.
    """
    if not relative_path:
        return None
    norm = relative_path.replace("\\", "/").split("/")
    for comp in norm[:-1]:  # project dir is a parent of the session file
        if len(comp) >= 3 and comp[1] == "-" and comp[0].isalpha():
            parts = comp.split("-")
            if len(parts) >= 2 and len(parts[0]) == 1 and parts[0].isalpha():
                drive = parts[0].upper() + ":"
                return drive + "\\" + "\\".join(parts[1:])
            return comp
    return None


_USAGE_KEYS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens",
                     "inputOther", "input"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens",
                      "completionTokens", "output", "outputOther"),
    "cache_read": ("cache_read", "cacheRead", "inputCacheRead"),
    "cache_write": ("cache_write", "cacheWrite", "inputCacheCreation"),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def _usage_sources(record: dict, nested: dict) -> list[dict]:
    """Usage-bearing dicts worth scanning for token counts.

    Real Workbuddy exports place token counts under ``message.usage``
    (snake_case) and ``providerData.usage`` / ``providerData.rawUsage``
    (camelCase and snake_case respectively), in addition to the top-level
    ``input_tokens``/``output_tokens`` (and Kimi's ``event`` block).
    """
    # Real Kimi wire carries its counters in record.usage (usage.record) and
    # in the step.end event's usage block ({inputOther, output, inputCacheRead,
    # inputCacheCreation}); scan those dicts as well.
    sources = [record, nested]
    record_usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    if record_usage:
        sources.append(record_usage)
    nested_usage = nested.get("usage") if isinstance(nested.get("usage"), dict) else {}
    if nested_usage:
        sources.append(nested_usage)
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    if isinstance(message.get("usage"), dict):
        sources.append(message["usage"])
    provider = record.get("providerData") if isinstance(record.get("providerData"), dict) else {}
    if isinstance(provider.get("usage"), dict):
        sources.append(provider["usage"])
    if isinstance(provider.get("rawUsage"), dict):
        sources.append(provider["rawUsage"])
    return sources


def _usage_text(record: dict, nested: dict) -> str | None:
    """Machine-parsable token summary from a usage/function_call_result record.

    Returns e.g. `input_tokens=123 output_tokens=45 cache_read=6 cache_write=7`
    or `None` when no token counts are present.
    """
    def lookup(source: dict, label: str):
        for key in _USAGE_KEYS[label]:
            value = source.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        return None

    sources = _usage_sources(record, nested)
    pairs: list[str] = []
    for label in _USAGE_KEYS:
        value = None
        for source in sources:
            value = lookup(source, label)
            if value is not None:
                break
        if value is not None:
            pairs.append(f"{label}={value}")
    return " ".join(pairs) if pairs else None


def _is_subagent_artifact(artifact) -> bool:
    """True when an artifact lives under a native subagents/ directory."""
    parts = (artifact.relative_path or "").replace("\\", "/").split("/")
    return "subagents" in parts


def _session_lifecycle_event(artifact, *, family: str, session_id: str, contract_version: str,
                             locator: str, native_session: str | None = None,
                             occurred_at: str | None = None) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(family, artifact.artifact_id, contract_version,
                               native_session or "session", kind=EventKind.SESSION_LIFECYCLE,
                               session_id=session_id, native_locator=locator),
        session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
        provenance=Provenance(artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                              native_locator=locator, native_session_id=native_session,
                              native_event_id=native_session, contract_version=contract_version),
        fidelity=_fidelity(), occurred_at=occurred_at,
        content=None, summary="session lifecycle",
    )

class _Family:
    """Shared JSONL adapter machinery for one family of this module."""

    def __init__(self, family: str, *, markers: tuple[str, ...], kinds: dict | None):
        self.family = family
        self.markers = markers
        self.kinds = kinds or {}

    def capability(self) -> CapabilityDescriptor:
        kinds = {k for k in (self.kinds.values() if self.kinds else ()) if k is not None}
        if self.family == "workbuddy":
            kinds |= {EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE,
                      EventKind.REASONING, EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
            relations = {RelationKind.CALL_RESULT}
        else:  # kimi / kimi-work
            kinds |= {EventKind.TURN_BOUNDARY, EventKind.LOOP_BOUNDARY,
                      EventKind.FILE_CONTEXT, EventKind.USER_MESSAGE,
                      EventKind.ASSISTANT_MESSAGE,
                      EventKind.TOOL_CALL, EventKind.TOOL_RESULT,
                      EventKind.COMPACTION_SUMMARY}
            relations = {RelationKind.CALL_RESULT}
        kinds.add(EventKind.SESSION_LIFECYCLE)
        kinds.add(EventKind.SUBAGENT_BOUNDARY)
        kinds.add(EventKind.USAGE)
        kinds.add(EventKind.UNKNOWN_NATIVE)
        relations.add(RelationKind.SUBAGENT)
        return CapabilityDescriptor(
            family=self.family, adapter_version=ADAPTER_VERSION,
            contract_version=CONTRACT_VERSION,
            supported_event_kinds=tuple(sorted(kinds, key=lambda k: k.value)),
            supported_relation_kinds=tuple(sorted(relations, key=lambda r: r.value)),
            fidelity_dimensions=tuple(FidelityDimension),
            capabilities={
                "native_shape": "jsonl_event_stream",
                "lifecycle": "loop_and_task_boundaries" if self.family != "workbuddy" else "call_result_pairing",
            },
        )

    def detect(self, artifact: SourceArtifact, *, artifact_root: Path) -> bool:
        if not (artifact.relative_path or "").lower().endswith(".jsonl"):
            return False
        try:
            with artifact_bytes_path(artifact_root, artifact).open("r", encoding="utf-8") as h:
                for raw in h:
                    line = raw.strip()
                    if line and any(m in line for m in self.markers):
                        return True
        except OSError:
            return False
        return False

    def _event(self, artifact, *, session_id, kind, locator, native_id=None,
               occurred_at=None, content=None, summary=None, fidelity=None,
               native_session=None, field_dispositions=()) -> TypedEvent:
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
            content=content, summary=summary,
            field_dispositions=tuple(field_dispositions),
        )

    def _record_kind(self, record: dict) -> EventKind | None:
        if self.family == "workbuddy":
            rtype = record.get("type")
            if rtype == "message":
                return EventKind.USER_MESSAGE if record.get("role") == "user" else (
                    EventKind.ASSISTANT_MESSAGE if record.get("role") == "assistant" else None)
            if rtype == "file-history-snapshot":
                # ambient file/context event, not a user/assistant message
                return EventKind.FILE_CONTEXT
            return _WORKBUDDY_KINDS.get(rtype)
        if _envelope(record) is not None:
            return self._record_kind_envelope(record)
        # New-format non-event control records (e.g. journal_header) are session
        # scaffolding, not unknown noise.
        if record.get("kind") == "journal_header":
            return EventKind.SESSION_LIFECYCLE
        rtype = record.get("type")
        nested = record.get("event") if isinstance(record.get("event"), dict) else {}
        nested_type = nested.get("type")
        if rtype == "context.append_loop_event" and nested_type == "content.part":
            part = nested.get("part") if isinstance(nested.get("part"), dict) else {}
            return {
                "text": EventKind.ASSISTANT_MESSAGE,
                "think": EventKind.REASONING,
            }.get(part.get("type"), EventKind.UNKNOWN_NATIVE)
        return {
            "metadata": EventKind.SESSION_LIFECYCLE,
            "turn_start": EventKind.TURN_BOUNDARY,
            "turn.prompt": EventKind.USER_MESSAGE,
            "turn.cancel": EventKind.TURN_BOUNDARY,
            "user_prompt": EventKind.USER_MESSAGE,
            "assistant_message": EventKind.ASSISTANT_MESSAGE,
            "loop_iteration": EventKind.LOOP_BOUNDARY,
            "context_append": EventKind.FILE_CONTEXT,
            "context.append_message": EventKind.FILE_CONTEXT,
            "context.append_loop_event": {
                "step.begin": EventKind.LOOP_BOUNDARY,
                "step.end": EventKind.LOOP_BOUNDARY,
                "tool.call": EventKind.TOOL_CALL,
                "tool.result": EventKind.TOOL_RESULT,
            }.get(nested_type, EventKind.UNKNOWN_NATIVE),
            "usage.record": EventKind.USAGE,
            "full_compaction.begin": EventKind.COMPACTION_SUMMARY,
            "context.apply_compaction": EventKind.COMPACTION_SUMMARY,
            "full_compaction.complete": EventKind.COMPACTION_SUMMARY,
            "task.started": EventKind.LOOP_BOUNDARY,
            "task.terminated": EventKind.LOOP_BOUNDARY,
            "turn.steer": EventKind.TURN_BOUNDARY,
            "goal.create": EventKind.LOOP_BOUNDARY,
            "goal.update": EventKind.LOOP_BOUNDARY,
            "goal.clear": EventKind.LOOP_BOUNDARY,
            "task_complete": EventKind.TURN_BOUNDARY,
        }.get(rtype)

    def _record_kind_envelope(self, record: dict) -> EventKind | None:
        """Classify a new-format (envelope) kimi/kimi-work event."""
        env = _envelope(record) or {}
        return _ENVELOPE_KINDS.get(env.get("type"))

    def _adapt_envelope_record(self, env: dict, record: dict, *, artifact,
                               session_id, locator) -> TypedEvent | None:
        """Adapt a new-format kimi/kimi-work journal event from its envelope."""
        etype = env.get("type")
        payload = env.get("payload") if isinstance(env.get("payload"), dict) else {}
        ts = _timestamp(env.get("timestamp"))
        sid = env.get("session_id")
        kind = _ENVELOPE_KINDS.get(etype)
        seq = env.get("seq")

        # Stable native id per envelope kind.
        native_id = None
        if etype in ("tool.call.started", "tool.result"):
            native_id = payload.get("toolCallId")
            if kind is EventKind.TOOL_RESULT and native_id:
                native_id = f"{native_id}#result"
        elif etype and etype.startswith("subagent."):
            native_id = payload.get("subagentId")
        elif etype == "error":
            native_id = seq
        elif etype is not None:
            native_id = seq

        # Build a bounded summary from the payload depending on the kind.
        summary = None
        content = None
        content_disp = ()
        if kind is EventKind.TOOL_CALL:
            summary = str(payload.get("name") or payload.get("description") or "")[:2048] or None
            args_text = _payload_str(payload.get("args") or payload.get("arguments"))
            if args_text is not None:
                content, content_disp = _capped(
                    args_text, _TOOL_INPUT_CAP, "args",
                    "tool input truncated; full text exceeds content cap")
        elif kind is EventKind.TOOL_RESULT:
            summary = str(payload.get("output") or "")[:2048] or None
            output_raw = payload.get("output")
            if isinstance(output_raw, dict):
                output_raw = (output_raw.get("text") if output_raw.get("text") is not None
                              else output_raw.get("output"))
            output_text = _payload_str(output_raw)
            if output_text is not None:
                content, content_disp = _capped(
                    output_text, _TOOL_OUTPUT_CAP, "output",
                    "tool output truncated; full text exceeds content cap")
        elif etype == "context.spliced":
            summary = _spliced_text(payload)
        elif kind is EventKind.SUBAGENT_BOUNDARY:
            summary = str(payload.get("subagentId") or "")[:2048] or None
        elif kind is EventKind.COMPACTION_SUMMARY:
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            summary = str(result.get("summary") or "")[:2048] or None
            if not summary and etype == "compaction.completed":
                summary = None
        elif etype == "error":
            summary = str(payload.get("message") or "")[:2048] or None
        elif etype in ("turn.started", "turn.ended"):
            pass
        elif "task." in (etype or "") and "taskId" in payload:
            summary = str(payload.get("description") or payload.get("taskId"))[:2048] or None

        if kind is None:
            unknown_text = _payload_str(payload if payload else env)
            unknown_content = None
            unknown_disp = ()
            if unknown_text is not None:
                unknown_content, unknown_disp = _capped(
                    unknown_text, _UNKNOWN_CAP, "payload",
                    "unknown native record truncated; bounded preservation")
            return self._event(
                artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                locator=locator, native_id=native_id, occurred_at=ts,
                content=unknown_content,
                fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                   RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                   CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                native_session=sid, field_dispositions=unknown_disp,
            )

        return self._event(
            artifact, session_id=session_id, kind=kind,
            locator=locator, native_id=native_id, occurred_at=ts,
            content=content, summary=summary, native_session=sid,
            field_dispositions=content_disp,
        )

    def _adapt_record(self, record: dict, artifact, *, session_id, locator) -> TypedEvent | None:
        if _envelope(record) is not None:
            return self._adapt_envelope_record(
                _envelope(record), record, artifact=artifact,
                session_id=session_id, locator=locator,
            )
        kind = self._record_kind(record)
        ts = _record_time(record)
        nested = record.get("event") if isinstance(record.get("event"), dict) else {}
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        sid = record.get("session_id") or record.get("sessionId")
        mid = (
            record.get("message_id") or record.get("task_id") or record.get("id")
            or nested.get("id") or nested.get("call_id") or nested.get("callId")
        )
        if kind is None:
            unknown_text = _payload_str(record)
            unknown_content = None
            unknown_disp = ()
            if unknown_text is not None:
                unknown_content, unknown_disp = _capped(
                    unknown_text, _UNKNOWN_CAP, "record",
                    "unknown native record truncated; bounded preservation")
            return self._event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                               locator=locator, native_id=mid, occurred_at=ts,
                               content=unknown_content,
                               fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                                  RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                                  CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                               native_session=sid, field_dispositions=unknown_disp)
        if kind is EventKind.TOOL_RESULT and mid:
            mid = f"{mid}#result"
        part = nested.get("part") if isinstance(nested.get("part"), dict) else {}
        # Round-4 fix: extract native tool/reasoning payloads as full-fidelity
        # event content (kimi wire loop events + workbuddy flat records).
        native_content = None
        native_disp = ()
        if kind is EventKind.TOOL_CALL:
            raw = nested.get("args")
            if raw is None:
                raw = record.get("arguments")
            if raw is None:
                raw = nested.get("arguments")
            args_text = _payload_str(raw)
            if args_text is not None:
                native_content, native_disp = _capped(
                    args_text, _TOOL_INPUT_CAP, "args",
                    "tool input truncated; full text exceeds content cap")
        elif kind is EventKind.TOOL_RESULT:
            raw = nested.get("result")
            if raw is None:
                raw = record.get("output")
            if isinstance(raw, dict):
                inner = (raw.get("text") if raw.get("text") is not None
                         else raw.get("output"))
                if inner is not None:
                    raw = inner
            result_text = _payload_str(raw)
            if result_text is not None:
                native_content, native_disp = _capped(
                    result_text, _TOOL_OUTPUT_CAP, "output",
                    "tool output truncated; full text exceeds content cap")
        elif kind is EventKind.REASONING:
            raw = part.get("think")
            if raw is None:
                raw = record.get("rawContent")
            think_text = _block_text(raw)
            if think_text is None:
                think_text = _payload_str(raw)
            if think_text is not None:
                native_content, native_disp = _capped(
                    think_text, _REASONING_CAP, "rawContent",
                    "reasoning truncated; full text exceeds content cap")
        elif kind is None or kind is EventKind.UNKNOWN_NATIVE:
            # Preserve unmodelled native records (kimi server journal events)
            # as bounded content instead of dropping the payload entirely.
            unknown_text = _payload_str(record)
            if unknown_text is not None:
                native_content, native_disp = _capped(
                    unknown_text, _UNKNOWN_CAP, "record",
                    "unknown native record truncated; bounded preservation")
        summary = str(record.get("result") or "")[:2048] or None
        if summary is None:
            summary = _text_blocks(record.get("content"))
        if summary is None:
            summary = _text_blocks(message.get("content"))
        if summary is None:
            summary = _text_blocks(nested.get("content"))
        if summary is None:
            summary = _text_blocks(record.get("input"))
        if summary is None:
            summary = _text_blocks(part.get("text"))
        if summary is None and isinstance(nested.get("text"), str):
            summary = nested["text"][:2048]
        if kind is EventKind.COMPACTION_SUMMARY:
            summary = str(
                record.get("summary") or record.get("contextSummary") or ""
            )[:2048] or summary
        if kind is EventKind.USAGE and summary is None:
            summary = _usage_text(record, nested)
        if record.get("type") == "context.append_message":
            role = message.get("role")
            if role == "user":
                kind = EventKind.USER_MESSAGE
            elif role == "assistant":
                kind = EventKind.ASSISTANT_MESSAGE
        is_message = kind in {
            EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE,
            EventKind.DEVELOPER_MESSAGE,
            EventKind.SYSTEM_MESSAGE,
        }
        # Message bodies keep the exact native text; the bounded summary is a
        # fallback only. USAGE events carry the machine-parseable token text
        # in ``summary`` and never as a message body.
        content = None
        if is_message:
            content = _text_blocks(record.get("content"))
            if content is None:
                content = _text_blocks(message.get("content"))
            if content is None:
                content = _text_blocks(nested.get("content"))
            if content is None:
                content = _text_blocks(record.get("input"))
            if content is None:
                content = _text_blocks(part.get("text"))
            if content is None and isinstance(record.get("text"), str):
                content = record["text"][:2048]
            if content is None:
                content = summary
        elif native_content is not None:
            content = native_content
        return self._event(
            artifact, session_id=session_id, kind=kind, locator=locator,
            native_id=mid, occurred_at=ts,
            content=content,
            summary=None if is_message else summary,
            native_session=sid,
            field_dispositions=native_disp,
        )

    def adapt(self, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
        artifacts = list(artifact_set.artifacts)
        if not artifacts:
            raise EventContractError(f"{self.family} adapter requires at least one artifact")
        main_artifacts = [a for a in artifacts if not _is_subagent_artifact(a)]
        sub_artifacts = sorted(
            (a for a in artifacts if _is_subagent_artifact(a)),
            key=lambda a: a.relative_path or "",
        )
        if len(main_artifacts) != 1:
            raise EventContractError(
                f"{self.family} adapter requires exactly one main (non-subagent) "
                f"artifact, got {len(main_artifacts)}"
            )
        artifact = main_artifacts[0]
        records = list(iter_jsonl_lines(artifact_root / artifact.content_hash[:32]))

        session_id = make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                   None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
        events: list[TypedEvent] = []
        relations: list[EventRelation] = []
        warnings: list[str] = []
        by_call_id: dict[str, tuple[TypedEvent | None, TypedEvent | None]] = {}


        def _record_session_id(r: dict) -> str | None:
            sid = r.get("session_id") or r.get("sessionId")
            if sid:
                return sid
            env = r.get("envelope")
            if isinstance(env, dict):
                return env.get("session_id")
            return None


        native_session = next(
            (_record_session_id(r) for r in records if _record_session_id(r)),
            Path(artifact.relative_path).stem,
        )
        cwd = _project_cwd(artifact.relative_path)
        title: str | None = None

        # The main session always owns an explicit lifecycle anchor so subagent
        # relations can target it deterministically.
        _first_ts = None
        if records:
            _env0 = _envelope(records[0])
            _first_ts = (
                _timestamp(_env0.get("timestamp")) if _env0 is not None
                else _record_time(records[0])
            )
        events.append(_session_lifecycle_event(
            artifact, family=self.family, session_id=session_id,
            contract_version=CONTRACT_VERSION,
            locator=f"{artifact.relative_path}#session", native_session=native_session,
            occurred_at=_first_ts,
        ))

        sub_boundaries: dict[str, TypedEvent] = {}
        for lineno, record in enumerate(records, start=1):
            locator = f"{artifact.relative_path}#L{lineno}"
            ev = self._adapt_record(record, artifact, session_id=session_id, locator=locator)
            if ev is None:
                continue
            events.append(ev)
            nested = record.get("event") if isinstance(record.get("event"), dict) else {}
            env = _envelope(record)
            if env is not None:
                env_payload = env.get("payload") if isinstance(env.get("payload"), dict) else {}
            else:
                env_payload = {}
            if (title is None and ev.kind is EventKind.USER_MESSAGE and ev.content
                    and not _is_system_placeholder_title(ev.content)):
                title = ev.content[:120]
            # New-format session.meta.updated carries the real conversation title.
            if env is not None and env.get("type") == "session.meta.updated":
                meta_title = env_payload.get("title") or (
                    env_payload.get("patch") or {}).get("title")
                if meta_title and not _is_system_placeholder_title(meta_title):
                    title = str(meta_title)[:120]
            if self.family == "workbuddy":
                call_id = record.get("call_id") or record.get("callId")
                if call_id:
                    start, _end = by_call_id.setdefault(call_id, (None, None))
                    if ev.kind is EventKind.TOOL_CALL:
                        by_call_id[call_id] = (ev, _end)
                    elif ev.kind is EventKind.TOOL_RESULT:
                        by_call_id[call_id] = (start, ev)
            elif env is not None and env.get("type") in (
                    "tool.call.started", "tool.result"):
                call_id = env_payload.get("toolCallId")
                if call_id:
                    start, end = by_call_id.setdefault(str(call_id), (None, None))
                    if ev.kind is EventKind.TOOL_CALL:
                        by_call_id[str(call_id)] = (ev, end)
                    elif ev.kind is EventKind.TOOL_RESULT:
                        by_call_id[str(call_id)] = (start, ev)
            elif record.get("type") == "context.append_loop_event":
                call_id = (nested.get("toolCallId") or nested.get("parentUuid") or nested.get("uuid") or nested.get("call_id") or nested.get("callId") or nested.get("id"))
                if call_id:
                    start, end = by_call_id.setdefault(str(call_id), (None, None))
                    if ev.kind is EventKind.TOOL_CALL:
                        by_call_id[str(call_id)] = (ev, end)
                    elif ev.kind is EventKind.TOOL_RESULT:
                        by_call_id[str(call_id)] = (start, ev)
            # New-format inline subagents: track one boundary per subagentId so a
            # child -> parent SUBAGENT relation can be emitted once per child.
            if env is not None and ev.kind is EventKind.SUBAGENT_BOUNDARY:
                sub_id = env_payload.get("subagentId")
                if sub_id:
                    sub_boundaries.setdefault(str(sub_id), ev)
            # Token-bearing records (Workbuddy function_call_result / usage, and
            # Kimi/live step.end.usage) also yield a standalone USAGE event in
            # addition to their mapped kind; a record that already maps to USAGE
            # (e.g. usage.record) is skipped to avoid duplication.
            if self.family in ("workbuddy", "kimi", "kimi-work") and ev.kind is not EventKind.USAGE:
                nested_usage = env_payload if env is not None and env_payload else nested
                usage_text = _usage_text(record, nested_usage)
                if usage_text:
                    events.append(self._event(
                        artifact, session_id=session_id, kind=EventKind.USAGE,
                        locator=f"{locator}@usage", native_id=None, occurred_at=ev.occurred_at,
                        content=None, summary=usage_text, native_session=native_session,
                    ))

        if self.family == "workbuddy" or by_call_id:
            for call_id, (start, end) in by_call_id.items():
                if start is None or end is None:
                    warnings.append(f"call {call_id!r} missing start/result (partial)")
                    continue
                relations.append(EventRelation(
                    relation_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                              f"rel-call:{call_id}"),
                    source_event_id=start.event_id, target_event_id=end.event_id,
                    relation_kind=RelationKind.CALL_RESULT,
                ))

        # New-format inline subagents: one SUBAGENT relation child -> parent per
        # distinct subagentId seen in the stream (boundary -> main lifecycle).
        for sub_id, boundary_ev in sub_boundaries.items():
            relations.append(EventRelation(
                relation_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-subenv:{sub_id}"),
                source_event_id=boundary_ev.event_id,
                target_event_id=events[0].event_id,  # main session lifecycle
                relation_kind=RelationKind.SUBAGENT,
            ))

        # Discover subagents/agent-*.jsonl files: one SUBAGENT_BOUNDARY event per
        # child (summary = filename) and a SUBAGENT relation child -> parent.
        for sub_artifact in sub_artifacts:
            sub_filename = Path(sub_artifact.relative_path).name
            sub_records: list[dict] = []
            try:
                sub_records = list(iter_jsonl_lines(artifact_root / sub_artifact.content_hash[:32]))
            except OSError:
                warnings.append(f"subagent artifact {sub_artifact.artifact_id} unreadable")
                continue
            child_native = next(
                (r.get("session_id") or r.get("sessionId") for r in sub_records
                 if r.get("session_id") or r.get("sessionId")),
                Path(sub_artifact.relative_path).stem,
            )
            child_session_id = make_event_id(
                self.family, sub_artifact.artifact_id, CONTRACT_VERSION, None,
                kind=EventKind.SESSION_LIFECYCLE, native_locator="session",
            )
            child_lifecycle = _session_lifecycle_event(
                sub_artifact, family=self.family, session_id=child_session_id,
                contract_version=CONTRACT_VERSION,
                locator=f"{sub_artifact.relative_path}#session", native_session=child_native,
                occurred_at=_record_time(sub_records[0]) if sub_records else None,
            )
            events.append(child_lifecycle)
            events.append(self._event(
                sub_artifact, session_id=child_session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                locator=f"{sub_artifact.relative_path}#boundary",
                native_id=sub_filename, occurred_at=child_lifecycle.occurred_at,
                content=None, summary=sub_filename, native_session=child_native,
            ))
            relations.append(EventRelation(
                relation_id=make_event_id(self.family, sub_artifact.artifact_id, CONTRACT_VERSION,
                                          "rel-subagent"),
                source_event_id=child_lifecycle.event_id,
                target_event_id=events[0].event_id,  # main session lifecycle
                relation_kind=RelationKind.SUBAGENT,
            ))

        unknown = sum(1 for e in events if e.kind is EventKind.UNKNOWN_NATIVE)
        if unknown:
            warnings.append(f"{unknown} unknown native record(s) preserved")

        sessions: list[AdaptedSession] = []
        if native_session:
            # Kimi/WB rarely change model mid-session; prefer the LAST record
            # that names one so an early default (config.update) is overridden
            # by the model actually used (usage.record / llm.request).
            model = None
            for _r in records:
                _candidate = _extract_model(_r)
                if _candidate:
                    model = _candidate
            sessions.append(AdaptedSession(
                session_id=session_id,
                provenance=Provenance(
                    artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                    native_locator=f"{artifact.relative_path}#session",
                    native_session_id=native_session, native_event_id=native_session,
                    contract_version=CONTRACT_VERSION,
                ),
                fidelity=_fidelity(), native_session_id=native_session,
                started_at=_session_time_bounds(records)[0] if records else None,
                ended_at=_session_time_bounds(records)[1] if records else None,
                cwd=cwd, model=model, title=title,
            ))

        return AdaptationResult(
            family=self.family, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
            artifacts=tuple(artifacts), events=tuple(events),
            fidelity=_fidelity(
                STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE,
                RELATION_COMPLETENESS=(
                    FidelityLevel.PARTIAL
                    if by_call_id and len(relations) != len(by_call_id)
                    else FidelityLevel.COMPLETE
                ),
            ),
            sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
        )


_FAMILIES = {
    "workbuddy": _Family("workbuddy", markers=("function_call_result",), kinds=_WORKBUDDY_KINDS),
    # The "kind":"event" marker covers the new-format journal stream
    # ({"kind":"event","seq":N,"envelope":{...}}).
    "kimi": _Family("kimi", markers=("loop_iteration", "context_append", "task_complete", "context.append_loop_event", "turn.prompt", "\"kind\":\"event\""), kinds=None),
    "kimi-work": _Family("kimi-work", markers=("loop_iteration", "context_append", "task_complete", "context.append_loop_event", "turn.prompt", "\"kind\":\"event\""), kinds=None),
}


def capability(family: str) -> CapabilityDescriptor:
    return _FAMILIES[family].capability()


def detect(family: str, artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    return _FAMILIES[family].detect(artifact, artifact_root=artifact_root)


def adapt(family: str, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    return _FAMILIES[family].adapt(artifact_set, artifact_root=artifact_root)