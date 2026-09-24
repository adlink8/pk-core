"""Phase 62-03: MimoCode / OpenCode SQLite adapters (families ``mimo``, ``opencode``).

Both families store sessions/messages/message_parts in a SQLite virtual
locator whose database also holds sensitive adjacent account/token tables.
Capture is allowlisted (declared tables/columns only) so those tables are
technically unreachable; this adapter reads only the declared conversation
tables from the filtered artifact. The two families share the parser
primitives but keep separate capability contracts and detection.
"""

from __future__ import annotations

import sqlite3
import json
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
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    FieldDisposition,
    FieldDispositionRecord,
    Provenance,
    RelationKind,
    TypedEvent,
    make_event_id,
)

ADAPTER_VERSION = "1.4.0"
CONTRACT_VERSION = "2"

ALLOWED_TABLES: tuple[str, ...] = ("sessions", "messages", "message_parts")
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": ("id", "title", "created_at"),
    "messages": ("id", "session_id", "role", "content", "created_at"),
    "message_parts": ("id", "message_id", "part_type", "content", "created_at"),
}

LIVE_ALLOWED_TABLES: tuple[str, ...] = ("session", "message", "part")
LIVE_ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "session": ("id", "parent_id", "title", "time_created", "time_updated", "time_compacting"),
    "message": ("id", "session_id", "time_created", "time_updated", "data"),
    "part": ("id", "message_id", "session_id", "time_created", "time_updated", "data"),
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
    "reasoning": EventKind.REASONING,
    "tool": EventKind.TOOL_CALL,
    "compaction": EventKind.COMPACTION_SUMMARY,
    "step-start": EventKind.TURN_BOUNDARY,
    "step-finish": EventKind.TURN_BOUNDARY,
    "file": EventKind.FILE_CONTEXT,
}

# Reasoning blocks routinely run to hundreds of thousands of characters
# (a single Mimo reasoning part was observed at ~159k chars).  Canonical
# content keeps the full text up to this cap; an overrun is honestly declared
# REDACTED + partial rather than silently truncated while still advertising
# complete content availability.
_REASONING_CONTENT_LIMIT = 100_000
_SUMMARY_LIMIT = 2048


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


