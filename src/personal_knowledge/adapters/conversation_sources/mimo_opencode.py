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

ADAPTER_VERSION = "1.5.0"
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
# (a single Mimo reasoning part was observed at ~159k chars). Reasoning, tool
# arguments, tool output and compaction bodies are all event *content* (the
# body) and are therefore never capped; only the navigation ``summary`` stays
# bounded at _SUMMARY_LIMIT.
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
        and output go into content verbatim (never truncated); CONTENT_
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

        def _payload(value) -> str | None:
            # Round-5 fix: preserve native whitespace verbatim (no strip).
            # Tool payloads are content, so they are never truncated.
            if value is None:
                return None
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str)
            return text or None

        args_json = _payload(args)
        if args_json:
            call = self._event(
                artifact, session_id=session_id, kind=EventKind.TOOL_CALL,
                locator=f"{locator_base}#part:{part_id}:call",
                native_id=f"{part_id}:call", occurred_at=occurred_at,
                content=args_json,
                summary=(args_json if len(args_json) <= 2048 else args_json[:2048]),
                native_payload_ref=f"{part_id}#{args_field}",
                fidelity=_fidelity(),
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

        out_json = _payload(output)
        if out_json:
            result = self._event(
                artifact, session_id=session_id, kind=EventKind.TOOL_RESULT,
                locator=f"{locator_base}#part:{part_id}:result",
                native_id=f"{part_id}:result", occurred_at=occurred_at,
                content=out_json,
                summary=(out_json if len(out_json) <= 2048 else out_json[:2048]),
                native_payload_ref=f"{part_id}#state.output",
                fidelity=_fidelity(),
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
        reduced to a 2048-char summary the way short labels are. Content keeps
        the text verbatim (no cap) and summary holds a bounded digest.
        """
        session_id = parent.session_id
        native_session = parent.provenance.native_session_id
        part_id = str(part["id"])
        locator = f"{artifact.relative_path}#part:{part_id}"

        if not text:
            # No reasoning text mapped: content and summary stay absent and
            # content availability is honestly partial, like the empty tool
            # argument path. Ciphertext without a local key is the same gap,
            # named by the field that actually holds it.
            cipher_field = _reasoning_ciphertext_field(part_data)
            if cipher_field:
                return self._event(
                    artifact, session_id=session_id, kind=EventKind.REASONING,
                    locator=locator, native_id=part_id, occurred_at=occurred_at,
                    content=None, summary=None, native_payload_ref=None,
                    fidelity=_fidelity(
                        CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                    field_dispositions=(
                        FieldDispositionRecord(
                            cipher_field, FieldDisposition.UNAVAILABLE,
                            f"{cipher_field} 无本地密钥，不能解密"),
                    ),
                    native_session=native_session,
                )
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

        return self._event(
            artifact, session_id=session_id, kind=EventKind.REASONING,
            locator=locator, native_id=part_id, occurred_at=occurred_at,
            content=text,
            summary=text[:_SUMMARY_LIMIT],
            native_payload_ref=f"{part_id}#text",
            fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.COMPLETE),
            field_dispositions=(
                FieldDispositionRecord(
                    "text", FieldDisposition.MAPPED, "full reasoning text mapped"),
            ),
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
        # Loss counters that must surface in warnings instead of vanishing:
        # orphan parts (parent message absent from the artifact) and JSON
        # payloads that could not be parsed.
        orphan_parts = 0
        bad_json = 0

        def _load_json(value) -> dict:
            nonlocal bad_json
            parsed, ok = _json_object_checked(value)
            if not ok:
                bad_json += 1
            return parsed

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
            # Auxiliary pre-scan of the same payloads the message loop reads;
            # parse loss is counted there (once per payload), not here.
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

        sessions_with_messages = {str(row["session_id"]) for row in messages}
        sessions_with_parts = _sessions_with_parts(parts, messages)

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
            empty_session = (
                sid not in sessions_with_messages and sid not in sessions_with_parts
            )
            events.append(self._event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                                      locator=f"{artifact.relative_path}#session:{sid}", native_id=sid,
                                      occurred_at=normalize_timestamp(
                                          row["time_created"] if live else row["created_at"]
                                      ),
                                      summary=str(row["title"] or "")[:256] or None, native_session=sid,
                                      field_dispositions=(
                                          FieldDispositionRecord(
                                              "session", FieldDisposition.UNAVAILABLE,
                                              "该 session 没有 message 和 part",
                                          ),
                                      ) if empty_session else ()))

        for msg in messages:
            sid = str(msg["session_id"])
            session_id = make_event_id(self.family, artifact.artifact_id, CONTRACT_VERSION,
                                       sid, kind=EventKind.SESSION_LIFECYCLE)
            data = _load_json(msg["data"]) if live else dict(msg)
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
                # The parent message is absent from the artifact (schema
                # drift, allowlist gap): the part must be counted, not
                # silently dropped.
                orphan_parts += 1
                continue
            part_data = _load_json(part["data"]) if live else dict(part)
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
            subtask_body = None
            if part_type == "subtask":
                kind = EventKind.SUBAGENT_BOUNDARY
                prompt = part_data.get("prompt")
                subtask_body = None if prompt is None else str(prompt)
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
                if part_type == "subtask":
                    part_content = subtask_body
                    part_summary = None
                else:
                    # Compaction/file bodies were previously only reachable
                    # through a 2048-char summary; they are content too.
                    part_content = text
                    part_summary = None if is_message else (text[:2048] if text else None)
                part_dispositions = (
                    (
                        FieldDispositionRecord(
                            "type", FieldDisposition.UNAVAILABLE,
                            "type=patch 只有 hash/files，没有正文",
                        ),
                    )
                    if part_type == "patch" else ()
                )
                ev = self._event(
                    artifact, session_id=parent.session_id, kind=kind,
                    locator=f"{artifact.relative_path}#part:{part['id']}",
                    native_id=part["id"],
                    occurred_at=normalize_timestamp(
                        part["time_created"] if live else part["created_at"]
                    ),
                    content=part_content,
                    summary=part_summary,
                    field_dispositions=part_dispositions,
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
        if orphan_parts:
            warnings.append(
                f"{orphan_parts} orphan part(s) whose parent message is absent "
                "from the artifact; they map to no typed event and stay only "
                "in the source artifact"
            )
        if bad_json:
            warnings.append(
                f"{bad_json} malformed JSON payload(s) decoded as empty; "
                "their raw bytes stay in the artifact"
            )

        return AdaptationResult(
            family=self.family, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
            artifacts=(artifact,), events=tuple(events),
            fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE),
            sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
        )


def _reasoning_ciphertext_field(part_data: dict) -> str | None:
    """Native field holding reasoning ciphertext, when text is empty."""
    metadata = part_data.get("metadata") if isinstance(part_data, dict) else None
    if not isinstance(metadata, dict):
        return None
    openai = metadata.get("openai")
    if not isinstance(openai, dict):
        return None
    cipher = openai.get("reasoningEncryptedContent")
    if isinstance(cipher, str) and cipher:
        return "metadata.openai.reasoningEncryptedContent"
    return None


def _sessions_with_parts(parts, messages) -> set[str]:
    message_session = {str(row["id"]): str(row["session_id"]) for row in messages}
    found: set[str] = set()
    for part in parts:
        part_session = part["session_id"] if "session_id" in part.keys() else None
        if part_session:
            found.add(str(part_session))
            continue
        owner = message_session.get(str(part["message_id"]))
        if owner:
            found.add(owner)
    return found


def _json_object(value) -> dict:
    """Parse ``value`` as a JSON object; failures decode to ``{}`` silently.

    Adaptation paths that must report loss use :func:`_json_object_checked`.
    """
    return _json_object_checked(value)[0]


def _json_object_checked(value) -> tuple[dict, bool]:
    """Parse ``value`` as a JSON object -> ``(obj, parsed_ok)``.

    ``parsed_ok`` is ``False`` when the value is a string that failed to
    parse as JSON (or parsed to a non-object); the caller counts the loss
    instead of letting it vanish.
    """
    if not isinstance(value, str):
        return (value if isinstance(value, dict) else {}), True
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}, False
    if isinstance(parsed, dict):
        return parsed, True
    return {}, False


# Fully-qualified counter names: unambiguous anywhere in a payload.
_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "inputOther"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "outputOther"),
    "cache_read": ("cache_read", "cacheRead", "inputCacheRead"),
    "cache_write": ("cache_write", "cacheWrite", "inputCacheCreation"),
    "total_tokens": ("total_tokens", "totalTokens"),
}

# Bare words that only mean token counters *inside* the native ``tokens``
# aggregate (``{"total": .., "input": .., "output": .., "cache": {"read": ..,
# "write": ..}}``) or a ``usage`` / ``cache`` sub-payload. As uncontexted
# payload keys they are ordinary words (an ``input`` of a config, a numeric
# ``read`` flag) and used to fabricate false USAGE events.
_BARE_USAGE_ALIASES = {
    "input_tokens": ("input",),
    "output_tokens": ("output",),
    "cache_read": ("read",),
    "cache_write": ("write",),
    "total_tokens": ("total",),
}

# Dict keys whose sub-payload is by definition a token/usage context.
_TOKEN_CONTEXT_KEYS = ("tokens", "usage", "cache")


def _canonical_usage_summary(data, *, token_context: bool = False):
    """Token counters from a nested payload -> canonical usage summary or None.

    Maps the real Mimo/OpenCode ``tokens`` aggregate (``{"total": ...,
    "input": ..., "output": ..., "cache": {"read": ..., "write": ...}}``)
    and any ``usage`` column shape onto the canonical grammar
    ``input_tokens=X output_tokens=Y [cache_read=Z cache_write=W]`` (only
    fields present, integer values), e.g. ``input_tokens=307 output_tokens=253
    cache_read=41152``. Canonical fields are always ordered first.

    Bare aliases (``input`` / ``output`` / ``total`` / ``read`` / ``write``)
    are trusted only in a token context: the whole payload when
    ``token_context=True`` (caller already extracted the ``usage`` dict), or a
    sub-payload reached through a ``tokens`` / ``usage`` / ``cache`` key.
    """

    def resolve_counter(key, bare_ok):
        for canonical, aliases in _USAGE_ALIASES.items():
            if key in aliases:
                return canonical
        if bare_ok:
            for canonical, aliases in _BARE_USAGE_ALIASES.items():
                if key in aliases:
                    return canonical
        return None

    counters: dict[str, int] = {}

    def flatten(node, bare_ok):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                canonical = resolve_counter(key, bare_ok)
                if canonical:
                    counters.setdefault(canonical, int(value))
            elif isinstance(value, dict):
                flatten(value, bare_ok or key in _TOKEN_CONTEXT_KEYS)

    if isinstance(data, dict):
        if isinstance(data.get("tokens"), dict):
            flatten(data["tokens"], bare_ok=True)
        else:
            flatten(data, bare_ok=token_context)
    if not counters:
        return None
    return " ".join(str(k) + "=" + str(counters[k]) for k in _USAGE_ALIASES if k in counters)


def _is_system_placeholder_title(text) -> bool:
    """True when a candidate title is system-injected scaffolding rather than
    a real user-authored title (aligned with codex / gemini / zcode):
    directive blocks opening with ``<`` (e.g. <INSTRUCTIONS>, <AGENTS...),
    AGENTS.md / 'instructions for' markers."""
    if not isinstance(text, str) or not text.strip():
        return False
    if text.lstrip().startswith("<"):
        return True
    lowered = text[:120].lower()
    return "agents.md" in lowered or "instructions for" in lowered


def _session_title(row, live):
    title = row["title"] if "title" in row.keys() else None
    if isinstance(title, str) and title.strip() and not _is_system_placeholder_title(title):
        return title.strip()[:256]
    return None


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
        # The usage dict itself is a token context: bare counter words
        # (input/output/...) are meaningful here.
        return _canonical_usage_summary(usage, token_context=True)
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