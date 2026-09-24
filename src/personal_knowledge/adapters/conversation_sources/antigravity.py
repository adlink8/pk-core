"""Phase 62-03: Antigravity SQLite trajectory adapter (family ``antigravity``).

Antigravity stores a trajectory/step/subtrajectory hierarchy in SQLite
(62-RESEARCH format matrix). Capture is allowlisted so adjacent
credential tables are unreachable; this adapter reads only the declared
hierarchy tables. Hierarchical trajectory relations are preserved as typed
``parent_child`` / ``subagent`` relations, with explicit partial transcript
fidelity for step kinds that are not user/assistant prose.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
    CapabilityDescriptor,
    SourceArtifact,
    SourceArtifactSet,
    artifact_bytes_path,
)
from personal_knowledge.adapters.conversation_sources.protobuf_wire import (
    PbMessage,
    WireFormatError,
    as_text,
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

FAMILY = "antigravity"
# 1.2.0 decodes the live store's binary protobuf ``step_payload`` column from the
# wire format instead of preserving it by reference (1.1.0).
# 1.2.1 also recovers NUL-padded UTF-16 tool output, reads the ``f140/f1``
# tool-execution annotations, and keeps annotation-only executions as events
# rather than dropping them.
ADAPTER_VERSION = "1.2.1"
CONTRACT_VERSION = "2"

ALLOWED_TABLES: tuple[str, ...] = ("trajectories", "steps", "subtrajectories")
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "trajectories": ("id", "name", "created_at"),
    "steps": ("id", "trajectory_id", "seq", "kind", "content", "metadata", "created_at"),
    "subtrajectories": ("id", "step_id", "parent_trajectory_id", "content", "created_at"),
}
LIVE_ALLOWED_TABLES: tuple[str, ...] = ("trajectory_meta", "steps", "parent_references")
LIVE_ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "trajectory_meta": ("trajectory_id", "cascade_id", "trajectory_type", "source"),
    "steps": ("idx", "step_type", "status", "has_subtrajectory", "metadata", "error_details", "permissions", "task_details", "render_info", "step_payload", "step_format"),
    "parent_references": ("idx", "data"),
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

_STEP_KINDS = {
    "user": EventKind.USER_MESSAGE,
    "assistant": EventKind.ASSISTANT_MESSAGE,
    "tool": EventKind.TOOL_CALL,
    "reasoning": EventKind.REASONING,
    "compaction": EventKind.COMPACTION_SUMMARY,
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
            EventKind.ASSISTANT_MESSAGE, EventKind.REASONING,
            EventKind.TOOL_CALL, EventKind.COMPACTION_SUMMARY,
            EventKind.SUBAGENT_BOUNDARY, EventKind.USAGE,
            EventKind.UNKNOWN_NATIVE,
        ),
        supported_relation_kinds=(RelationKind.PARENT_CHILD, RelationKind.SUBAGENT),
        fidelity_dimensions=tuple(FidelityDimension),
        capabilities={
            "native_shape": "sqlite_trajectory_store",
            "hierarchy": "trajectory_step_subtrajectory",
            "transcript_fidelity": "decoded_from_protobuf_wire_format",
            "content_availability": "decoded_via_wire_format_when_schema_absent",
            "usage_field_mapping": "raw_field_numbers_no_schema_names",
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
                "AND name IN ('trajectories','trajectory_meta')"
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
           content=None, summary=None, fidelity=None, native_session=None) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                               native_id or locator, kind=kind, session_id=session_id),
        session_id=session_id, kind=kind,
        provenance=_provenance(artifact, locator, session=native_session, native_id=native_id),
        fidelity=fidelity or _fidelity(), occurred_at=occurred_at,
        content=content, summary=summary,
    )


def adapt(artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt one filtered Antigravity snapshot into typed events/relations."""
    if len(artifact_set.artifacts) != 1:
        raise EventContractError(
            f"{FAMILY} adapter requires exactly one artifact, got {len(artifact_set.artifacts)}"
        )
    artifact = artifact_set.artifacts[0]
    if artifact.source_kind != "sqlite":
        raise EventContractError(f"{FAMILY} adapter requires a sqlite artifact")
    try:
        con = sqlite3.connect(f"file:{artifact_root / artifact.content_hash[:32]}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "trajectory_meta" in tables:
                return _adapt_live_store(con, artifact)
            trajectories = con.execute("SELECT * FROM trajectories").fetchall()
            steps = con.execute("SELECT * FROM steps").fetchall()
            sub_flows = con.execute("SELECT * FROM subtrajectories").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise EventContractError(f"{FAMILY} artifact unreadable: {exc}") from exc

    sessions: list[AdaptedSession] = []
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    by_step: dict[str, TypedEvent] = {}
    by_trajectory: dict[str, str] = {}
    unknown = 0

    for trajectory in trajectories:
        sid = str(trajectory["id"])
        session_id = make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                   sid, kind=EventKind.SESSION_LIFECYCLE)
        by_trajectory[sid] = session_id
        sessions.append(AdaptedSession(
            session_id=session_id,
            provenance=_provenance(artifact, f"{artifact.relative_path}#trajectory:{sid}",
                                   session=sid, native_id=sid),
            fidelity=_fidelity(), native_session_id=sid, started_at=trajectory["created_at"],
            title=str(trajectory["name"])[:256] if trajectory["name"] else None,
        ))
        events.append(_event(artifact, session_id=session_id, kind=EventKind.SESSION_LIFECYCLE,
                             locator=f"{artifact.relative_path}#trajectory:{sid}", native_id=sid,
                             occurred_at=trajectory["created_at"],
                             summary=str(trajectory["name"] or "")[:256] or None, native_session=sid))

    for step in steps:
        sid = str(step["trajectory_id"])
        session_id = by_trajectory.get(sid)
        if session_id is None:
            warnings.append(f"step {step['id']!r} references unknown trajectory {sid!r}")
            continue
        kind = _STEP_KINDS.get(step["kind"])
        locator = f"{artifact.relative_path}#step:{step['id']}"
        if kind is None:
            unknown += 1
            ev = _event(artifact, session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                        locator=locator, native_id=step["id"], occurred_at=step["created_at"],
                        fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                                           RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
                                           CONTENT_AVAILABILITY=FidelityLevel.PARTIAL),
                        native_session=sid)
            events.append(ev)
            by_step[str(step["id"])] = ev
            continue
        source_content = step["content"]
        exact_content = None if source_content is None else str(source_content)
        is_message = kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
        ev = _event(artifact, session_id=session_id, kind=kind, locator=locator,
                    native_id=step["id"], occurred_at=step["created_at"],
                    content=exact_content if is_message else None,
                    summary=None if is_message else (exact_content[:2048] or None)
                    if exact_content is not None else None,
                    native_session=sid)
        events.append(ev)
        by_step[str(step["id"])] = ev
        metadata_payload = (step["metadata"]
                            if "metadata" in step.keys() else None)
        usage_payload = (metadata_payload if metadata_payload is not None
                         else step["content"])
        usage_summary = _usage_summary(_json_object(usage_payload))
        if usage_summary:
            events.append(_event(
                artifact, session_id=session_id, kind=EventKind.USAGE,
                locator=f"{locator}#usage", native_id=f"{step['id']}:usage",
                occurred_at=step["created_at"], summary=usage_summary,
                native_session=sid,
            ))

    # Subtrajectories are side branches attached to a parent step.
    for sub in sub_flows:
        parent = by_step.get(str(sub["step_id"]))
        if parent is None:
            warnings.append(f"subtrajectory {sub['id']!r} references unknown step {sub['step_id']!r}")
            continue
        ev = _event(artifact, session_id=parent.session_id, kind=EventKind.SUBAGENT_BOUNDARY,
                    locator=f"{artifact.relative_path}#subtrajectory:{sub['id']}",
                    native_id=sub["id"], occurred_at=sub["created_at"],
                    summary=str(sub["content"] or "")[:2048] or None,
                    native_session=parent.provenance.native_session_id)
        events.append(ev)
        relations.append(EventRelation(
            relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                      f"rel-subagent:{ev.event_id}:{parent.event_id}"),
            source_event_id=ev.event_id, target_event_id=parent.event_id,
            relation_kind=RelationKind.SUBAGENT,
        ))

    # Every step belongs to its trajectory's session-lifecycle event.
    for step in steps:
        ev = by_step.get(str(step["id"]))
        if ev is None:
            continue
        session_id = by_trajectory.get(str(step["trajectory_id"]))
        anchor = next((e for e in events if e.session_id == session_id
                       and e.kind is EventKind.SESSION_LIFECYCLE), None)
        if anchor is not None and anchor.event_id != ev.event_id:
            relations.append(EventRelation(
                relation_id=make_event_id(FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                                          f"rel-parent:{ev.event_id}:{anchor.event_id}"),
                source_event_id=ev.event_id, target_event_id=anchor.event_id,
                relation_kind=RelationKind.PARENT_CHILD,
            ))

    if unknown:
        warnings.append(f"{unknown} unknown step kind(s) preserved")

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION, contract_version=CONTRACT_VERSION,
        artifacts=(artifact,), events=tuple(events),
        fidelity=_fidelity(STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL if unknown else FidelityLevel.COMPLETE,
                           RELATION_COMPLETENESS=FidelityLevel.PARTIAL if not relations else FidelityLevel.COMPLETE),
        sessions=tuple(sessions), relations=tuple(relations), warnings=tuple(warnings),
    )


