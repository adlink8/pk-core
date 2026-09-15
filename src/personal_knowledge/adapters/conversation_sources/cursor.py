"""Phase 62-03: Cursor versioned-discovery adapter (family ``cursor``).

Cursor stores session/thread identity in machine-local project/database
artifacts whose schema varies by version. This adapter versions its schema
probe: supported thread/session stores are distinguished from
attribution-only databases, unsafe/ambiguous stores fail closed, and the
single observed family is represented without inventing native semantics.
"""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path, PurePosixPath

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
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    Provenance,
    TypedEvent,
    make_event_id,
)

FAMILY = "cursor"
ADAPTER_VERSION = "1.0.0"
CONTRACT_VERSION = "2"

# Versioned schema probes: supported stores carry thread/session tables.
SUPPORTED_PROBES: dict[str, tuple[str, ...]] = {
    "v1": ("threads", "messages"),
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


def _fidelity(**overrides) -> FidelityProfile:
    levels = dict(_COMPLETE)
    for key, value in overrides.items():
        levels[FidelityDimension[key]] = value
    return FidelityProfile.from_levels(levels)


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        supported_event_kinds=(EventKind.SESSION_LIFECYCLE, EventKind.USER_MESSAGE,
                               EventKind.ASSISTANT_MESSAGE, EventKind.USAGE,
                               EventKind.UNKNOWN_NATIVE),
        supported_relation_kinds=(),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "machine_local_project_database",
            "schema_probes": "v1(threads,messages)",
            "ambiguous_stores": "fail_closed",
        },
    )