class _Family:
    def __init__(self, family: str):
        self.family = family

    def capability(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            family=self.family, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
            supported_event_kinds=(
                EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
                EventKind.ASSISTANT_MESSAGE, EventKind.REASONING,
                EventKind.TOOL_CALL, EventKind.COMPACTION_SUMMARY,
                EventKind.USAGE, EventKind.UNKNOWN_NATIVE,
            ),
            supported_relation_kinds=(RelationKind.PARENT_CHILD,),
            fidelity_dimensions=tuple(FidelityDimension),
            capabilities={
                "native_shape": "sqlite_virtual_locator",
                "tables": ",".join(ALLOWED_TABLES),
                "adjacent_tables": "forbidden_by_capture_allowlist",
            },
        )

    def detect(self, artifact: SourceArtifact, *, artifact_root: Path) -> bool:
        if artifact.source_kind != "sqlite":
            return False
        try:
            con = sqlite3.connect(f"file:{artifact_bytes_path(artifact_root, artifact)}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('messages','message')"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return False
        return bool(rows)

    def _event(self, artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
               content=None, summary=None, fidelity=None, native_session=None,
               native_payload_ref=None, field_dispositions=()) -> TypedEvent:
        return TypedEvent(
            event_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                   native_id or locator, kind=kind, session_id=session_id),
            session_id=session_id, kind=kind,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=locator, native_session_id=native_session or None,
                native_event_id=native_id, contract_version=CONTRACT_VERSION,
            ),
            fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
            content=content, summary=summary, native_payload_ref=native_payload_ref,
            field_dispositions=tuple(field_dispositions),
        )

    def _tool_part_events(self, artifact, *, parent, part, part_data, live,
                          occurred_at, locator_base):
        """Emit TOOL_CALL / TOOL_RESULT events for a Mimo/OpenCode tool part.

        Real tool parts carry the call arguments under state.input (or
        input / arguments) and the result under state.output; the
        old code only read text/content and silently dropped both
        (fidelity claimed complete while the args never landed).  Arguments
        and output are truncated to bounded canonical content; CONTENT_
        AVAILABILITY is only complete when the payload actually mapped, and a
        field disposition documents a missing argument payload.
        """
        events: list = []
        relations: list = []
        session_id = parent.session_id
        native_session = parent.provenance.native_session_id
        part_id = str(part["id"])

        state = part_data.get("state")
        state = state if isinstance(state, dict) else {}
        args = None
        args_field = None
        for field in ("input", "arguments"):
            if field in state:
                args = state.get(field)
                args_field = "state." + field
                break
        if args is None:
            for field in ("input", "arguments"):
                if field in part_data:
                    args = part_data.get(field)
                    args_field = field
                    break
        output = state.get("output") if "output" in state else part_data.get("output")

        def _payload(value, limit=50000):
            # Round-5 fix: preserve native whitespace verbatim (no strip) and
            # report truncation so the caller can record a field disposition.
            if value is None:
                return None, False
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str)
            if not text:
                return None, False
            truncated = len(text) > limit
            return (text[:limit] if truncated else text), truncated

        args_json, args_truncated = _payload(args)
        if args_json:
            args_disp = ()
            if args_truncated:
                args_disp = (FieldDispositionRecord(
                    args_field or "state.input", FieldDisposition.MAPPED,
                    "tool input truncated; full text exceeds content cap",
                ),)
            call = self._event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=f"{locator_base}#part:{part_id}:call",
                native_id=f"{part_id}:call", occurred_at=occurred_at,
                content=args_json,
                summary=(args_json if len(args_json) <= 2048 else args_json[:2048]),
                native_payload_ref=f"{part_id}#{args_field}",
                fidelity=_fidelity(),
                field_dispositions=args_disp,
                native_session=native_session,
            )
        else:
            call = self._event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=f"{locator_base}#part:{part_id}:call",
                native_id=f"{part_id}:call", occurred_at=occurred_at,
                content=None, summary=None, native_payload_ref=None,
                fidelity=_fidelity(
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                field_dispositions=(
                    FieldDispositionRecord(
                        "state.input", FieldDisposition.UNAVAILABLE,
                        "tool arguments not present in native tool part"),
                ),
                native_session=native_session,
            )
        events.append(call)

        out_json, out_truncated = _payload(output)
        if out_json:
            out_disp = ()
            if out_truncated:
                out_disp = (FieldDispositionRecord(
                    "state.output", FieldDisposition.MAPPED,
                    "tool output truncated; full text exceeds content cap",
                ),)
            result = self._event(
                artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
                locator=f"{locator_base}#part:{part_id}:result",
                native_id=f"{part_id}:result", occurred_at=occurred_at,
                content=out_json,
                summary=(out_json if len(out_json) <= 2048 else out_json[:2048]),
                native_payload_ref=f"{part_id}#state.output",
                fidelity=_fidelity(),
                field_dispositions=out_disp,
                native_session=native_session,
            )
            events.append(result)
            relations.append(EventRelation(
                relation_id=make_event_id(
                    self.family, artifact.artifact_id, CONTRACT_VERSION,
                    f"rel-call:{part_id}"),
                source_event_id=call.event_id, target_event_id=result.event_id,
                relation_kind=RelationKind.CALL_RESULT,
            ))
        return events, relations

    def _reasoning_part_event(self, artifact, *, parent, part, part_data, text,
                              live, occurred_at):
        """Emit one REASONING event with the full reasoning text in content.

        Reasoning carries the semantics of the turn itself, so it must not be
        reduced to a 2048-char summary the way long non-message parts are.
        Canonical content keeps the full text up to _REASONING_CONTENT_LIMIT
        and summary holds the 2048-char digest; an overrun is declared with
        a REDACTED field disposition and PARTIAL content availability instead
        of advertising complete coverage of truncated text (repeat of the
        observed ~159k-char reasoning loss).
        """
        session_id = parent.session_id
        native_session = parent.provenance.native_session_id
        part_id = str(part["id"])
        locator = f"{artifact.relative_path}#part:{part_id}"

        if not text:
            # No reasoning text mapped: content and summary stay absent and
            # content availability is honestly partial, like the empty tool
            # argument path.
            return self._event(
                artifact, session_id=session_id, kind=EventKind.REASONING,
                locator=locator, native_id=part_id, occurred_at=occurred_at,
                content=None, summary=None, native_payload_ref=None,
                fidelity=_fidelity(
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                field_dispositions=(
                    FieldDispositionRecord(
                        "text", FieldDisposition.UNAVAILABLE,
                        "reasoning text not present in native part"),
                ),
                native_session=native_session,
            )

        over_limit = len(text) > _REASONING_CONTENT_LIMIT
        dispositions = (
            FieldDispositionRecord(
                "text", FieldDisposition.REDACTED,
                f"reasoning truncated to {_REASONING_CONTENT_LIMIT} chars"),
        ) if over_limit else (
            FieldDispositionRecord(
                "text", FieldDisposition.MAPPED, "full reasoning text mapped"),
        )
        return self._event(
            artifact, session_id=session_id, kind=EventKind.REASONING,
            locator=locator, native_id=part_id, occurred_at=occurred_at,
            content=text[:_REASONING_CONTENT_LIMIT],
            summary=text[:_SUMMARY_LIMIT],
            native_payload_ref=f"{part_id}#text",
            fidelity=_fidelity(
                CONTENT_AVAILABILITY=(
                    FidelityLevel.PARTIAL if over_limit
                    else FidelityLevel.COMPLETE)),
            field_dispositions=dispositions,
            native_session=native_session,
        )

    def adapt(self, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
        if len(artifact_set.artifacts) != 1:
            raise EventContractError(
                f"{self.family} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
            )
        artifact = artifact_set.artifacts[0]
        if artifact.source_kind != "sqlite":
            raise EventContractError(f"{self.family} adapter requires a sqlite artifact")
        try:
            con = sqlite3.connect(f"file:{artifact_root / artifact.content_hash[:32]}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                live = {"session", "message", "part"} <= tables
                sessions_rows = con.execute(
                    "SELECT * FROM session" if live else "SELECT * FROM sessions"
                ).fetchall()
                messages = con.execute(
                    "SELECT * FROM message" if live else "SELECT * FROM messages"
                ).fetchall()
                parts = con.execute(
                    "SELECT * FROM part" if live else "SELECT * FROM message_parts"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise EventContractError(f"{self.family} artifact unreadable: {exc}") from exc

        sessions: list[AdaptedSession] = []
        events: list[TypedEvent] = []
        relations: list[EventRelation] = []
        warnings: list[str] = []
        by_message: dict[str, TypedEvent] = {}
        unknown = 0

        # Mimo carries no session.model column; fall back per-session to the
        # first assistant message model id before building sessions so an open
        # sqlite connection is not needed inside the loop.
        msg_model_by_session: dict[str, str] = {}
        # Real Mimo/OpenCode live messages carry the working directory under
        # data.path.cwd; surface it on the session when the session row itself
        # exposes no cwd/directory column (the captured session allowlist often
        # omits it). Pre-scanned so sessions built before the message loop can
        # pick it up.
        msg_cwd_by_session: dict[str, str] = {}
        for _msg in messages:
            _data = _json_object(_msg["data"]) if live else dict(_msg)
            if isinstance(_data, dict):
                _cand = (_data.get("modelID") or _data.get("model_id"))
                if isinstance(_cand, str) and _cand.strip():
                    msg_model_by_session.setdefault(
                        str(_msg["session_id"]), _cand.strip()[:256])
                _path = _data.get("path")
                if isinstance(_path, dict):
                    _pwd = _path.get("cwd") or _path.get("directory")
                    if isinstance(_pwd, str) and _pwd.strip():
                        msg_cwd_by_session.setdefault(
                            str(_msg["session_id"]), _pwd.strip()[:512])

        for row in sessions_rows:
            sid = str(row["id"])
            session_id = make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                       sid, kind=EventKind.SESSION_LIFECYCLE)
            sessions.append(AdaptedSession(
                session_id=session_id,
                provenance=Provenance(
                    artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                    native_locator=f"{artifact.relative_path}#session:{sid}",
                    native_session_id=sid, native_event_id=sid, contract_version=CONTRACT_VERSION,
                ),
                fidelity=_fidelity(
                    COMPACTION_VISIBILITY=FidelityLevel.PARTIAL if live else FidelityLevel.COMPLETE
                ), native_session_id=sid,
                started_at=normalize_timestamp(
                    row["time_created"] if live else row["created_at"]
                ),
                # Round-4 fix: native session.time_updated was never mapped, so
                # ended_at was always NULL despite the source having the value.
                ended_at=normalize_timestamp(
                    (row["time_updated"] if live and row["time_updated"]
                     else (row["updated_at"] if "updated_at" in row.keys() else None))
                ),
                title=_session_title(row, live),
                cwd=_session_cwd_field(row) or msg_cwd_by_session.get(sid),
                model=_session_model_field(row) or msg_model_by_session.get(sid),
            ))
            events.append(self._event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                                      locator=f"{artifact.relative_path}#session:{sid}", native_id=sid,
                                      occurred_at=normalize_timestamp(
                                          row["time_created"] if live else row["created_at"]
                                      ),
                                      summary=str(row["title"] or "")[:256] or None, native_session=sid))

        for msg in messages:
            sid = str(msg["session_id"])
            session_id = make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                       sid, kind=EventKind.SESSION_LIFECYCLE)
            data = _json_object(msg["data"]) if live else dict(msg)
            role = data.get("role")
            kind = EventKind.USER_MESSAGE if role == "user" else (
                EventKind.ASSISTANT_MESSAGE if role == "assistant" else None)
            locator = f"{artifact.relative_path}#message:{msg['id']}"
            if kind is None:
                unknown += 1
                ev = self._event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                                 locator=locator, native_id=msg["id"],
                                 occurred_at=normalize_timestamp(
                                     msg["time_created"] if live else msg["created_at"]
                                 ),
                                 fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                                    RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                                 native_session=sid)
                events.append(ev)
                by_message[str(msg["id"])] = ev
                continue
            raw_content = data.get("content")
            ev = self._event(
                artifact, session_id=session_id, kind=kind, locator=locator,
                native_id=msg["id"],
                occurred_at=normalize_timestamp(
                    msg["time_created"] if live else msg["created_at"]
                ),
                content=None if raw_content is None else str(raw_content),
                native_session=sid,
            )
            events.append(ev)
            by_message[str(msg["id"])] = ev
            usage_summary = _mimo_usage_summary(msg, data, live)
            if usage_summary:
                usev = self._event(
                    artifact, session_id=session_id, kind=EventKind.USAGE,
                    locator=f"{artifact.relative_path}#message:{msg['id']}:usage",
                    native_id=f"{msg['id']}:usage",
                    occurred_at=normalize_timestamp(
                        msg["time_created"] if live else msg["created_at"]
                    ),
                    content=None, summary=usage_summary,
                    fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                    native_session=sid,
                )
                events.append(usev)

        for part in parts:
            parent = by_message.get(str(part["message_id"]))
            if parent is None:
                continue
            part_data = _json_object(part["data"]) if live else dict(part)
            part_type = part_data.get("type") if live else part["part_type"]

            if part_type == "tool":
                # Tool parts carry no text/content; their arguments live in
                # state.input (or input/arguments) and their result in
                # state.output. Emit TOOL_CALL + TOOL_RESULT with those
                # payloads instead of silently dropping them.
                tool_events, tool_relations = self._tool_part_events(
                    artifact, parent=parent, part=part, part_data=part_data,
                    live=live,
                    occurred_at=normalize_timestamp(
                        part["time_created"] if live else part["created_at"]
                    ),
                    locator_base=artifact.relative_path,
                )
                events.extend(tool_events)
                relations.extend(tool_relations)
                for ev in tool_events:
                    relations.append(EventRelation(
                        relation_id=make_event_id(
                            self.family, artifact.artifact_id, CONTRACT_VERSION,
                            f"rel-parent:{ev.event_id}:{parent.event_id}"),
                        source_event_id=ev.event_id, target_event_id=parent.event_id,
                        relation_kind=RelationKind.PARENT_CHILD,
                    ))
                tool_usage = _canonical_usage_summary(part_data)
                if tool_usage:
                    events.append(self._event(
                        artifact, session_id=parent.session_id, kind=EventKind.USAGE,
                        locator=f"{artifact.relative_path}#part:{part['id']}:usage",
                        native_id=f"{part['id']}:usage",
                        occurred_at=normalize_timestamp(
                            part["time_created"] if live else part["created_at"]
                        ),
                        content=None, summary=tool_usage,
                        fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                        native_session=parent.provenance.native_session_id,
                    ))
                continue

            kind = _PART_KINDS.get(part_type)
            if live and part_type == "text":
                kind = parent.kind
            if kind is None:
                unknown += 1
                kind = EventKind.UNKNOWN_NATIVE
            raw_content = (
                part_data.get("text")
                if "text" in part_data
                else part_data.get("content")
            )
            text = None if raw_content is None else str(raw_content)
            is_message = kind in {
                EventKind.USER_MESSAGE,
                EventKind.ASSISTANT_MESSAGE,
                EventKind.DEVELOPER_MESSAGE,
                EventKind.SYSTEM_MESSAGE,
            }
            if kind is EventKind.REASONING:
                # Reasoning is semantics-bearing content: keep the full text
                # in canonical content (not dropped) with a 2048-char summary,
                # declaring any overrun honestly via REDACTED + partial.
                ev = self._reasoning_part_event(
                    artifact, parent=parent, part=part, part_data=part_data,
                    text=text, live=live,
                    occurred_at=normalize_timestamp(
                        part["time_created"] if live else part["created_at"]
                    ),
                )
            else:
                ev = self._event(
                    artifact, session_id=parent.session_id, kind=kind,
                    locator=f"{artifact.relative_path}#part:{part['id']}",
                    native_id=part["id"],
                    occurred_at=normalize_timestamp(
                        part["time_created"] if live else part["created_at"]
                    ),
                    content=text if is_message else None,
                    summary=None if is_message else (text[:2048] if text else None),
                    native_session=parent.provenance.native_session_id,
                )
            events.append(ev)
            relations.append(EventRelation(
                relation_id=make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-parent:{ev.event_id}:{parent.event_id}"),
                source_event_id=ev.event_id, target_event_id=parent.event_id,
                relation_kind=RelationKind.PARENT_CHILD,
            ))
            # Real Mimo/OpenCode carry token usage on a part (e.g. the
            # step-finish aggregate) as part.data["tokens"]; surface it as a
            # standalone USAGE event in canonical input_tokens= form.
            part_usage = _canonical_usage_summary(part_data)
            if part_usage:
                events.append(self._event(
                    artifact, session_id=parent.session_id, kind=EventKind.USAGE,
                    locator=f"{artifact.relative_path}#part:{part['id']}:usage",
                    native_id=f"{part['id']}:usage",
                    occurred_at=normalize_timestamp(
                        part["time_created"] if live else part["created_at"]
                    ),
                    content=None, summary=part_usage,
                    fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                    native_session=parent.provenance.native_session_id,
                ))

        if unknown:
            warnings.append(f"{unknown} unknown native record(s) preserved")

        return AdaptationResult(
            family=self.family, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
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


_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "input", "inputOther"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "output", "outputOther"),
    "cache_read": ("cache_read", "cacheRead", "inputCacheRead", "read"),
    "cache_write": ("cache_write", "cacheWrite", "inputCacheCreation", "write"),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
}