def _adapt_live_store(
    con: sqlite3.Connection, artifact: SourceArtifact
) -> AdaptationResult:
    """Adapt the live Antigravity store (trajectory_meta + steps).

    Live schema: every ``steps.step_payload`` is a binary protobuf ``Step``
    message (``step_format = 0``). No ``.proto`` schema ships with the store, but
    the wire format is self-describing, so the transcript is decoded structurally
    -- see :func:`_decode_live_step` for the field mapping recovered from real
    artifacts. Payloads that are not well-formed protobuf stay preserved by
    reference, never invented. JSON-encoded payloads (future/alternate stores)
    are decoded too.
    """
    trajectories = con.execute("SELECT * FROM trajectory_meta").fetchall()
    steps = con.execute("SELECT * FROM steps ORDER BY idx").fetchall()
    events: list[TypedEvent] = []
    relations: list[EventRelation] = []
    warnings: list[str] = []
    partial = _fidelity(
        STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
        RELATION_COMPLETENESS=FidelityLevel.UNKNOWN,
        CONTENT_AVAILABILITY=FidelityLevel.UNAVAILABLE,
        COMPACTION_VISIBILITY=FidelityLevel.UNKNOWN,
    )
    by_session: dict[str, str] = {}
    # Session records are built once decoding finishes so their fidelity reflects
    # what was actually recovered; the events only need the session id up front.
    session_specs: list[tuple[str, Provenance, str]] = []

    for trajectory in trajectories:
        native_session = str(trajectory["trajectory_id"])
        session_id = make_event_id(
            FAMILY, artifact.artifact_id, CONTRACT_VERSION, native_session,
            kind=EventKind.SESSION_LIFECYCLE,
        )
        by_session[native_session] = session_id
        locator = f"{artifact.relative_path}#trajectory:{native_session}"
        provenance = _provenance(
            artifact, locator, session=native_session, native_id=native_session
        )
        session_specs.append((session_id, provenance, native_session))
        events.append(_event(
            artifact, session_id=session_id,
            kind=EventKind.SESSION_LIFECYCLE, locator=locator,
            native_id=native_session, fidelity=_fidelity(),
            native_session=native_session,
        ))

    # Steps carry no trajectory FK on the live schema: attribute the whole flat
    # step list to the first trajectory (real stores hold a single trajectory),
    # and flag the ambiguity when several trajectories are present.
    if not session_specs:
        return AdaptationResult(
            family=FAMILY, adapter_version=ADAPTER_VERSION,
            contract_version=CONTRACT_VERSION, artifacts=(artifact,),
            sessions=(), events=(), relations=(), fidelity=partial,
            warnings=("live store has no trajectory_meta rows; nothing adapted",),
        )
    anchor_session = session_specs[0][0]
    anchor_native = session_specs[0][2]
    if len(trajectories) > 1:
        warnings.append(
            "live store has multiple trajectory_meta rows but steps carry no "
            "trajectory FK; all steps attributed to the first trajectory"
        )

    protobuf_steps = 0
    decoded_steps = 0
    empty_steps = 0
    undecodable_steps = 0
    annotation_only_results = 0
    json_steps = 0
    unreadable = 0
    call_owner: dict[str, list[str]] = {}
    pending_results: list[tuple[str, str]] = []
    for step in steps:
        idx = int(step["idx"])
        step_locator = f"{artifact.relative_path}#step:{idx}"
        payload_kind, decoded = _classify_step_payload(step["step_payload"])
        origin = f"step_type={step['step_type']};status={step['status']}"

        if payload_kind == "json":
            json_steps += 1
            _emit_json_step(
                events, artifact, anchor_session, anchor_native, step_locator,
                idx, decoded, origin,
            )
            continue

        if payload_kind == "protobuf":
            step_decode = _decode_live_step(
                int(step["step_type"]), bytes(step["step_payload"])
            )
            if step_decode is not None:
                protobuf_steps += 1
                if not step_decode.parts:
                    empty_steps += 1
                    continue
                decoded_steps += 1
                seen_keys: dict[str, int] = {}
                for part in step_decode.parts:
                    kind = _PART_KINDS.get(part.role)
                    if kind is None:
                        continue
                    if part.role == "tool_result" and part.text is None:
                        annotation_only_results += 1
                    # The step is the store's only stable identity for a
                    # payload, and a call id may legitimately repeat across
                    # steps (real artifacts reuse e.g. ``call_285840`` twice),
                    # so the step index is part of the native id while
                    # ``link`` carries the call correlation. A role repeated
                    # within one step would otherwise collide too.
                    key = part.native or part.role
                    seen_keys[key] = seen_keys.get(key, 0) + 1
                    repeat = f":{seen_keys[key]}" if seen_keys[key] > 1 else ""
                    native = f"{anchor_native}:step:{idx}:{key}{repeat}"
                    event = TypedEvent(
                        event_id=make_event_id(
                            FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                            native, kind=kind, session_id=anchor_session,
                        ),
                        session_id=anchor_session, kind=kind,
                        provenance=_provenance(
                            artifact, f"{step_locator}#{part.role}",
                            session=anchor_native, native_id=native,
                        ),
                        fidelity=_decoded_fidelity(),
                        occurred_at=_iso_utc(step_decode.epoch),
                        ordinal=idx,
                        content=part.text,
                        summary=part.summary,
                        field_dispositions=part.dispositions,
                        native_payload_ref=f"{artifact.artifact_id}:{step_locator}",
                    )
                    events.append(event)
                    if part.role == "tool_call" and part.link:
                        call_owner.setdefault(part.link, []).append(event.event_id)
                    elif part.role == "tool_result" and part.link:
                        pending_results.append((part.link, event.event_id))
                continue

            # Not well-formed protobuf: keep the bytes addressable rather than
            # inventing content that cannot actually be recovered.
            undecodable_steps += 1
            events.append(TypedEvent(
                event_id=make_event_id(
                    FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                    f"{anchor_native}:step:{idx}",
                    kind=EventKind.UNKNOWN_NATIVE, session_id=anchor_session,
                ),
                session_id=anchor_session, kind=EventKind.UNKNOWN_NATIVE,
                provenance=_provenance(
                    artifact, step_locator, session=anchor_native,
                    native_id=f"{anchor_native}:step:{idx}",
                ),
                fidelity=partial,
                field_dispositions=(
                    FieldDispositionRecord(
                        "step_payload", FieldDisposition.PRESERVED_BY_REFERENCE,
                        "binary Step payload that is not well-formed protobuf "
                        "wire format; no .proto schema available",
                    ),
                ),
                ordinal=idx,
                native_payload_ref=f"{artifact.artifact_id}:{step_locator}",
                summary=f"{origin};step_format={step['step_format']}",
            ))
            continue

        # empty/unreadable payload
        unreadable += 1
        events.append(TypedEvent(
            event_id=make_event_id(
                FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                f"{anchor_native}:step:{idx}",
                kind=EventKind.UNKNOWN_NATIVE, session_id=anchor_session,
            ),
            session_id=anchor_session, kind=EventKind.UNKNOWN_NATIVE,
            provenance=_provenance(
                artifact, step_locator, session=anchor_native,
                native_id=f"{anchor_native}:step:{idx}",
            ),
            fidelity=partial, ordinal=idx,
            native_payload_ref=f"{artifact.artifact_id}:{step_locator}",
            summary=f"{origin};step_payload=<empty>",
        ))

    if protobuf_steps:
        warnings.append(
            f"{protobuf_steps} protobuf step payload(s) decoded from the wire "
            "format (no .proto schema ships with the store); field numbers are "
            "authoritative, field names follow real-artifact recon"
        )
    if undecodable_steps:
        warnings.append(
            f"{undecodable_steps} step payload(s) were not well-formed protobuf "
            "wire format (no .proto schema ships with the store); preserved by "
            "reference, semantic decode unavailable"
        )
    if empty_steps:
        warnings.append(
            f"{empty_steps} protobuf step payload(s) carried no recoverable "
            "transcript content and produced no event"
        )
    if annotation_only_results:
        warnings.append(
            f"{annotation_only_results} tool execution step(s) carried no "
            "recoverable result text; their f140/f1 annotations were recorded "
            "in the event summary and the step payload stays addressable"
        )
    if json_steps:
        warnings.append(f"{json_steps} step payload(s) decoded from JSON")
    if unreadable:
        warnings.append(f"{unreadable} step payload(s) were empty/unreadable")

    linked = 0
    unmatched = 0
    for call_id, result_event_id in pending_results:
        owners = call_owner.get(call_id)
        if not owners:
            unmatched += 1
            continue
        # A call id can repeat within one store, so owners are consumed in
        # step order: the earliest unclaimed call is the result's owner.
        call_event_id = owners.pop(0)
        linked += 1
        relations.append(EventRelation(
            relation_id=make_event_id(
                FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                f"rel-call-result:{call_id}:{linked}",
            ),
            source_event_id=result_event_id, target_event_id=call_event_id,
            relation_kind=RelationKind.CALL_RESULT,
        ))
    if unmatched:
        warnings.append(
            f"{unmatched} tool result(s) had no matching tool call "
            "in the same store"
        )

    recovered = decoded_steps > 0
    session_fidelity = _fidelity(
        STRUCTURE_COMPLETENESS=(
            FidelityLevel.COMPLETE if recovered else FidelityLevel.PARTIAL
        ),
        RELATION_COMPLETENESS=(
            FidelityLevel.PARTIAL if relations else FidelityLevel.UNKNOWN
        ),
        CONTENT_AVAILABILITY=(
            FidelityLevel.COMPLETE if recovered else FidelityLevel.UNAVAILABLE
        ),
        COMPACTION_VISIBILITY=FidelityLevel.COMPLETE,
    )
    sessions = tuple(
        AdaptedSession(
            session_id=session_id, provenance=provenance,
            fidelity=session_fidelity, native_session_id=native_session,
        )
        for session_id, provenance, native_session in session_specs
    )

    return AdaptationResult(
        family=FAMILY, adapter_version=ADAPTER_VERSION,
        contract_version=CONTRACT_VERSION, artifacts=(artifact,),
        sessions=sessions, events=tuple(events),
        relations=tuple(relations),
        fidelity=session_fidelity,
        warnings=tuple(warnings),
    )


