"""Phase 62-03: ZCode SQLite adapter (family ``zcode``).

ZCode stores conversation parts in a SQLite store with native trace and
turn IDs (62-RESEARCH format matrix). Capture uses the allowlisted online
backup seam (:func:`capture_sqlite`) so credential-adjacent tables are
dropped before publishing; this adapter reads ONLY the declared
conversation tables from the filtered artifact. Trace IDs are preserved as
session identity without making trace a universal concept (D-20);
text/reasoning/tool/step/compaction parts map to typed events and turn
membership relations.
"""

from __future__ import annotations

import sqlite3
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
from personal_knowledge.adapters.conversation_sources.time_utils import (
    normalize_timestamp,
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

FAMILY = "zcode"
ADAPTER_VERSION = "1.5.0"
CONTRACT_VERSION = "2"

ALLOWED_TABLES: tuple[str, ...] = ("conversation_traces", "conversation_parts")
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "conversation_traces": ("trace_id", "title", "created_at"),
    "conversation_parts": ("part_id", "trace_id", "turn_id", "part_type",
                           "role", "content", "created_at"),
}
LIVE_ALLOWED_TABLES: tuple[str, ...] = ("session", "message", "part")
LIVE_ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "session": ("id", "parent_id", "title", "time_created", "time_updated",
                   "time_compacting", "trace_id", "directory", "path"),
    "message": ("id", "session_id", "time_created", "time_updated", "data", "sequence"),
    "part": ("id", "message_id", "session_id", "time_created", "time_updated", "data", "sequence"),
}

_COMPLETE = {
    FidelityDimension.SOURCE_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.ORDERING_CONFIDENCE: FidelityLevel.COMPLETE,
    FidelityDimension.RELATION_COMPLETENESS: FidelityLevel.COMPLETE,
    FidelityDimension.CONTENT_AVAILABILITY: FidelityLevel.COMPLETE,
    FidelityDimension.COMPACTION_VISIBILITY: FidelityLevel.COMPLETE,
    FidelityDimension.NATIVE_ID_STABILITY: FidelityLevel.COMPLETE,
}

_PART_KINDS = {
    "text": None,  # decided by role
    "reasoning": EventKind.REASONING,
    "tool": EventKind.TOOL_CALL,
    "step": EventKind.TURN_BOUNDARY,
    "compaction": EventKind.COMPACTION_SUMMARY,
    "step-start": EventKind.TURN_BOUNDARY,
    "step-finish": EventKind.TURN_BOUNDARY,
    "file": EventKind.FILE_CONTEXT,
}

# Round-4 audit: tool state payloads (input/output) were never extracted and
# reasoning text was silently capped at 2048 inside ``summary``. Tool input is
# now event content (cap 50k), tool output becomes a TOOL_RESULT event (cap
# 100k) linked via CALL_RESULT, and reasoning text moves to content (cap 100k)
# — truncation is flagged via a field disposition instead of being silent.
_TOOL_INPUT_CAP = 50_000
_TOOL_OUTPUT_CAP = 100_000
_REASONING_CAP = 100_000


