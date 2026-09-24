"""Phase 62-02: Copilot / vscode-copilot JSONL trace adapter.

Copilot exports turn start/end, assistant message and tool execution
start/complete records. Lifecycle events are paired by native IDs; a
missing ``tool_execution_complete`` or ``turn_end`` is tolerated as bounded
partial fidelity (never guessed). The family keeps a single capability
contract with an alias for vscode-copilot.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from pathlib import Path
import json

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

FAMILY = "copilot"
ADAPTER_VERSION = "1.2.0"
CONTRACT_VERSION = "2"

# Round-4 audit fix: tool arguments/results were never stored as event content
# (name-only summaries), and sessions without session.info/model_change lost
# the model that tool.execution_complete records still carry.
_TOOL_INPUT_CAP = 50_000
_TOOL_OUTPUT_CAP = 100_000


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
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True) or None
    except (TypeError, ValueError):
        return str(value) or None


def _tool_content(data: dict, kind) -> tuple[str | None, tuple]:
    """Full tool arguments (TOOL_CALL) or result text (TOOL_RESULT), capped."""
    if kind is EventKind.TOOL_CALL:
        raw = data.get("arguments")
        if raw is None:
            raw = data.get("args")
        text = _payload_str(raw)
        if text is None:
            return None, ()
        return _capped(text, _TOOL_INPUT_CAP, "arguments",
                       "tool input truncated; full text exceeds content cap")
    if kind is EventKind.TOOL_RESULT:
        raw = data.get("result")
        if isinstance(raw, dict):
            inner = (raw.get("detailedContent") if raw.get("detailedContent")
                     else raw.get("content"))
            if inner is not None:
                raw = inner
        text = _payload_str(raw)
        if text is None:
            return None, ()
        return _capped(text, _TOOL_OUTPUT_CAP, "result",
                       "tool output truncated; full text exceeds content cap")
    return None, ()

_COMPLETE = {
    FidelityDimension.SOURCE_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.ORDERING_CONFIDENCE: FidelityLevel.COMPLETE,
    FidelityDimension.RELATION_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.CONTENT_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.COMPACTION_VISIBILITY: FidelityLevel.COMPLETE,
    FidelityDimension.NATIVE_ID_STABILITY: FidelityLevel.COMPLETE,
}

_KINDS = {
    # legacy underscore keys (Phase 62 fixture shape)
    "turn_start": EventKind.TURN_BOUNDARY,
    "turn_end": EventKind.TURN_BOUNDARY,
    "assistant_message": EventKind.ASSISTANT_MESSAGE,
    "tool_execution_start": EventKind.TOOL_CALL,
    "tool_execution_complete": EventKind.TOOL_RESULT,
    # real vscode-copilot dotted event stream
    "session.start": EventKind.SESSION_LIFECYCLE,
    "session.shutdown": EventKind.SESSION_LIFECYCLE,
    "session.info": EventKind.SESSION_LIFECYCLE,
    "session.model_change": EventKind.SESSION_LIFECYCLE,
    "session.compaction_start": EventKind.COMPACTION_SUMMARY,
    "session.compaction_complete": EventKind.COMPACTION_SUMMARY,
    "user.message": EventKind.USER_MESSAGE,
    "assistant.turn_start": EventKind.TURN_BOUNDARY,
    "assistant.message": EventKind.ASSISTANT_MESSAGE,
    "assistant.turn_end": EventKind.TURN_BOUNDARY,
    "tool.execution_start": EventKind.TOOL_CALL,
    "tool.execution_complete": EventKind.TOOL_RESULT,
    "subagent.started": EventKind.SUBAGENT_BOUNDARY,
    "subagent.failed": EventKind.SUBAGENT_BOUNDARY,
    "subagent.completed": EventKind.SUBAGENT_BOUNDARY,
    "usage": EventKind.USAGE,
    "usage.record": EventKind.USAGE,
}


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE, EventKind.TURN_BOUNDARY,
            EventKind.ASSISTANT_MESSAGE, EventKind.USER_MESSAGE,
            EventKind.TOOL_CALL, EventKind.TOOL_RESULT,
            EventKind.COMPACTION_SUMMARY, EventKind.SUBAGENT_BOUNDARY,
            EventKind.USAGE, EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(RelationKind.CALL_RESULT,),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "jsonl_trace",
            "aliases": "vscode-copilot",
            "pairing": "native_tool_id_lifecycle",
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    suffix = Path(artifact.relative_path or "").suffix.lower()
    if suffix not in (".jsonl", ".json"):
        return False
    try:
        path = artifact_bytes_path(artifact_root, artifact)
        if suffix == ".json":
            doc = json.loads(path.read_text(encoding="utf-8"))
            return isinstance(doc, dict) and isinstance(doc.get("requests"), list)
        with path.open("r", encoding="utf-8") as h:
            for raw in h:
                line = raw.strip()
                if not line:
                    continue
                return (
                    '"turn_start"' in line or '"tool_execution_start"' in line
                    or '"assistant.turn_start"' in line or '"session.start"' in line
                )
    except OSError:
        return False
    return False


def _event(artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
           content=None, summary=None, fidelity=None, native_session=None,
           field_dispositions=()) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
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


_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "completion_tokens", "output"),
    "cache_read": ("cache_read", "cacheRead"),
    "cache_write": ("cache_write", "cacheWrite"),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def _usage_summary(data: dict) -> str | None:
    # Canonical machine-parseable usage summary from token fields (if any).
    # Native counters are mapped onto input_tokens=X output_tokens=Y
    # [cache_read=Z cache_write=W] (only present, integer values).
    counters: dict[str, int] = {}

    def _collect(src: str, value) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            for canonical, aliases in _USAGE_ALIASES.items():
                if src in aliases:
                    counters.setdefault(canonical, int(value))
                    return
        elif isinstance(value, dict):
            for sub, subvalue in value.items():
                _collect(sub, subvalue)

    for key, value in data.items():
        if key in _USAGE_ALIASES or any(
            key in aliases for aliases in _USAGE_ALIASES.values()
        ):
            _collect(key, value)
    if not counters:
        return None
    return " ".join(
        f"{key}={counters[key]}" for key in _USAGE_ALIASES if key in counters
    )


def _adapt_record(record: dict, artifact, *, session_id, locator) -> TypedEvent | None:
    kind = _KINDS.get(record.get("type"))
    ts = record.get("timestamp")
    data = record.get("data") if isinstance(record.get("data"), dict) else record
    sid = record.get("session_id") or data.get("sessionId")
    native_id = (
        record.get("id") or data.get("toolCallId") or data.get("toolId")
        or record.get("tool_id") or data.get("messageId")
        or record.get("message_id") or data.get("turnId")
        or record.get("turn_id") or data.get("interactionId")
        or data.get("agentName")
    )
    if kind is None:
        return _event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                      locator=locator, native_id=record.get("id"), occurred_at=ts,
                      fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                         RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                         CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                      native_session=sid)
    tool_id = (
        record.get("tool_id")
        or data.get("toolId")
        or data.get("toolCallId")
    )
    if kind is EventKind.TOOL_RESULT and tool_id:
        # start and complete share the native tool id; make_event_id omits
        # kind when a native id is present, so disambiguate the completion.
        native_id = f"{tool_id}#complete"
    is_message = kind in {
        EventKind.USER_MESSAGE,
        EventKind.ASSISTANT_MESSAGE,
        EventKind.DEVELOPER_MESSAGE,
        EventKind.SYSTEM_MESSAGE,
    }
    raw_content = data.get("content")
    content = None if raw_content is None else str(raw_content)
    if kind is EventKind.USAGE:
        # machine-parseable summary of any token/usage fields (only where present)
        usage_summary = _usage_summary(data)
        return _event(
            artifact, session_id=session_id, kind=kind, locator=locator,
            native_id=native_id or "usage",
            occurred_at=ts, content=None, summary=usage_summary,
            fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
            native_session=sid,
        )
    if kind is EventKind.SUBAGENT_BOUNDARY:
        # boundary event whose summary names the sub-agent id (the tool-call
        # id it was spawned under), falling back to the agent name.
        return _event(
            artifact, session_id=session_id, kind=kind, locator=locator,
            native_id=native_id, occurred_at=ts, content=None,
            summary=str(
                data.get("toolCallId")
                or data.get("agentName")
                or native_id or ""
            )[:256] or None,
            native_session=sid,
        )
    if kind is EventKind.COMPACTION_SUMMARY:
        # carry any compaction summary text; a bare start/end marker uses the
        # native event id as a stable, name-only summary.
        return _event(
            artifact, session_id=session_id, kind=kind, locator=locator,
            native_id=native_id, occurred_at=ts, content=None,
            summary=str(data.get("summary") or native_id or "")[:2048] or None,
            native_session=sid,
        )
    if kind is EventKind.TURN_BOUNDARY:
        # turn boundaries are structural markers; name them by turn id when present
        turn_id = data.get("turnId") or data.get("turn_id")
        return _event(
            artifact, session_id=session_id, kind=kind, locator=locator,
            native_id=native_id, occurred_at=ts, content=None,
            summary=(f"turn:{turn_id}" if turn_id else None),
            native_session=sid,
        )
    tool_content, tool_disp = _tool_content(data, kind)
    return _event(
        artifact, session_id=session_id, kind=kind, locator=locator,
        native_id=native_id, occurred_at=ts,
        content=(content if is_message else tool_content),
        summary=(
            None if is_message else
            str(raw_content or data.get("name") or data.get("toolName") or "")[:2048] or None
        ),
        native_session=sid,
        field_dispositions=tool_disp,
    )


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one immutable Copilot trace into typed events/relations."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    records, malformed = _load_records(
        artifact_root / artifact.content_hash[:32], artifact.relative_path
    )

    session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    if malformed:
        warnings.append(f"{malformed} malformed/native-corrupt record(s) skipped")
    tool_starts: dict[str, TypedEvent] = {}
    tool_ends: dict[str, TypedEvent] = {}
    native_session = next((
        r.get("session_id")
        or ((r.get("data") or {}).get("sessionId") if isinstance(r.get("data"), dict) else None)
        for r in records
        if r.get("session_id") or (
            isinstance(r.get("data"), dict) and (r.get("data") or {}).get("sessionId")
        )
    ), Path(artifact.relative_path).stem)

    for lineno, record in enumerate(records, start=1):
        ev = _adapt_record(record, artifact, session_id=session_id,
                           locator=f"{artifact.relative_path}#L{lineno}")
        if ev is None:
            continue
        events.append(ev)
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        tool_id = (
            record.get("tool_id") or data.get("toolId") or data.get("toolCallId")
        )
        if not tool_id:
            continue
        if ev.kind is EventKind.TOOL_CALL:
            tool_starts[tool_id] = ev
        elif ev.kind is EventKind.TOOL_RESULT:
            tool_ends[tool_id] = ev

    for tool_id, start in tool_starts.items():
        end = tool_ends.get(tool_id)
        if end is None:
            warnings.append(f"tool {tool_id!r} has no completion (partial)")
            # an orphaned TOOL_CALL advertises bounded fidelity on itself
            # (partial) so downstream never mistakes it for a complete pair.
            partial = _fidelity(
                STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                RELATION_COMPLETENESS=FidelityLevel.PARTIAL,
            )
            events[events.index(start)] = _replace(start, fidelity=partial)
            continue
        relations.append(EventRelation(
            relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                      f"rel-call:{tool_id}"),
            source_event_id=start.event_id, target_event_id=end.event_id,
            relation_kind=RelationKind.CALL_RESULT,
        ))

    unknown = sum(1 for e in events if e.kind is EventKind.UNKNOWN_NATIVE)
    if unknown:
        warnings.append(f"{unknown} unknown native record(s) preserved")

    sessions: list[AdaptedSession] = []
    if native_session:
        started_at = _session_started_at(records)
        ended_at = _session_ended_at(records)
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=f"{artifact.relative_path}#session",
                native_session_id=native_session, native_event_id=native_session,
                contract_version=CONTRACT_VERSION,
            ),
            fidelity=_fidelity(), native_session_id=native_session,
            cwd=_session_cwd(records),
            git_branch=_session_branch(records),
            model=_session_model(records),
            title=_first_user_content(records),
            started_at=started_at,
            ended_at=ended_at,
        ))

    relation_loss = bool(tool_starts) and len(tool_starts) != len(relations)
    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events),
        fidelity=_fidelity(
            STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE,
            RELATION_COMPLETENESS=FidelityLevel.PARTIAL if relation_loss else FidelityLevel.COMPLETE,
        ),
        sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
    )


def _load_records(path: Path, relative_path: str) -> tuple[list[dict], int]:
    if Path(relative_path).suffix.lower() == ".jsonl":
        values = list(iter_jsonl_lines(path, strict=False))
        nonblank = sum(1 for line in path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines() if line.strip("\x00 \t\r\n"))
        return values, max(0, nonblank - len(values))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventContractError(f"{FAMILY} JSON artifact unreadable: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("requests"), list):
        raise EventContractError(f"{FAMILY} JSON export has no requests list")
    sid = str(doc.get("sessionId") or Path(relative_path).stem)
    records: list[dict] = [{
        "type": "session.start", "id": sid,
        "timestamp": doc.get("creationDate"), "data": {"sessionId": sid},
    }]
    for index, request in enumerate(doc["requests"]):
        if not isinstance(request, dict):
            continue
        message = request.get("message")
        if isinstance(message, dict):
            message = message.get("text")
        request_id = str(request.get("requestId") or f"request-{index}")
        records.append({
            "type": "user.message", "id": request_id,
            "timestamp": request.get("timestamp"),
            "data": {"sessionId": sid, "messageId": request_id, "content": message},
        })
        response_parts = request.get("response")
        text_parts: list[str] = []
        if isinstance(response_parts, list):
            for part in response_parts:
                if isinstance(part, dict) and isinstance(part.get("value"), str):
                    text_parts.append(part["value"])
        response_id = str(request.get("responseId") or f"{request_id}:response")
        records.append({
            "type": "assistant.message", "id": response_id,
            "timestamp": request.get("timestamp"),
            "data": {
                "sessionId": sid, "messageId": response_id,
                "content": "\n".join(text_parts),
            },
        })
    return records, 0


def _record_data(record: dict) -> dict:
    return record.get("data") if isinstance(record.get("data"), dict) else record


def _session_cwd(records: list[dict]) -> str | None:
    """Recover the working directory from a cwd field anywhere in the trace."""
    for record in records:
        if record.get("cwd"):
            return str(record["cwd"])
        data = _record_data(record)
        if data.get("cwd"):
            return str(data["cwd"])
        context = data.get("context")
        if isinstance(context, dict) and context.get("cwd"):
            return str(context["cwd"])
    return None


def _session_branch(records: list[dict]) -> str | None:
    """Recover the git branch from a branch field (e.g. session.start context)."""
    for record in records:
        if record.get("branch"):
            return str(record["branch"])
        data = _record_data(record)
        if data.get("branch"):
            return str(data["branch"])
        context = data.get("context")
        if isinstance(context, dict) and context.get("branch"):
            return str(context["branch"])
    return None


def _session_model(records: list[dict]) -> str | None:
    """Recover the model from session.info / session.model_change records."""
    for record in records:
        rtype = record.get("type")
        if rtype not in ("session.info", "session.model_change"):
            continue
        data = _record_data(record)
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            # e.g. "Model changed to: Gemini 3 Pro (Preview)"
            marker = "Model changed to:"
            if marker in message:
                return message.split(marker, 1)[1].strip()
            return message.strip()
        model = data.get("model") or data.get("modelId") or data.get("newModel")
        if model:
            return str(model)
    # Round-4 fix: sessions without session.info/model_change often still
    # carry the model id on tool.execution_complete records.
    for record in records:
        if record.get("type") != "tool.execution_complete":
            continue
        model = _record_data(record).get("model")
        if model:
            return str(model)
    return None


def _session_started_at(records: list[dict]) -> str | None:
    """started_at = the session.start record timestamp (creation time)."""
    for record in records:
        if record.get("type") == "session.start" and record.get("timestamp"):
            return str(record["timestamp"])
    return None


def _session_ended_at(records: list[dict]) -> str | None:
    """ended_at = last session.shutdown timestamp, else last trace timestamp."""
    last: str | None = None
    for record in records:
        if record.get("type") == "session.shutdown" and record.get("timestamp"):
            return str(record["timestamp"])
        if record.get("timestamp"):
            last = str(record["timestamp"])
    return last


def _first_user_content(records: list[dict]) -> str | None:
    """Title = first *genuine* user message, excluding injected system
    prompts (e.g. bracket-led [Assistant Rules] blocks), bounded to the
    first 120 chars."""
    for record in records:
        kind = _KINDS.get(record.get("type"))
        if kind is not EventKind.USER_MESSAGE:
            continue
        data = _record_data(record)
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        stripped = content.strip()
        if _looks_like_system_prompt(stripped):
            continue
        return stripped[:120]
    return None


def _looks_like_system_prompt(text: str) -> bool:
    """Heuristic for a system-injected user message that is not the real
    opening prompt: bracket-led directive blocks and common preamble words."""
    lowered = text[:120].lower()
    if text.lstrip().startswith("["):
        return True
    for marker in (
        "assistant rules", "system prompt", "begin system",
        "you are a coding agent", "the current datetime",
        "current_datetime",
    ):
        if marker in lowered:
            return True
    return False