class _Part(NamedTuple):
    """One recoverable content fragment of a protobuf step."""

    role: str
    text: str | None = None
    native: str | None = None
    summary: str | None = None
    dispositions: tuple = ()
    link: str | None = None
    """Correlation key joining a tool result to its tool call (the call id).
    Kept separate from ``native`` because the native id must stay unique per
    event while the call id is deliberately shared across the call and result.
    """


class _StepDecode(NamedTuple):
    """A decoded step: its timestamp plus every content fragment found."""

    epoch: int | None
    parts: tuple[_Part, ...]


# Protobuf step roles -> typed conversation event kinds.
_PART_KINDS = {
    "user": EventKind.USER_MESSAGE,
    "assistant": EventKind.ASSISTANT_MESSAGE,
    "reasoning": EventKind.REASONING,
    "tool_call": EventKind.TOOL_CALL,
    "tool_result": EventKind.TOOL_RESULT,
    "usage": EventKind.USAGE,
    "subagent": EventKind.SUBAGENT_BOUNDARY,
    "compaction": EventKind.COMPACTION_SUMMARY,
    "error": EventKind.UNKNOWN_NATIVE,
}


def _decoded_fidelity() -> FidelityProfile:
    """Fidelity for a fragment recovered from the protobuf wire format."""
    return _fidelity(
        STRUCTURE_COMPLETENESS=FidelityLevel.COMPLETE,
        RELATION_COMPLETENESS=FidelityLevel.PARTIAL,
        CONTENT_AVAILABILITY=FidelityLevel.COMPLETE,
        COMPACTION_VISIBILITY=FidelityLevel.COMPLETE,
    )