def _capped(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _payload_text(value) -> str | None:
    """Serialize a native tool payload (dict or str) as stable text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value) or None


def _truncation(field: str, reason: str) -> tuple[FieldDispositionRecord, ...]:
    return (FieldDispositionRecord(
        field_name=field, disposition=FieldDisposition.MAPPED, reason=reason,
    ),)


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "output"),
    "cache_read": ("cache_read", "cacheRead", "cached_input_tokens", "read"),
    "cache_write": ("cache_write", "cacheWrite", "cache_creation_input_tokens", "write"),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
}


def _usage_summary(data) -> str | None:
    """Machine-parseable canonical usage summary from any token-bearing payload (USAGE).

    Walks the payload and maps native counters - including the real ZCode
    "tokens" aggregate ({"total": ..., "input": ..., "output": ...,
    "cache": {"read": ..., "write": ...}}) - onto the canonical grammar
    "input_tokens=X output_tokens=Y [cache_read=Z cache_write=W]" (only
    present, integer values). Returns None when no counters are present.
    """
    counters: dict[str, int] = {}

    def walk(obj) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    for canonical, aliases in _USAGE_ALIASES.items():
                        if key in aliases:
                            counters.setdefault(canonical, int(value))
                            break
                elif isinstance(value, dict):
                    walk(value)

    walk(data)
    if not counters:
        return None
    return " ".join(
        f"{key}={counters[key]}" for key in _USAGE_ALIASES if key in counters
    )


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE, EventKind.REASONING,
            EventKind.TOOL_CALL, EventKind.TOOL_RESULT,
            EventKind.TURN_BOUNDARY,
            EventKind.COMPACTION_SUMMARY, EventKind.USAGE,
            EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(
            RelationKind.TURN_MEMBERSHIP, RelationKind.COMPACTED_RANGE,
            RelationKind.CALL_RESULT,
            RelationKind.SUBAGENT, RelationKind.BRANCH,
        ),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "sqlite_virtual_locator",
            "tables": ",".join(ALLOWED_TABLES),
            "trace_semantics": "session_identity_only",
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    if artifact.source_kind != "sqlite":
        return False
    try:
        con = sqlite3.connect(f"file:{artifact_bytes_path(artifact_root, artifact)}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('conversation_parts','part')"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(rows)


def _provenance(artifact: SourceArtifact, locator: str, *, session: str | None, native_id: str | None) -> Provenance:
    return Provenance(
        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
        native_locator=locator, native_session_id=session or None,
        native_event_id=native_id, contract_version=CONTRACT_VERSION,
    )


def _event(artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
           content=None, summary=None, fidelity=None, native_session=None,
           field_dispositions=()) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               native_id or locator, kind=kind, session_id=session_id),
        session_id=session_id, kind=kind,
        provenance=_provenance(artifact, locator, session=native_session, native_id=native_id),
        fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
        content=content, summary=summary,
        field_dispositions=tuple(field_dispositions),
    )


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


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one filtered ZCode snapshot into typed events/relations."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    blob = artifact_root / artifact.content_hash[:32]
    if artifact.source_kind != "sqlite":
        raise EventContractError(f"{FAMILY} adapter requires a sqlite artifact")

    try:
        con = sqlite3.connect(f"file:{blob}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            live = {"session", "message", "part"} <= tables
            traces = con.execute(
                "SELECT * FROM session" if live else "SELECT * FROM conversation_traces"
            ).fetchall()
            messages = con.execute("SELECT * FROM message").fetchall() if live else []
            parts = con.execute(
                "SELECT * FROM part" if live else "SELECT * FROM conversation_parts"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise EventContractError(f"{FAMILY} artifact unreadable: {exc}") from exc

    # Session end = last native activity across its messages + parts (time_updated
    # preferred, falling back to time_created / created_at), keyed by the native
    # session/trace id so AdaptedSession.ended_at is filled. Values sort lexically
    # within one source's consistent timestamp format (ISO or epoch-ms integers).
    last_activity_by_sid: dict[str, str] = {}

    def _fold_activity(sid: str, ts) -> None:
        if not ts:
            return
        text = str(ts)
        if text > last_activity_by_sid.get(sid, ""):
            last_activity_by_sid[sid] = text

    # Round-5 fix: the native session row's own time_updated can be touched
    # after the last message/part (e.g. compaction), so it must participate in
    # the end-time fold instead of deriving ended_at only from message/part
    # activity.
    for trace in traces:
        sid = str(trace["id"] if live else trace["trace_id"])
        if live:
            _fold_activity(sid, trace["time_updated"] or trace["time_created"])
        elif "updated_at" in trace.keys():
            _fold_activity(sid, trace["updated_at"] or trace["created_at"])
    for message in messages:
        sid = str(message["session_id"])
        _fold_activity(sid, message["time_updated"] or message["time_created"])
    for part in parts:
        sid = str(part["session_id"] if live else part["trace_id"])
        if live:
            _fold_activity(sid, part["time_updated"] or part["time_created"])
        else:
            _fold_activity(sid, part["created_at"])

    sessions: list[AdaptedSession] = []
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    by_part: dict[str, TypedEvent] = {}
    unknown = 0
    # first real (non-system) user-message text per native session, for the
    # title fallback when the stored title is absent or system scaffolding.
    first_user_text_by_sid: dict[str, str] = {}

    for trace in traces:
        sid = str(trace["id"] if live else trace["trace_id"])
        session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                   sid, kind=EventKind.SESSION_LIFECYCLE)
        raw_title = str(trace["title"])[:120] if trace["title"] else None
        # A stored title that is system-injected scaffolding is not a real
        # title; leave it to be filled from the first real user message.
        if raw_title and not _is_system_placeholder_title(raw_title):
            title = raw_title
        else:
            title = None
        # Working directory: real zcode stores it in directory/path (no
        # literal `cwd` column); synthetic fixtures use `cwd`. Prefer the
        # explicit `cwd` when present, then the native working-dir fields.
        cwd = None
        for _key in ("cwd", "directory", "path"):
            if _key in trace.keys() and trace[_key]:
                cwd = str(trace[_key])
                break
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=_provenance(artifact, f"{artifact.relative_path}#trace:{sid}",
                                   session=sid, native_id=sid),
            fidelity=_fidelity(
                RELATION_COMPLETENESS=FidelityLevel.PARTIAL if live else FidelityLevel.COMPLETE
            ), native_session_id=sid,
            started_at=normalize_timestamp(
                trace["time_created"] if live else trace["created_at"]
            ),
            ended_at=normalize_timestamp(last_activity_by_sid.get(sid)),
            title=title, cwd=cwd,
        ))
        events.append(_event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                             locator=f"{artifact.relative_path}#trace:{sid}", native_id=sid,
                             occurred_at=normalize_timestamp(
                                 trace["time_created"] if live else trace["created_at"]
                             ),
                             summary=title, native_session=sid))

    message_roles = {}
    if live:
        for message in messages:
            data = _json_object(message["data"])
            message_roles[str(message["id"])] = data.get("role")

    for part in parts:
        sid = str(part["session_id"] if live else part["trace_id"])
        session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                   sid, kind=EventKind.SESSION_LIFECYCLE)
        data = _json_object(part["data"]) if live else dict(part)
        ptype = data.get("type") if live else part["part_type"]
        kind = _PART_KINDS.get(ptype)
        part_id = str(part["id"] if live else part["part_id"])
        locator = f"{artifact.relative_path}#part:{part_id}"
        # Live tool parts carry call arguments and results inside
        # ``data.state`` (Round-4 fix): the part becomes a TOOL_CALL with the
        # serialized ``state.input`` as content plus a TOOL_RESULT event with
        # ``state.output``, linked by a CALL_RESULT relation.
        if (kind is EventKind.TOOL_CALL and live
                and isinstance(data.get("state"), dict)):
            state = data["state"]
            tool_name = str(data["tool"]) if data.get("tool") else None
            input_text = _payload_text(state.get("input"))
            input_disp = ()
            if input_text is not None:
                input_text, truncated = _capped(input_text, _TOOL_INPUT_CAP)
                if truncated:
                    input_disp = _truncation(
                        "input", "tool input truncated; full text exceeds content cap")
            call_ev = _event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=locator, native_id=part_id,
                occurred_at=normalize_timestamp(part["time_created"]),
                content=input_text, summary=tool_name,
                field_dispositions=input_disp, native_session=sid,
            )
            events.append(call_ev)
            by_part[part_id] = call_ev
            output_text = _payload_text(state.get("output"))
            if output_text is not None:
                output_disp = ()
                output_text, truncated = _capped(output_text, _TOOL_OUTPUT_CAP)
                if truncated:
                    output_disp = _truncation(
                        "output", "tool output truncated; full text exceeds content cap")
                result_ev = _event(
                    artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
                    locator=f"{locator}#result", native_id=f"{part_id}:result",
                    occurred_at=normalize_timestamp(
                        part["time_updated"] or part["time_created"]
                    ),
                    content=output_text, summary=tool_name,
                    field_dispositions=output_disp, native_session=sid,
                )
                events.append(result_ev)
                relations.append(EventRelation(
                    relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                              f"rel-call:{call_ev.event_id}"),
                    source_event_id=call_ev.event_id, target_event_id=result_ev.event_id,
                    relation_kind=RelationKind.CALL_RESULT,
                ))
            continue
        if kind is None:
            if ptype == "text":
                role = message_roles.get(str(part["message_id"])) if live else part["role"]
                kind = EventKind.USER_MESSAGE if role == "user" else (
                    EventKind.ASSISTANT_MESSAGE if role == "assistant" else None)
            if kind is None:
                unknown += 1
                ev = _event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                            locator=locator, native_id=part_id,
                            occurred_at=normalize_timestamp(
                                part["time_created"] if live else part["created_at"]
                            ),
                            fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                               RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                               CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                            native_session=sid)
                events.append(ev)
                by_part[part_id] = ev
                continue
        raw_content = data.get("text") if "text" in data else data.get("content")
        text = None if raw_content is None else str(raw_content)
        is_message = kind in {
            EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE,
            EventKind.DEVELOPER_MESSAGE,
            EventKind.SYSTEM_MESSAGE,
        }
        if kind is EventKind.REASONING:
            # Round-4 fix: reasoning text is full-fidelity content (cap 100k,
            # truncation dispositioned) instead of a silently capped summary.
            content = None
            summary = None
            reasoning_disp = ()
            if text is not None:
                content, truncated = _capped(text, _REASONING_CAP)
                if truncated:
                    reasoning_disp = _truncation(
                        "text", "reasoning truncated; full text exceeds content cap")
            ev = _event(
                artifact, session_id=session_id, kind=kind, locator=locator,
                native_id=part["id"] if live else part["part_id"],
                occurred_at=normalize_timestamp(
                    part["time_created"] if live else part["created_at"]
                ),
                content=content, summary=summary,
                field_dispositions=reasoning_disp, native_session=sid,
            )
        else:
            ev = _event(
                artifact, session_id=session_id, kind=kind, locator=locator,
                native_id=part["id"] if live else part["part_id"],
                occurred_at=normalize_timestamp(
                    part["time_created"] if live else part["created_at"]
                ),
                content=text if is_message else None,
                summary=None if is_message else (text[:2048] or None if text else None),
                native_session=sid,
            )
        events.append(ev)
        by_part[str(part["id"] if live else part["part_id"])] = ev
        if (kind is EventKind.USER_MESSAGE and text and text.strip()
                and not _is_system_placeholder_title(text)):
            first_user_text_by_sid.setdefault(sid, text.strip()[:120])
        usage_summary = _usage_summary(data)
        if usage_summary:
            events.append(_event(
                artifact, session_id=session_id, kind=EventKind.USAGE,
                locator=f"{locator}#usage", native_id=f"{part_id}:usage",
                occurred_at=normalize_timestamp(
                    part["time_created"] if live else part["created_at"]
                ),
                summary=usage_summary, native_session=sid,
            ))

    # Title fallback: sessions whose stored title is missing or system
    # scaffolding get the first real user-message text (capped at 120) so a
    # fixed-length source truncation/plugin prompt never becomes the title.
    for i, session in enumerate(sessions):
        if session.title is None and first_user_text_by_sid.get(session.native_session_id):
            sessions[i] = replace(
                session, title=first_user_text_by_sid[session.native_session_id],
            )

    # Turn membership: parts of the same native turn link to one another (the
    # first part of the turn is the anchor).
    turn_groups: dict[str, list[str]] = {}
    for part in parts:
        turn_key = (
            str(part["message_id"]) if live else str(part["turn_id"])
        )
        turn_groups.setdefault(turn_key, []).append(
            str(part["id"] if live else part["part_id"])
        )
    for turn_parts in turn_groups.values():
        anchor = next((by_part[p] for p in turn_parts if p in by_part), None)
        if anchor is None:
            continue
        for pid in turn_parts:
            member = by_part.get(pid)
            if member is not None and member.event_id != anchor.event_id:
                relations.append(EventRelation(
                    relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                              f"rel-turn:{member.event_id}:{anchor.event_id}"),
                    source_event_id=member.event_id, target_event_id=anchor.event_id,
                    relation_kind=RelationKind.TURN_MEMBERSHIP,
                ))

    # Compaction range: each compaction summary covers the earlier kept events
    # of its session, anchored at the compacted range start.
    order = {e.event_id: i for i, e in enumerate(events)}
    for cev in [e for e in events if e.kind is EventKind.COMPACTION_SUMMARY]:
        kept = [e for e in events
                if e.session_id == cev.session_id
                and e.kind is not EventKind.COMPACTION_SUMMARY]
        if not kept:
            continue
        earliest = min(kept, key=lambda e: (e.occurred_at or "", order[e.event_id]))
        if earliest.event_id == cev.event_id:
            continue
        relations.append(EventRelation(
            relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                      f"rel-compact:{cev.event_id}"),
            source_event_id=cev.event_id, target_event_id=earliest.event_id,
            relation_kind=RelationKind.COMPACTED_RANGE,
        ))

    # Native parent_id -> SUBAGENT relation (live session table only): the child
    # session-lifecycle event points back to the parent session-lifecycle event.
    if live:
        life = {e.provenance.native_event_id: e for e in events
                if e.kind is EventKind.SESSION_LIFECYCLE}
        for trace in traces:
            pid = trace["parent_id"]
            if pid is None:
                continue
            child_life = life.get(str(trace["id"]))
            parent_life = life.get(str(pid))
            if child_life is None or parent_life is None:
                continue
            if child_life.event_id == parent_life.event_id:
                continue
            relations.append(EventRelation(
                relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-subagent:{child_life.event_id}"),
                source_event_id=child_life.event_id, target_event_id=parent_life.event_id,
                relation_kind=RelationKind.SUBAGENT,
            ))

    if unknown:
        warnings.append(f"{unknown} unknown part type(s) preserved")

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events),
        fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE),
        sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
    )


def _json_object(value) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
