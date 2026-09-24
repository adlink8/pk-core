"""Phase 62-03: Grok multi-file session directory adapter (family ``grok``).

Grok exports a multi-file session directory (summary, transcript, events,
updates, compaction/checkpoint/recap files, subagents, terminal —
62-RESEARCH format matrix). Capture snapshots a declared allowlisted file
set; this adapter preserves cross-file relationships as typed relations and
reports summary-only fidelity as partial, never complete.
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
from personal_knowledge.adapters.conversation_sources.agentsview_pathless import (
    adapt_pathless_observation,
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

FAMILY = "grok"
ADAPTER_VERSION = "1.2.0"
CONTRACT_VERSION = "2"

# Native ``chat_history.jsonl`` record type -> canonical event kind.
#
# Grok tags every transcript record with ``type`` and never with ``role``
# (62-RESEARCH format matrix). The previous revision read this file through a
# ``role`` envelope, which matched nothing: the whole transcript degraded to
# zero content events and the session was reported as summary-only. Map the
# observed native types explicitly instead of guessing at a foreign shape.
_RECORD_KINDS: dict[str, EventKind] = {
    "user": EventKind.USER_MESSAGE,
    "human": EventKind.USER_MESSAGE,
    "assistant": EventKind.ASSISTANT_MESSAGE,
    "ai": EventKind.ASSISTANT_MESSAGE,
    "model": EventKind.ASSISTANT_MESSAGE,
    "system": EventKind.SYSTEM_MESSAGE,
    "developer": EventKind.DEVELOPER_MESSAGE,
    "tool_result": EventKind.TOOL_RESULT,
    "tool_use": EventKind.TOOL_CALL,
    # Backend-side tool invocations (e.g. web_search) carry their payload in a
    # nested ``kind`` object rather than a tool_call id.
    "backend_tool_call": EventKind.TOOL_CALL,
    "reasoning": EventKind.REASONING,
    "usage": EventKind.USAGE,
}

# Declared allowlist for the directory capture (D-08): conversation files only.
ALLOWED_RELATIVE_PATHS: tuple[str, ...] = (
    "summary.json",
    "summary.md",
    "chat_history.jsonl",
    "events.jsonl",
    "updates.jsonl",
    "compaction.md",
    "checkpoint.json",
    "recap.md",
    "requests.jsonl",
    "subagents.json",
    "terminal.jsonl",
)

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


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(
            EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
            EventKind.ASSISTANT_MESSAGE, EventKind.DEVELOPER_MESSAGE,
            EventKind.SYSTEM_MESSAGE, EventKind.REASONING,
            EventKind.TOOL_CALL, EventKind.TOOL_RESULT,
            EventKind.COMPACTION_SUMMARY,
            EventKind.SUBAGENT_BOUNDARY, EventKind.USAGE,
            EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(
            RelationKind.SOURCE_SESSION_CROSSWALK,
            RelationKind.COMPACTED_RANGE,
            RelationKind.CALL_RESULT,
        ),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "multi_file_session_directory|agentsview_pathless_observation",
            "allowlist": ",".join(ALLOWED_RELATIVE_PATHS),
            "summary_only_fidelity": "partial",
        },
    )


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    """True when the artifact set contains the Grok summary marker."""
    if artifact.source_kind == "sqlite":
        relative = (artifact.relative_path or "").lower()
        return "sessions.db" in relative or "agentsview" in relative
    if artifact.source_kind != "file":
        return False
    if Path(artifact.relative_path).name not in (
        "summary.json", "summary.md", "chat_history.jsonl"
    ):
        return False
    try:
        head = artifact_bytes_path(artifact_root, artifact).read_text(encoding="utf-8")[:16384]
    except OSError:
        return False
    # Whitespace-stripped comparison: a JSON formatter emitting ``"type": "user"``
    # must not disqualify the transcript. The window is deliberately wider than a
    # single line — a Grok session opens with a multi-kilobyte system prompt, so
    # the original 512-byte probe never reached the first message record and the
    # whole transcript was silently excluded as "not a Grok file".
    compact = "".join(head.split())
    return (
        "# Summary" in head or "grok_session" in head or '"role"' in head
        or '"session_summary"' in head
        or '"type":"system"' in compact
        or '"type":"user"' in compact
        or '"type":"assistant"' in compact
        or '"tool_calls"' in compact
        or '"model_fingerprint"' in compact
        or '"encrypted_content"' in compact
    )


def _provenance(artifact: SourceArtifact, locator: str, *, session: str | None, native_id: str | None) -> Provenance:
    return Provenance(
        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
        native_locator=locator, native_session_id=session or None,
        native_event_id=native_id, contract_version=CONTRACT_VERSION,
    )


def _event(artifact, *, session_id, kind, locator, native_id=None, occurred_at=None,
           content=None, summary=None, fidelity=None, native_session=None) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               native_id or locator, kind=kind, session_id=session_id),
        session_id=session_id, kind=kind,
        provenance=_provenance(artifact, locator, session=native_session, native_id=native_id),
        fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
        content=content, summary=summary,
    )


def _read_jsonl_blob(root: Path, artifact: SourceArtifact) -> list[dict]:
    try:
        text = (root / artifact.content_hash[:32]).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _flatten_content(value) -> str | None:
    """Recover text from a native content value without inventing content.

    Grok mixes shapes: ``user`` rows carry a list of typed parts
    (``{"type": "text", "text": ...}``) while ``assistant`` / ``tool_result`` /
    ``system`` rows carry a bare string. A plain ``str(value)`` would persist a
    Python repr for the list shape, so flatten the parts explicitly.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        saw_text = False
        for item in value:
            if isinstance(item, str):
                saw_text = True
                parts.append(item)
            elif isinstance(item, dict) and (
                item.get("type") in ("text", "summary_text") or "text" in item
            ):
                raw = item.get("text")
                saw_text = True
                parts.append("" if raw is None else str(raw))
        return "\n".join(parts) if saw_text else None
    if isinstance(value, dict):
        raw = value.get("text")
        return str(raw) if isinstance(raw, str) else None
    return None