def _iso_utc(epoch: int | None) -> str | None:
    """Render the step's ``f5/f1/f1`` epoch seconds as an ISO-8601 UTC stamp."""
    if epoch is None:
        return None
    try:
        moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# Rendering limits for tool-execution annotations (kept small: these are
# narrative labels, and the large values duplicate the tool call arguments).
_ANNOTATION_VALUE_LIMIT = 300
_ANNOTATION_SUMMARY_LIMIT = 1200
# Short human-readable keys used to label a tool result when the result text
# itself is present. Chosen from the real-artifact key census.
_ANNOTATION_LABEL_KEYS = ("toolSummary", "toolAction")


def _annotations(node: PbMessage) -> list[tuple[str, str]]:
    """Decode the repeated ``f140/f1`` annotation pairs of a tool execution.

    Each entry is a ``StringPair`` message: ``f1`` the key (``toolSummary``,
    ``toolAction``, ``CommandLine``, ``TargetFile``, ``CodeContent`` ...), ``f2``
    the value. A key census over the live stores shows these annotations mirror
    the ``step_type=15`` tool-call arguments, so they matter mainly for the
    steps that carry no arguments elsewhere.
    """
    pairs: list[tuple[str, str]] = []
    for value in node.blobs(1):
        pair = PbMessage(value, strict=False)
        key = pair.text(1)
        if not key:
            continue
        pairs.append((key, pair.text(2, lenient=True) or ""))
    return pairs