def _probe_schema(db: Path) -> tuple[str | None, set[str]]:
    """Return (probe_version, present_table_names); None version = unsupported."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            con.close()
    except sqlite3.Error:
        return None, set()
    for version, required in SUPPORTED_PROBES.items():
        if set(required) <= tables:
            return version, tables
    return None, tables


def detect(artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    """True for a Cursor store whose schema matches a supported probe version."""
    if artifact.source_kind == "file" and artifact.relative_path.lower().endswith(".jsonl"):
        try:
            with artifact_bytes_path(artifact_root, artifact).open("r", encoding="utf-8") as handle:
                first = next((json.loads(line) for line in handle if line.strip()), {})
            return first.get("role") in ("user", "assistant") and isinstance(
                first.get("message"), (dict, str)
            )
        except (OSError, ValueError):
            return False
    if artifact.source_kind != "sqlite":
        return False
    version, _tables = _probe_schema(artifact_bytes_path(artifact_root, artifact))
    return version is not None


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one supported Cursor store; attribution-only/ambiguous stores
    fail closed with an honest blocked result."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    if artifact.source_kind == "file":
        return _adapt_jsonl(artifact, artifact_root=artifact_root)
    if artifact.source_kind != "sqlite":
        raise EventContractError(f"{FAMILY} adapter requires sqlite or JSONL")

    version, tables = _probe_schema(artifact_root / artifact.content_hash[:32])
    if version is None:
        # Unsafe/ambiguous/attribution-only store: honest blocked result.
        session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                   None, kind=EventKind.SESSION_LIFECYCLE, native_locator="blocked")
        blocked = _fidelity(SOURCE_AVAILABILITY=FidelityLevel.PARTIAL,
                            STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                            CONTENT_AVAILABILITY=FidelityLevel.UNAVAILABLE)
        return AdaptationResult(
            family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
            artifacts=(artifact,), events=(), fidelity=blocked, sessions=(), relations=(),
            warnings=(f"schema not supported by any probe version; present tables: {sorted(tables)[:8]}",),
        )

    session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               None, kind=EventKind.SESSION_LIFECYCLE, native_locator=f"probe:{version}")
    events: list[TypedEvent] = []
    sessions: list[AdaptedSession] = []

    try:
        con = sqlite3.connect(f"file:{artifact_root / artifact.content_hash[:32]}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            threads = con.execute("SELECT * FROM threads").fetchall()
            messages = con.execute("SELECT * FROM messages").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise EventContractError(f"{FAMILY} artifact unreadable: {exc}") from exc

    if not threads:
        return AdaptationResult(
            family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
            artifacts=(artifact,), events=(), fidelity=_fidelity(RELATION_COMPLETENESS=FidelityLevel.PARTIAL),
            sessions=(), relations=(), warnings=("threads table is empty (partial)",),
        )

    message_timestamps = [
        m["created_at"] for m in messages if "created_at" in m.keys() and m["created_at"] is not None
    ]

    for thread in threads:
        tid = str(thread["id"] if "id" in thread.keys() else thread[0])
        locator = f"{artifact.relative_path}#thread:{tid}"
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=locator, native_session_id=tid, native_event_id=tid,
                contract_version=CONTRACT_VERSION,
            ),
            fidelity=_fidelity(), native_session_id=tid,
            started_at=(
                thread["created_at"]
                if "created_at" in thread.keys() and thread["created_at"] is not None
                else None
            ),
            ended_at=message_timestamps[-1] if message_timestamps else None,
        ))
        events.append(TypedEvent(
            event_id=session_id,
            session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=locator, native_session_id=tid, native_event_id=tid,
                contract_version=CONTRACT_VERSION,
            ),
            fidelity=_fidelity(),
            occurred_at=thread["created_at"] if "created_at" in thread.keys() else None,
            summary=str(thread["title"] if "title" in thread.keys() else "")[:256] or None,
        ))

    for message in messages:
        if "role" not in message.keys():
            continue
        role = message["role"]
        kind = EventKind.USER_MESSAGE if role == "user" else (
            EventKind.ASSISTANT_MESSAGE if role in ("assistant", "model") else None)
        locator = f"{artifact.relative_path}#message:{message['id'] if 'id' in message.keys() else len(events)}"
        if kind is None:
            events.append(TypedEvent(
                event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION, locator,
                                       kind=EventKind.UNKNOWN_NATIVE, session_id=session_id),
                session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                provenance=Provenance(
                    artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                    native_locator=locator, native_session_id=session_id,
                    native_event_id=message["id"] if "id" in message.keys() else None,
                    contract_version=CONTRACT_VERSION,
                ),
                fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                   RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                   CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
            ))
            continue
        events.append(TypedEvent(
            event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION, locator,
                                   kind=kind, session_id=session_id),
            session_id=session_id, kind=kind,
            provenance=Provenance(
                artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
                native_locator=locator, native_session_id=session_id,
                native_event_id=message["id"] if "id" in message.keys() else None,
                contract_version=CONTRACT_VERSION,
            ),
            fidelity=_fidelity(),
            occurred_at=message["created_at"] if "created_at" in message.keys() else None,
            content=(
                None if "content" not in message.keys() or message["content"] is None
                else str(message["content"])
            ),
        ))

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events), fidelity=_fidelity(),
        sessions=tuple(sessions), relations=(), warnings=(),
    )


_USAGE_KEYS = (
    "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
    "cache_read", "cache_write",
)


def _usage_event(artifact, *, row, nested_usage=None, native_session, session_id,
                 partial, index) -> TypedEvent:
    """Emit a machine-parseable USAGE event for a Cursor jsonl row with token data."""
    usage = nested_usage
    if usage is None:
        usage = {
            key: row[key] for key in _USAGE_KEYS
            if key in row and isinstance(row[key], (int, float))
        }
    parts = []
    for key, value in (usage.items() if isinstance(usage, dict) else ()):
        if isinstance(value, (int, float)):
            parts.append(f"{key}={int(value)}")
    locator = f"{artifact.relative_path}#usage:{index}"
    return TypedEvent(
        event_id=make_event_id(
            FAMILY, artifact.artifact_id, CONTRACT_VERSION, None,
            kind=EventKind.USAGE, session_id=session_id, native_locator=locator,
        ),
        session_id=session_id, kind=EventKind.USAGE,
        provenance=Provenance(
            artifact_id=artifact.artifact_id,
            artifact_hash=artifact.content_hash,
            native_locator=locator, native_session_id=native_session,
            contract_version=CONTRACT_VERSION,
        ),
        fidelity=_fidelity(
            RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
            COMPACTION_VISIBILITY=FidelityLevel.UNKNOWN,
            NATIVE_ID_STABILITY=FidelityLevel.PARTIAL,
            CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
        ),
        ordinal=index,
        content=None,
        summary=" ".join(parts) or None,
        native_payload_ref=f"{artifact.artifact_id}:{locator}",
    )


def _jsonl_project_cwd(relative_path):
    """Restore the Cursor project dir name encoded in the transcript path."""
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    for i, part in enumerate(parts):
        if part == "agent-transcripts" and (i + 1) < len(parts):
            return parts[i + 1]
    parent = PurePosixPath(relative_path.replace("\\", "/")).parent
    return parent.name or None


def _first_model(rows):
    """Best-effort first model id observed in the transcript records (if any).

    Cursor transcripts expose the model name under a few variant keys at the
    record level ("model" / "model_id" / "modelID") or nested inside the
    "message" object. Extraction is best-effort: transcripts that carry no
    model leave AdaptedSession.model as None rather than guessing.
    """
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = row.get("model")
        if candidate is None:
            candidate = row.get("model_id") or row.get("modelID")
        if candidate is None:
            message = row.get("message")
            if isinstance(message, dict):
                candidate = (message.get("model") or message.get("model_id")
                             or message.get("modelID") or message.get("model_name"))
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:256]
        if isinstance(candidate, (int, float)):
            return str(candidate)[:256]
    return None


def _first_user_message(rows):
    """Use the first user message as the session title (bounded)."""
    for row in rows:
        if row.get("role") != "user":
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, str) and content.strip():
            return content.strip()[:256]
    return None


def _adapt_jsonl(artifact: SourceArtifact, *, artifact_root: Path) -> AdaptationResult:
    rows: list[dict] = []
    try:
        for line in (artifact_root / artifact.content_hash[:32]).read_text(
            encoding="utf-8"
        ).splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    except (OSError, ValueError) as exc:
        raise EventContractError(f"{FAMILY} JSONL artifact unreadable: {exc}") from exc

    native_session = Path(artifact.relative_path).stem
    session_id = make_event_id(
        FAMILY, artifact.artifact_id, CONTRACT_VERSION, native_session,
        kind=EventKind.SESSION_LIFECYCLE,
    )
    partial = _fidelity(
        RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
        COMPACTION_VISIBILITY=FidelityLevel.UNKNOWN,
        NATIVE_ID_STABILITY=FidelityLevel.PARTIAL,
    )
    session_locator = f"{artifact.relative_path}#session"
    provenance = Provenance(
        artifact_id=artifact.artifact_id, artifact_hash=artifact.content_hash,
        native_locator=session_locator, native_session_id=native_session,
        native_event_id=native_session, contract_version=CONTRACT_VERSION,
    )
    events = [TypedEvent(
        event_id=session_id, session_id=session_id,
        kind=EventKind.SESSION_LIFECYCLE, provenance=provenance,
        fidelity=partial,
    )]
    unknown = 0
    for index, row in enumerate(rows, start=1):
        role = row.get("role")
        kind = (
            EventKind.USER_MESSAGE if role == "user" else
            EventKind.ASSISTANT_MESSAGE if role == "assistant" else
            EventKind.TURN_BOUNDARY if row.get("type") == "turn_ended" else
            EventKind.UNKNOWN_NATIVE
        )
        if kind is EventKind.UNKNOWN_NATIVE:
            unknown += 1
        message = row.get("message")
        if isinstance(message, dict):
            source_content = message.get("content")
        else:
            source_content = message
        exact_content = None if source_content is None else str(source_content)
        is_message = kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
        locator = f"{artifact.relative_path}#L{index}"
        events.append(TypedEvent(
            event_id=make_event_id(
                FAMILY, artifact.artifact_id, CONTRACT_VERSION, None,
                kind=kind, session_id=session_id, native_locator=locator,
            ),
            session_id=session_id, kind=kind,
            provenance=Provenance(
                artifact_id=artifact.artifact_id,
                artifact_hash=artifact.content_hash,
                native_locator=locator, native_session_id=native_session,
                contract_version=CONTRACT_VERSION,
            ),
            fidelity=partial if kind is EventKind.UNKNOWN_NATIVE else _fidelity(
                RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                COMPACTION_VISIBILITY=FidelityLevel.UNKNOWN,
                NATIVE_ID_STABILITY=FidelityLevel.PARTIAL,
            ),
            ordinal=index,
            content=exact_content if is_message else None,
            summary=None if is_message else (
                exact_content[:2048] or None if exact_content is not None else None
            ),
            native_payload_ref=f"{artifact.artifact_id}:{locator}",
        ))
        usage = row.get("usage")
        if isinstance(usage, dict) or isinstance(usage, (int, float)) or any(
            k in row for k in ("input_tokens", "output_tokens", "prompt_tokens",
                               "completion_tokens", "cache_read", "cache_write")
        ):
            events.append(_usage_event(
                artifact=artifact, row=row, nested_usage=usage,
                native_session=native_session, session_id=session_id,
                partial=partial, index=index,
            ))
    warnings = (
        (f"{unknown} unknown native record(s) preserved",) if unknown else ()
    )
    # 无 message 行的 transcript：不产生幽灵会话（与 _adapt 空 threads 行为对齐）。
    message_count = sum(
        1 for e in events
        if e.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
    )
    if message_count == 0:
        return AdaptationResult(
            family=FAMILY, adapter_version="1.1.0",
            contract_version=CONTRACT_VERSION, artifacts=(artifact,),
            sessions=(), events=(), relations=(), fidelity=partial,
            warnings=warnings + ("empty transcript: no messages, session skipped",),
        )
    project_cwd = _jsonl_project_cwd(artifact.relative_path)
    row_timestamps = [r.get("timestamp") for r in rows if r.get("timestamp")]
    return AdaptationResult(
        family=FAMILY, adapter_version="1.1.0",
        contract_version=CONTRACT_VERSION, artifacts=(artifact,),
        sessions=(AdaptedSession(
            session_id=session_id, provenance=provenance,
            fidelity=partial, native_session_id=native_session,
            started_at=row_timestamps[0] if row_timestamps else None,
            ended_at=row_timestamps[-1] if row_timestamps else None,
            cwd=project_cwd, model=_first_model(rows),
            title=_first_user_message(rows),
        ),), events=tuple(events), relations=(), fidelity=partial,
        warnings=warnings,
    )