def _tool_call_text(call: dict) -> str | None:
    """One-line rendering of a native tool invocation (name + arguments)."""
    name = call.get("name") or call.get("tool_name")
    arguments = call.get("arguments")
    if isinstance(arguments, (dict, list)):
        try:
            arguments = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = str(arguments)
    if name and arguments:
        return f"{name} {arguments}"
    if name or arguments:
        return str(name or arguments)
    return None


def _backend_call_text(row: dict) -> str | None:
    """One-line rendering of a backend-side invocation (e.g. web_search)."""
    kind_obj = row.get("kind")
    if not isinstance(kind_obj, dict):
        return None
    parts: list[str] = []
    tool_type = kind_obj.get("tool_type")
    if isinstance(tool_type, str) and tool_type:
        parts.append(tool_type)
    action = kind_obj.get("action")
    if isinstance(action, dict):
        for key in ("type", "query", "url"):
            value = action.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
    return " ".join(parts)[:2048] if parts else None


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one captured Grok session directory into typed events/relations."""
    if not artifact_set.artifacts:
        raise EventContractError(f"{FAMILY} adapter requires at least one artifact")
    if (
        len(artifact_set.artifacts) == 1
        and artifact_set.artifacts[0].source_kind == "sqlite"
    ):
        return adapt_pathless_observation(
            artifact_set, artifact_root=artifact_root, family=FAMILY,
            adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        )
    artifacts = artifact_set.artifacts
    by_path = {Path(a.relative_path).name: a for a in artifacts}

    session_id = make_event_id(FAMILY, artifacts[0].artifact_id, CONTRACT_VERSION,
                               None, kind=EventKind.SESSION_LIFECYCLE, native_locator="session")
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    native_session = None

    summary_artifact = by_path.get("summary.md")
    if summary_artifact is not None:
        try:
            summary_text = (artifact_root / summary_artifact.content_hash[:32]).read_text(encoding="utf-8")
        except OSError:
            summary_text = ""
        native_session = _first_line(summary_text)
        events.append(_event(summary_artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                             locator="summary.md#doc", native_id="summary",
                             summary=summary_text[:2048] or None, native_session=native_session))

    summary_json = by_path.get("summary.json")
    if summary_json is not None:
        try:
            doc = json.loads(
                (artifact_root / summary_json.content_hash[:32]).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            doc = {}
        info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
        native_session = str(info.get("id") or Path(summary_json.relative_path).parent.name)
        session_id = make_event_id(
            FAMILY, summary_json.artifact_id, CONTRACT_VERSION, native_session,
            kind=EventKind.SESSION_LIFECYCLE,
        )
        events.append(_event(
            summary_json, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
            locator=f"{summary_json.relative_path}#info", native_id=native_session,
            occurred_at=doc.get("created_at"), native_session=native_session,
        ))
        if doc.get("session_summary"):
            events.append(_event(
                summary_json, session_id=session_id,
                kind=EventKind.COMPACTION_SUMMARY,
                locator=f"{summary_json.relative_path}#session_summary",
                native_id=f"{native_session}:summary",
                occurred_at=doc.get("updated_at"),
                summary=str(doc.get("session_summary"))[:2048],
                fidelity=_fidelity(
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                ),
                native_session=native_session,
            ))

    chat = by_path.get("chat_history.jsonl")
    if chat is not None:
        # Native tool-call id -> emitted TOOL_CALL event id, so a later
        # tool_result can be linked back to the call it answers.
        pending_calls: dict[str, str] = {}
        # Grok reuses one native reasoning id across several distinct rows of a
        # session (observed: a single ``rs_...`` id on 10 separate reasoning
        # rows). Event ids are content-addressed per (artifact, native id) and
        # exclude the event kind, so a reused id would collapse distinct rows
        # into duplicate events and fail the contract. Disambiguate repeats
        # positionally; the first occurrence keeps the bare native id.
        seen_native: dict[str, int] = {}
        for index, row in enumerate(_read_jsonl_blob(artifact_root, chat)):
            rtype = str(row.get("type") or row.get("role") or "")
            locator = f"chat_history.jsonl#{index}"
            native_id = str(row.get("id") or f"row-{index}")
            repeat = seen_native.get(native_id, 0)
            seen_native[native_id] = repeat + 1
            if repeat:
                native_id = f"{native_id}#{repeat}"
            occurred_at = row.get("timestamp")
            kind = _RECORD_KINDS.get(rtype)
            if kind is None:
                events.append(_event(chat, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                                     locator=locator, native_id=native_id,
                                     occurred_at=occurred_at,
                                     fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                                        RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                                        CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                                     native_session=native_session))
                continue

            if kind is EventKind.REASONING:
                # Native reasoning ships as an encrypted blob plus an optional
                # plaintext summary. Only the summary is recoverable text; the
                # ciphertext is preserved by reference, never decoded.
                event = _event(chat, session_id=session_id, kind=kind, locator=locator,
                               native_id=native_id, occurred_at=occurred_at,
                               content=_flatten_content(row.get("summary")),
                               fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                               native_session=native_session)
                if row.get("encrypted_content"):
                    event = _with_disposition(
                        event, field_name="encrypted_content",
                        disposition=FieldDisposition.PRESERVED_BY_REFERENCE,
                        reason="reasoning content is encrypted; plaintext not available",
                    )
                events.append(event)
                continue

            if kind is EventKind.TOOL_CALL:
                # Backend-side invocations carry no id; the nested ``kind``
                # object holds the tool type and its action payload.
                events.append(_event(chat, session_id=session_id, kind=kind,
                                     locator=f"{locator}#kind", native_id=native_id,
                                     occurred_at=occurred_at,
                                     content=_backend_call_text(row),
                                     native_session=native_session))
                continue

            events.append(_event(chat, session_id=session_id, kind=kind, locator=locator,
                                 native_id=native_id, occurred_at=occurred_at,
                                 content=_flatten_content(row.get("content")),
                                 native_session=native_session))
            if kind is EventKind.TOOL_RESULT:
                call_id = str(row.get("tool_call_id") or "")
                if call_id and call_id in pending_calls:
                    relations.append(EventRelation(
                        relation_id=make_event_id(FAMILY, chat.artifact_id, CONTRACT_VERSION,
                                                  f"rel-call:{call_id}:{native_id}"),
                        source_event_id=pending_calls[call_id],
                        target_event_id=events[-1].event_id,
                        relation_kind=RelationKind.CALL_RESULT,
                    ))
            # An assistant turn may carry any number of tool invocations; each
            # becomes its own typed event so call/result pairing survives.
            for call_index, call in enumerate(row.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or f"{native_id}:call:{call_index}")
                call_event = _event(chat, session_id=session_id, kind=EventKind.TOOL_CALL,
                                    locator=f"{locator}#tool_call:{call_index}",
                                    native_id=call_id, occurred_at=occurred_at,
                                    content=_tool_call_text(call),
                                    native_session=native_session)
                events.append(call_event)
                pending_calls[call_id] = call_event.event_id
            usage_summary = _row_usage_summary(row)
            if usage_summary:
                events.append(_event(
                    chat, session_id=session_id, kind=EventKind.USAGE,
                    locator=f"chat_history.jsonl#usage:{index}",
                    native_id=f"{native_id}:usage",
                    occurred_at=occurred_at, content=None,
                    summary=usage_summary,
                    fidelity=_fidelity(CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                    native_session=native_session,
                ))

    # Compaction: a markdown/checkpoint file is a typed compaction summary.
    for name in ("compaction.md", "checkpoint.json", "recap.md"):
        artifact = by_path.get(name)
        if artifact is None:
            continue
        try:
            text = (artifact_root / artifact.content_hash[:32]).read_text(encoding="utf-8")
        except OSError:
            continue
        compactor = _event(artifact, session_id=session_id, kind=EventKind.COMPACTION_SUMMARY,
                           locator=f"{name}#doc", native_id=name,
                           summary=text[:2048] or None, native_session=native_session)
        events.append(compactor)
        # Best-effort COMPACTED_RANGE: link the compaction to the last preceding
        # non-compaction event so the range references real, known endpoints.
        prior = _last_preceding_non_compaction(events, compactor.event_id)
        if prior is not None:
            relations.append(EventRelation(
                relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-compact:{compactor.event_id}:{prior.event_id}"),
                source_event_id=prior.event_id, target_event_id=compactor.event_id,
                relation_kind=RelationKind.COMPACTED_RANGE,
            ))
        else:
            compactor = _with_disposition(
                compactor,
                field_name="compacted_range",
                disposition=FieldDisposition.UNSUPPORTED,
                reason="no preceding event locatable to anchor a compacted range",
            )
            events[-1] = compactor

    # Subagents: cross-file relation from subagent entries to the parent session.
    sub_artifact = by_path.get("subagents.json")
    if sub_artifact is not None:
        try:
            sub_doc = json.loads((artifact_root / sub_artifact.content_hash[:32]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sub_doc = []
        subs = sub_doc if isinstance(sub_doc, list) else sub_doc.get("subagents", [])
        parent = next((e for e in events if e.kind is EventKind.SESSION_LIFECYCLE), None)
        for index, sub in enumerate(subs if isinstance(subs, list) else []):
            if not isinstance(sub, dict):
                continue
            ev = _event(sub_artifact, session_id=session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                        locator=f"subagents.json#{index}", native_id=sub.get("id") or f"sub-{index}",
                        occurred_at=sub.get("created_at"),
                        summary=str(sub.get("name") or sub.get("task") or "")[:256] or None,
                        native_session=native_session)
            events.append(ev)
            if parent is not None:
                relations.append(EventRelation(
                    relation_id=make_event_id(FAMILY, sub_artifact.artifact_id, CONTRACT_VERSION,
                                              f"rel-cross:{ev.event_id}:{parent.event_id}"),
                    source_event_id=ev.event_id, target_event_id=parent.event_id,
                    relation_kind=RelationKind.SOURCE_SESSION_CROSSWALK,
                ))

    # Fidelity: summary-only is honest partial; a chat history file raises content fidelity.
    has_chat = chat is not None
    content_level = FidelityLevel.COMPLETE if has_chat else FidelityLevel.PARTIAL
    structure_level = FidelityLevel.COMPLETE if (has_chat or summary_artifact is not None) else FidelityLevel.PARTIAL

    sessions: list[AdaptedSession] = []
    if summary_artifact is not None:
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=_provenance(summary_artifact, "summary.md#doc",
                                   session=native_session, native_id="summary"),
            fidelity=_fidelity(CONTENT_AVAILABILITY=content_level),
            native_session_id=native_session,
        ))
    elif summary_json is not None:
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=_provenance(
                summary_json, f"{summary_json.relative_path}#info",
                session=native_session, native_id=native_session,
            ),
            fidelity=_fidelity(
                CONTENT_AVAILABILITY=content_level,
                STRUCTURE_COMPLETENESS=structure_level,
                RELATION_COMPLETENESS=(
                    FidelityLevel.COMPLETE if has_chat else FidelityLevel.UNKNOWN
                ),
            ),
            native_session_id=native_session,
            started_at=doc.get("created_at") if isinstance(doc, dict) else None,
            ended_at=doc.get("updated_at") if isinstance(doc, dict) else None,
            cwd=_grok_cwd(info),
            model=_grok_model(doc, info, artifact_root, chat),
            git_branch=_grok_branch(info, doc),
            title=_grok_title(info, doc),
        ))

    # ce_events carries a (generation_id, session_id) foreign key into
    # ce_sessions, so every session id an event carries must be backed by a
    # session record. A multi-file session directory is staged and adapted one
    # file at a time, so a lone ``chat_history.jsonl`` (no summary.md /
    # summary.json in the artifact set) emits transcript events with no summary
    # anchor to build the record from. Backfill one record per used id,
    # anchored to the artifact that actually carries those events.
    known_sessions = {session.session_id for session in sessions}
    for event in events:
        if event.session_id in known_sessions:
            continue
        anchor = next(
            (a for a in artifacts if a.artifact_id == event.provenance.artifact_id),
            None,
        )
        if anchor is None:
            continue
        sessions.append(AdaptedSession(
            session_id=event.session_id,
            provenance=_provenance(
                anchor, f"{anchor.relative_path}#session",
                session=native_session, native_id=native_session,
            ),
            fidelity=_fidelity(
                CONTENT_AVAILABILITY=content_level,
                STRUCTURE_COMPLETENESS=structure_level,
                RELATION_COMPLETENESS=(
                    FidelityLevel.COMPLETE if has_chat else FidelityLevel.UNKNOWN
                ),
            ),
            native_session_id=native_session,
        ))
        known_sessions.add(event.session_id)

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=tuple(sorted(artifacts, key=lambda a: a.artifact_id)),
        events=tuple(events),
        fidelity=_fidelity(CONTENT_AVAILABILITY=content_level, STRUCTURE_COMPLETENESS=structure_level),
        sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
    )


_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "output"),
    "cache_read": ("cache_read", "cacheRead"),
    "cache_write": ("cache_write", "cacheWrite"),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def _grok_cwd(info: dict) -> str | None:
    """Working directory from summary.json ``info``.

    The native Grok export puts the project path in ``info.cwd``; older/other
    variants use ``info.project``. Support both so a schema rename does not
    silently drop the session working directory.
    """
    if not isinstance(info, dict):
        return None
    candidate = info.get("cwd")
    if candidate is None:
        candidate = info.get("project")
    return candidate if isinstance(candidate, str) and candidate.strip() else None


def _grok_model(doc: dict, info: dict, artifact_root: Path, chat) -> str | None:
    """Model id for the session.

    Prefer the native summary.json ``current_model_id`` (top-level, confirmed
    in real exports) over an ``info.model`` variant; fall back to the first
    model id observed in the chat_history transcript
    (``model_id``/``model``/``modelID``). The summary-native source is the
    authoritative current model, so it wins over a possibly-stale row-level
    model.
    """
    for source in (doc, info):
        if not isinstance(source, dict):
            continue
        candidate = source.get("current_model_id") or source.get("model")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:256]
    if chat is None:
        return None
    try:
        rows = _read_jsonl_blob(artifact_root, chat)
    except Exception:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = row.get("model_id") or row.get("model") or row.get("modelID")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:256]
    return None


def _grok_branch(info: dict, doc: dict) -> str | None:
    """Git branch for the session from summary.json ``head_branch``.

    Real Grok exports put the branch at top-level ``head_branch``; some
    variants keep it under ``info``. Support both so a schema rename never
    silently drops the branch.
    """
    for source in (doc, info):
        if not isinstance(source, dict):
            continue
        candidate = source.get("head_branch")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:256]
    return None



def _grok_title(info: dict, doc: dict) -> str | None:
    """Session title: explicit title, else a bounded session_summary fallback."""
    title = info.get("title") if isinstance(info, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()[:256]
    summary = doc.get("session_summary") if isinstance(doc, dict) else None
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:256]
    return None


def _row_usage_summary(row: dict) -> str | None:
    # Machine-parseable canonical usage summary from a chat_history row.
    # Surfaces a top-level "usage" dict and/or token fields directly on the
    # row, mapping native counters onto the canonical grammar input_tokens=X
    # output_tokens=Y [cache_read=Z cache_write=W] (only present, integers).
    usage = row.get("usage")
    data = {}
    if isinstance(usage, dict):
        data = usage
    elif isinstance(usage, (int, float)):
        data = {"usage": usage}
    for key in row:
        if key in _USAGE_ALIASES or any(
            key in aliases for aliases in _USAGE_ALIASES.values()
        ):
            if key not in data and isinstance(row[key], (int, float)):
                data[key] = row[key]
    counters: dict[str, int] = {}
    for native, value in data.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for canonical, aliases in _USAGE_ALIASES.items():
            if native in aliases:
                counters.setdefault(canonical, int(value))
                break
    if not counters:
        return None
    return " ".join(
        f"{key}={counters[key]}" for key in _USAGE_ALIASES if key in counters
    )


def _last_preceding_non_compaction(events, current_id):
    """Return the last event emitted before ``current_id`` that is not a compaction."""
    for ev in reversed(events):
        if ev.event_id == current_id:
            continue
        if ev.kind is EventKind.COMPACTION_SUMMARY:
            continue
        return ev
    return None


def _with_disposition(event, *, field_name, disposition, reason):
    """Rebuild a frozen TypedEvent adding one field disposition."""
    return TypedEvent(
        event_id=event.event_id, session_id=event.session_id, kind=event.kind,
        provenance=event.provenance, fidelity=event.fidelity,
        field_dispositions=event.field_dispositions + (
            FieldDispositionRecord(
                field_name=field_name, disposition=disposition, reason=reason,
            ),
        ),
        occurred_at=event.occurred_at, ordinal=event.ordinal,
        native_payload_ref=event.native_payload_ref,
        content=event.content, summary=event.summary,
    )


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:128]
    return None