def _render_annotations(
    pairs: list[tuple[str, str]], *, keys: tuple[str, ...] | None = None,
) -> str | None:
    """Render annotation pairs as ``key=value`` text, bounded in length."""
    chosen = [
        (key, value) for key, value in pairs
        if keys is None or key in keys
    ]
    if not chosen:
        return None
    rendered: list[str] = []
    for key, value in chosen:
        if not value:
            rendered.append(key)
            continue
        if len(value) > _ANNOTATION_VALUE_LIMIT:
            value = value[:_ANNOTATION_VALUE_LIMIT] + "…"
        rendered.append(f"{key}={value}")
    summary = " | ".join(rendered)
    if len(summary) > _ANNOTATION_SUMMARY_LIMIT:
        summary = summary[:_ANNOTATION_SUMMARY_LIMIT] + "…"
    return summary


_ANNOTATIONS_REFERENCE_ONLY = (
    FieldDispositionRecord(
        "step_payload.f140.f2",
        FieldDisposition.PRESERVED_BY_REFERENCE,
        "tool result text absent or not recoverable text; the step payload "
        "stays addressable through the event's payload reference. Any "
        "f140/f1 annotations recovered by raw field number are recorded in "
        "the event summary (the store ships no .proto schema to name them)",
    ),
)