def _canonical_usage_summary(data):
    """Token counters from a nested payload -> canonical usage summary or None.

    Maps the real Mimo/OpenCode ``tokens`` aggregate (``{"total": ...,
    "input": ..., "output": ..., "cache": {"read": ..., "write": ...}}``)
    and any ``usage`` column shape onto the canonical grammar
    ``input_tokens=X output_tokens=Y [cache_read=Z cache_write=W]`` (only
    fields present, integer values), e.g. ``input_tokens=307 output_tokens=253
    cache_read=41152``. Canonical fields are always ordered first.
    """
    def resolve_counter(key):
        for canonical, aliases in _USAGE_ALIASES.items():
            if key in aliases:
                return canonical
        return None

    counters = {}

    def flatten(node, base=''):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                canonical = resolve_counter(key)
                if canonical:
                    counters.setdefault(canonical, int(value))
            elif isinstance(value, dict):
                flatten(value, key)

    tokens = data.get('tokens') if isinstance(data, dict) else None
    if isinstance(tokens, dict):
        flatten(tokens)
    elif isinstance(data, dict):
        flatten(data)
    if not counters:
        return None
    return " ".join(str(k) + "=" + str(counters[k]) for k in _USAGE_ALIASES if k in counters)


def _session_title(row, live):
    title = row["title"] if "title" in row.keys() else None
    return (title.strip()[:256] if isinstance(title, str) and title.strip() else None)