def _decode_live_step(step_type: int, payload: bytes) -> _StepDecode | None:
    """Decode one binary protobuf ``Step`` payload into content fragments.

    The store ships no ``.proto`` schema, so field *numbers* are authoritative
    and the semantic mapping below was recovered from real artifacts (70 stores,
    ~1.3k user turns, ~12.5k model turns, ~21k tool calls):

    =========  ===============================================================
    step_type  layout
    =========  ===============================================================
    14         ``f19/f2`` user prompt; ``f19/f7|f9|f11`` attachments, whose
               ``f1`` is a ``file://``/``http`` URI and whose ``f11/f2/f1``
               carries attached prose
    15         ``f20/f1`` assistant reply (``f20/f8`` mirrors it, so it is
               skipped); ``f20/f3`` reasoning trace; ``f20/f7`` tool call
               ``{f1: call_id, f2: tool name, f3: JSON arguments}``;
               ``f5/f9`` token counters
    132        ``f5/f4`` tool call ``{f1: call_id, f2: tool name, f3: arguments}``;
               ``f140/f1`` repeated execution annotations ``{f1: key, f2:
               value}`` (``toolSummary``/``toolAction``/``CommandLine``/...),
               and ``f140/f2/f1`` the textual tool result
    17         ``f24/f3`` execution error ``{f1: title, f2: detail, f7: HTTP}``
    23         ``f30/f5`` compaction summary body (the ``<summary>`` tag is
               present in only a minority of them); ``f30/f4`` the session
               title on the steps that carry no body
    101        ``f114/f2/f1`` message title, ``f114/f2/f2`` message body,
               ``f114/f3`` message kind
    =========  ===============================================================

    Returns ``None`` when the payload is not well-formed protobuf, letting the
    caller fall back to preserving the bytes by reference.
    """
    try:
        top = PbMessage(payload, strict=True)
    except WireFormatError:
        return None

    parts: list[_Part] = []
    meta = top.sub1(5, strict=False)
    epoch: int | None = None
    if meta is not None:
        stamp = meta.sub1(1, strict=False)
        if stamp is not None:
            epoch = stamp.integer(1)

    if step_type == 14:
        node = top.sub1(19, strict=False)
        if node is not None:
            texts: list[str] = []
            attachments: list[str] = []
            for number, wire_type, value in node.fields:
                if wire_type != 2:
                    continue
                if number == 2:
                    text = as_text(value, lenient=True)
                    if text:
                        texts.append(text)
                elif number in (7, 9, 11):
                    child = PbMessage(value, strict=False)
                    uri = child.text(1)
                    if uri and "://" in uri:
                        attachments.append(uri)
                    nested = child.sub1(2, strict=False)
                    if nested is not None:
                        attached = nested.text(1)
                        if attached:
                            texts.append(attached)
            if texts or attachments:
                parts.append(_Part(
                    role="user",
                    text="\n\n".join(texts) or None,
                    summary=(
                        "attachments: " + " ".join(attachments)
                        if attachments else None
                    ),
                ))

    elif step_type == 15:
        for blob in top.blobs(20):
            node = PbMessage(blob, strict=False)
            for number, wire_type, value in node.fields:
                if wire_type != 2:
                    continue
                if number == 1:
                    text = as_text(value, lenient=True)
                    if text:
                        parts.append(_Part(role="assistant", text=text))
                elif number == 3:
                    text = as_text(value, lenient=True)
                    if text:
                        parts.append(_Part(role="reasoning", text=text))
                elif number == 7:
                    call = PbMessage(value, strict=False)
                    call_id = call.text(1)
                    tool = call.text(2)
                    arguments = call.text(3)
                    if call_id or tool:
                        parts.append(_Part(
                            role="tool_call", text=arguments,
                            native=f"call:{call_id}" if call_id else None,
                            summary=tool, link=call_id,
                        ))
        if meta is not None:
            usage = meta.sub1(9, strict=False)
            if usage is not None:
                counters = [
                    (number, value) for number, wire_type, value in usage.fields
                    if wire_type == 0 and number not in (1, 6)
                ]
                if counters:
                    parts.append(_Part(
                        role="usage",
                        summary=" ".join(f"f{n}={v}" for n, v in counters),
                        dispositions=(FieldDispositionRecord(
                            "step_payload.f5.f9",
                            FieldDisposition.PRESERVED_BY_REFERENCE,
                            "token counters recovered by raw field number; the "
                            "store ships no .proto schema to name them",
                        ),),
                    ))

    elif step_type == 132:
        # Tool execution: f5/f4 identifies the call ({f1: call_id, f2: name,
        # f3: JSON arguments}); f140/f1 carries repeated key/value annotations
        # and f140/f2/f1 the textual result. The call itself is already emitted
        # from the step_type=15 turn, so only the result is produced here and
        # linked back by call_id.
        call_id = None
        if meta is not None:
            request = meta.sub1(4, strict=False)
            if request is not None:
                call_id = request.text(1)
        for blob in top.blobs(140):
            node = PbMessage(blob, strict=False)
            annotations = _annotations(node)
            result_node = node.sub1(2, strict=False)
            text = (
                result_node.text(1, lenient=True)
                if result_node is not None else None
            )
            # When the result text survives it is the content and the
            # annotations only supply a human-readable label. When it does not,
            # the annotations are the only surviving trace of the execution --
            # they are then rendered in full rather than dropped.
            if text is not None:
                summary = _render_annotations(
                    annotations, keys=_ANNOTATION_LABEL_KEYS,
                )
                dispositions: tuple = ()
            else:
                summary = _render_annotations(annotations)
                dispositions = _ANNOTATIONS_REFERENCE_ONLY
                if summary is None and result_node is not None:
                    summary = "tool result is not recoverable text"
            if text is None and summary is None:
                # Neither a result nor annotations: nothing to emit beyond the
                # payload reference the step already carries.
                break
            parts.append(_Part(
                role="tool_result", text=text, summary=summary,
                native=f"call:{call_id}:result" if call_id else None,
                link=call_id, dispositions=dispositions,
            ))
            break

    elif step_type == 17:
        for blob in top.blobs(24):
            node = PbMessage(blob, strict=False)
            error = node.sub1(3, strict=False)
            if error is None:
                continue
            pieces = [p for p in (error.text(1), error.text(2)) if p]
            summary = " | ".join(pieces) or "agent execution error"
            code = error.integer(7)
            if code:
                summary += f" (http {code})"
            parts.append(_Part(role="error", summary=summary))
            break

    elif step_type == 23:
        # Compaction / session continuation. The body lives in f30/f5, but the
        # ``<summary>`` tag appears in only a minority of them (26 of 75 real
        # summaries), so the marker cannot be the selector. The steps that
        # carry no body hold a session title in f30/f4 instead. f30/f15 is a
        # transcript.jsonl URI and f30/f19 repeats user prompts already
        # captured as user messages, so neither is content here.
        for blob in top.blobs(30):
            node = PbMessage(blob, strict=False)
            body = node.text(5, lenient=True)
            if body:
                parts.append(_Part(role="compaction", text=body))
                break
            title = node.text(4, lenient=True)
            if title:
                parts.append(_Part(role="compaction", summary=title))
                break

    elif step_type == 101:
        for blob in top.blobs(114):
            node = PbMessage(blob, strict=False)
            body = node.sub1(2, strict=False)
            title = body.text(1) if body is not None else None
            text = body.text(2) if body is not None else None
            kind = node.text(3)
            if text:
                parts.append(_Part(
                    role="subagent", text=text, summary=title or kind,
                ))
            elif title:
                parts.append(_Part(role="subagent", summary=title))

    return _StepDecode(epoch=epoch, parts=tuple(parts))


def _classify_step_payload(raw) -> tuple:
    """Classify a steps.step_payload blob -> (kind, decoded)."""
    if raw is None:
        return "empty", None
    data = bytes(raw)
    if not data:
        return "empty", None
    try:
        text = data.decode("utf-8").strip()
        if text[:1] in ("{", "["):
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return "json", parsed
    except (UnicodeDecodeError, ValueError):
        pass
    return "protobuf", None


def _emit_json_step(
    events: list, artifact, session_id: str, native_session: str,
    step_locator: str, idx: int, payload, origin: str,
) -> None:
    """Map one JSON step payload to typed events in-place."""
    role_map = {
        "user": EventKind.USER_MESSAGE,
        "assistant": EventKind.ASSISTANT_MESSAGE,
        "tool": EventKind.TOOL_CALL,
        "reasoning": EventKind.REASONING,
        "compaction": EventKind.COMPACTION_SUMMARY,
    }
    if isinstance(payload, dict):
        # Tolerate a wrapper list under common keys (messages/steps/items).
        for wrap_key in ("messages", "items", "parts", "steps"):
            wrapped = payload.get(wrap_key)
            if isinstance(wrapped, list):
                payload = wrapped
                break
        else:
            payload = [payload]
    elif isinstance(payload, list):
        payload = payload
    else:
        payload = [payload]
    items = payload
    for n, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("kind")
        kind = role_map.get(role) if isinstance(role, str) else None
        content = item.get("content")
        native = f"{native_session}:step:{idx}:{n}"
        locator = f"{step_locator}#part:{n}"
        if kind is None:
            events.append(TypedEvent(
                event_id=make_event_id(
                    FAMILY, artifact.artifact_id, CONTRACT_VERSION, native,
                    kind=EventKind.UNKNOWN_NATIVE, session_id=session_id,
                ),
                session_id=session_id, kind=EventKind.UNKNOWN_NATIVE,
                provenance=_provenance(
                    artifact, locator, session=native_session, native_id=native,
                ),
                fidelity=_fidelity(
                    STRUCTURE_COMPLETENESS=FidelityLevel.PARTIAL,
                    CONTENT_AVAILABILITY=FidelityLevel.PARTIAL,
                ),
                ordinal=idx, summary=f"{origin};role={role!r}",
            ))
            continue
        is_message = kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
        text = None if content is None else str(content)
        events.append(TypedEvent(
            event_id=make_event_id(
                FAMILY, artifact.artifact_id, CONTRACT_VERSION, native,
                kind=kind, session_id=session_id,
            ),
            session_id=session_id, kind=kind,
            provenance=_provenance(
                artifact, locator, session=native_session, native_id=native,
            ),
            fidelity=_fidelity(
                CONTENT_AVAILABILITY=(
                    FidelityLevel.COMPLETE if text is not None else FidelityLevel.UNAVAILABLE
                ),
            ),
            ordinal=idx,
            content=text if (is_message and text is not None) else None,
            summary=(
                None if (is_message and text is not None)
                else (text[:2048] or None if text is not None else origin)
            ),
        ))
        usage = _usage_summary(item)
        if usage:
            events.append(TypedEvent(
                event_id=make_event_id(
                    FAMILY, artifact.artifact_id, CONTRACT_VERSION,
                    f"{native}:usage", kind=EventKind.USAGE, session_id=session_id,
                ),
                session_id=session_id, kind=EventKind.USAGE,
                provenance=_provenance(
                    artifact, f"{locator}#usage", session=native_session,
                    native_id=f"{native}:usage",
                ),
                fidelity=_fidelity(), ordinal=idx, summary=usage,
            ))


def _json_object(value) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage_summary(data) -> str | None:
    """Machine-parseable usage summary from any token-bearing payload (USAGE).

    Emits ``name=value`` pairs for every leaf whose key path mentions a token/
    cache counter, e.g. ``input_tokens=30 output_tokens=9 cache_read=2``.
    Returns None when no token counters are present. Deterministic ordering for
    stable digests.
    """
    counters: dict[str, int] = {}

    def walk(obj, prefix: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                walk(value, f"{prefix}.{key}" if prefix else key)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            name = prefix.split(".")[-1]
            lowered = prefix.lower()
            if "token" in lowered or "cache" in lowered:
                counters[name] = int(obj)

    walk(data)
    if not counters:
        return None
    preferred = (
        "input_tokens", "output_tokens", "total_tokens",
        "cache_read", "cache_write", "cache_creation_input_tokens",
    )
    order = sorted(counters, key=lambda k: (k not in preferred, k))
    return " ".join(f"{name}={counters[name]}" for name in order)