def _session_cwd_field(row):
    for key in ("cwd", "directory", "root"):
        if key in row.keys() and row[key]:
            return str(row[key])[:512]
    return None


def _session_model_field(row):
    """Model id from the session row model column (JSON or plain string).

    Some families (OpenCode) store the model as JSON, e.g.
    {"id": "gpt-5", "providerID": ".."}; the id is what the dataset exposes.
    Non-JSON strings are used as-is. A schema that carries the model under
    metadata or agent instead is consulted only when an explicit model column
    is absent.
    """
    if "model" in row.keys() and row["model"]:
        value = row["model"]
        parsed = _json_object(value)
        if parsed and isinstance(parsed, dict):
            model = parsed.get("id") or parsed.get("model") or parsed.get("name")
            if isinstance(model, str) and model.strip():
                return model.strip()[:256]
        if isinstance(value, str) and value.strip():
            return value.strip()[:256]
    for key in ("metadata", "agent"):
        if key in row.keys() and row[key]:
            parsed = _json_object(row[key])
            if isinstance(parsed, dict):
                model = (parsed.get("id") or parsed.get("model")
                         or parsed.get("modelID") or parsed.get("name"))
                if isinstance(model, str) and model.strip():
                    return model.strip()[:256]
    return None



def _mimo_usage_summary(msg, data, live):
    """Machine-parseable usage summary (canonical keys) from a message/column."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, str):
        parsed = _json_object(usage)
        usage = parsed or None
    if usage is None and "usage" in msg.keys():
        usage = msg["usage"]
        if isinstance(usage, str):
            parsed = _json_object(usage)
            usage = parsed or None
    if isinstance(usage, dict):
        return _canonical_usage_summary(usage)
    return _canonical_usage_summary(data)


_FAMILIES = {
    "mimo": _Family("mimo"),
    "opencode": _Family("opencode"),
}


def capability(family: str) -> CapabilityDescriptor:
    return _FAMILIES[family].capability()


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    """Detection is family-agnostic here; ownership is resolved by the caller."""
    return _FAMILIES["mimo"].detect(artifact, artifact_root=artifact_root)


def adapt_family(family: str):
    return _FAMILIES[family].adapt


def adapt(family: str, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    return _FAMILIES[family].adapt(artifact_set, artifact_root=artifact_root)