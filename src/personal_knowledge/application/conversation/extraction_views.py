"""Phase 62-05: deterministic replaceable extraction views.

Phase 62 CONTEXT D-20..D-24: trace is one replaceable view, not evidence
authority. This module owns ONLY deterministic view construction:

  - :class:`EventGraph` — immutable view input (sessions/events/relations)
  - :class:`ViewType` — the seven required derived view kinds (D-21)
  - :class:`DerivedView` + typed subclasses (Turn/NativeTrace/Episode/
    CompactionWindow/Session/Topic/CrossSession)
  - :func:`make_view_id` — stable ids derived from generation + builder
    version + ordered evidence event set; views rebuild without re-running
    adapters (D-21)
  - seven builder functions plus :func:`build_all_views` orchestration

Hard rules:
  - ``NativeTraceView`` uses explicit source-native trace/turn/loop ids only;
    adjacency guesses are never labeled native (D-20).
  - Unknown/partial relations reduce view fidelity instead of being reported
    complete (D-13/D-24).
  - Compaction summaries are navigation signals with exact event lineage, never
    self-authenticating truth (D-23/D-24).
  - Every view exposes exact ``evidence_event_refs``; candidates derived from a
    view retain both lineage and stable event refs (D-24).

No I/O, no network, no provider calls (D-31).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    EventRelation,
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    RelationKind,
    TypedEvent,
)

BUILDER_VERSION = "1"

# Relations that bind events into an execution episode (D-20 grouping rule).
_EPISODE_RELATIONS: frozenset[RelationKind] = frozenset(
    {
        RelationKind.TURN_MEMBERSHIP,
        RelationKind.PARENT_CHILD,
        RelationKind.BRANCH,
        RelationKind.SIDECHAIN,
        RelationKind.SUBAGENT,
    }
)

# Source-native boundary kinds that anchor a native trace (D-20).
_NATIVE_TRACE_ANCHOR_KINDS: frozenset[EventKind] = frozenset(
    {EventKind.TURN_BOUNDARY, EventKind.LOOP_BOUNDARY, EventKind.SUBAGENT_BOUNDARY}
)

# Boundary kinds that anchor a turn view.
_TURN_ANCHOR_KINDS: frozenset[EventKind] = frozenset(
    {EventKind.TURN_BOUNDARY, EventKind.LOOP_BOUNDARY}
)

# Relations that can link events across sessions (cross-session view).
_CROSS_SESSION_RELATIONS: frozenset[RelationKind] = frozenset(
    {
        RelationKind.SOURCE_SESSION_CROSSWALK,
        RelationKind.BRANCH,
        RelationKind.SIDECHAIN,
        RelationKind.SUBAGENT,
        RelationKind.PARENT_CHILD,
    }
)


class ViewType(str, Enum):
    """The seven required derived view kinds (Phase 62 D-21)."""

    TURN = "turn"
    NATIVE_TRACE = "native_trace"
    EPISODE = "episode"
    COMPACTION_WINDOW = "compaction_window"
    SESSION = "session"
    TOPIC = "topic"
    CROSS_SESSION = "cross_session"


class ViewBuilderError(RuntimeError):
    """A view builder received an inconsistent event graph."""


@dataclass(frozen=True)
class EventGraph:
    """Immutable input for deterministic view construction (one generation)."""

    generation_id: str
    events: tuple[TypedEvent, ...]
    relations: tuple[EventRelation, ...] = ()
    sessions: tuple[AdaptedSession, ...] = ()
    builder_version: str = BUILDER_VERSION

    def __post_init__(self) -> None:
        if not self.generation_id:
            raise ViewBuilderError("event graph requires a generation id")
        known = {e.event_id for e in self.events}
        for rel in self.relations:
            if rel.source_event_id not in known or rel.target_event_id not in known:
                raise ViewBuilderError(
                    f"relation {rel.relation_id} references an event outside "
                    "the generation"
                )


@dataclass(frozen=True)
class ContradictionSlot:
    """A deterministic contradiction identified between evidence events."""

    slot_id: str
    kind: str
    refs: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"slot_id": self.slot_id, "kind": self.kind, "refs": list(self.refs)}


@dataclass(frozen=True)
class DerivedView:
    """A versioned, lineage-carrying derived view (D-21/D-24)."""

    view_id: str
    view_type: ViewType
    generation_id: str
    builder_version: str
    session_id: str | None
    members: tuple[str, ...]
    evidence_event_refs: tuple[str, ...]
    lineage: tuple[str, ...]
    fidelity: FidelityProfile
    contradictions: tuple[ContradictionSlot, ...]
    metadata: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {
            "view_id": self.view_id,
            "view_type": self.view_type.value,
            "generation_id": self.generation_id,
            "builder_version": self.builder_version,
            "session_id": self.session_id,
            "members": list(self.members),
            "evidence_event_refs": list(self.evidence_event_refs),
            "lineage": list(self.lineage),
            "fidelity": self.fidelity.to_dict(),
            "contradictions": [c.to_dict() for c in self.contradictions],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TurnView(DerivedView):
    """One native turn: a boundary anchor plus its turn-membership events."""


@dataclass(frozen=True)
class NativeTraceView(DerivedView):
    """A source-native bounded execution episode (D-20)."""

    native_trace_id: str


@dataclass(frozen=True)
class EpisodeView(DerivedView):
    """A policy-derived episode; versioned deterministic heuristics allowed."""

    heuristics: tuple[str, ...]


@dataclass(frozen=True)
class CompactionWindowView(DerivedView):
    """A compaction summary related to its compacted/retained event sets."""

    summary_event_id: str
    compacted_event_refs: tuple[str, ...]
    retained_event_refs: tuple[str, ...]


@dataclass(frozen=True)
class SessionView(DerivedView):
    """Whole-session member/event lineage with contradiction slots."""

    member_view_ids: tuple[str, ...]


@dataclass(frozen=True)
class TopicView(DerivedView):
    """A deterministic topic anchored at a summary or file-context event."""

    topic_key: str


@dataclass(frozen=True)
class CrossSessionView(DerivedView):
    """Sessions linked by explicit cross-session relations or id collisions."""

    session_ids: tuple[str, ...]


@dataclass(frozen=True)
class ViewBuildResult:
    """Deterministic result of building every view for one generation."""

    generation_id: str
    builder_version: str
    views: tuple[DerivedView, ...]
    digest: str

    def views_by_type(self) -> dict[ViewType, tuple[DerivedView, ...]]:
        grouped: dict[ViewType, list[DerivedView]] = {
            vt: [] for vt in ViewType
        }
        for view in self.views:
            grouped[view.view_type].append(view)
        return {vt: tuple(vs) for vt, vs in grouped.items()}


# --------------------------------------------------------------- identity

def make_view_id(
    view_type: ViewType,
    generation_id: str,
    builder_version: str,
    evidence_event_refs: tuple[str, ...],
) -> str:
    """Stable view identity derived from generation + builder version +
    ordered evidence event set (D-21). Rebuilds are identical."""
    ordered = tuple(sorted(set(evidence_event_refs)))
    payload = "|".join(
        [view_type.value, generation_id, builder_version, *ordered]
    )
    return "view:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_contradiction_id(kind: str, *refs: str) -> str:
    payload = "|".join([kind, *sorted(set(refs))])
    return "contra:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def view_set_digest(result: ViewBuildResult) -> str:
    """Deterministic digest over the full view set (view ids + evidence)."""
    payload = {
        "generation_id": result.generation_id,
        "builder_version": result.builder_version,
        "views": sorted(
            (
                (v.view_id, tuple(sorted(v.evidence_event_refs)))
                for v in result.views
            )
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------- helpers

def _by_event_id(graph: EventGraph) -> dict[str, TypedEvent]:
    return {e.event_id: e for e in graph.events}


def _events_by_session(graph: EventGraph) -> dict[str, list[TypedEvent]]:
    grouped: dict[str, list[TypedEvent]] = {}
    for event in graph.events:
        grouped.setdefault(event.session_id, []).append(event)
    return grouped


def _session_events(graph: EventGraph, session_id: str) -> list[TypedEvent]:
    return [e for e in graph.events if e.session_id == session_id]


def _sorted_events(events) -> list[TypedEvent]:
    return sorted(
        events,
        key=lambda e: (
            e.ordinal is None,
            e.ordinal if e.ordinal is not None else 0,
            e.event_id,
        ),
    )


def _outgoing(
    graph: EventGraph, event_id: str, kinds: frozenset[RelationKind]
) -> tuple[EventRelation, ...]:
    return tuple(
        r for r in graph.relations
        if r.source_event_id == event_id and r.relation_kind in kinds
    )


def _outgoing_index(graph: EventGraph) -> dict[str, list[EventRelation]]:
    grouped: dict[str, list[EventRelation]] = {}
    for relation in graph.relations:
        grouped.setdefault(relation.source_event_id, []).append(relation)
    return grouped


def _evidence_refs(events: list[TypedEvent]) -> tuple[str, ...]:
    return tuple(e.event_id for e in _sorted_events(events))


def _member_loss(events: list[TypedEvent]) -> bool:
    return any(e.fidelity.has_loss() for e in events)


def _aggregate_fidelity(
    members: list[TypedEvent],
    *,
    native_ids_ok: bool = True,
    relation_ok: bool = True,
    compaction_visible: bool = True,
    forced_partial: tuple[str, ...] = (),
) -> FidelityProfile:
    """Combine member fidelities into one honest view fidelity (D-13).

    Any member loss or missing native boundary/relation reduces the view;
    ``partial/unknown`` is never reported as complete.
    """
    loss = _member_loss(members)
    forced = set(forced_partial)
    levels: dict[FidelityDimension, FidelityLevel] = {}
    for dim in FidelityDimension:
        if dim.value in forced:
            levels[dim] = FidelityLevel.PARTIAL
    levels.setdefault(
        FidelityDimension.SOURCE_AVAILABILITY,
        FidelityLevel.COMPLETE
        if members and all(e.provenance.resolvable() for e in members)
        else FidelityLevel.PARTIAL,
    )
    levels.setdefault(
        FidelityDimension.STRUCTURE_COMPLETENESS,
        FidelityLevel.PARTIAL if (loss or not members) else FidelityLevel.COMPLETE,
    )
    levels.setdefault(
        FidelityDimension.ORDERING_CONFIDENCE,
        FidelityLevel.COMPLETE
        if all(e.ordinal is not None for e in members)
        else FidelityLevel.PARTIAL,
    )
    levels.setdefault(
        FidelityDimension.RELATION_COMPLETENESS,
        FidelityLevel.PARTIAL if (loss or not relation_ok) else FidelityLevel.COMPLETE,
    )
    levels.setdefault(
        FidelityDimension.CONTENT_AVAILABILITY,
        FidelityLevel.PARTIAL
        if any(
            e.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is not FidelityLevel.COMPLETE
            for e in members
        )
        else FidelityLevel.COMPLETE,
    )
    levels.setdefault(
        FidelityDimension.COMPACTION_VISIBILITY,
        FidelityLevel.COMPLETE if compaction_visible else FidelityLevel.PARTIAL,
    )
    levels.setdefault(
        FidelityDimension.NATIVE_ID_STABILITY,
        FidelityLevel.COMPLETE
        if native_ids_ok
        and all(e.provenance.native_event_id for e in members)
        else FidelityLevel.PARTIAL,
    )
    return FidelityProfile.from_levels(levels)


def _contradictions_for_session(
    graph: EventGraph, session_id: str, by_id: dict[str, TypedEvent],
    *, session_events: list[TypedEvent] | None = None,
) -> tuple[ContradictionSlot, ...]:
    """Deterministic contradiction slots inside one session (D-24)."""
    slots: list[ContradictionSlot] = []
    # 1. an event both retained and compacted by summaries
    retained_targets = {
        r.target_event_id
        for r in graph.relations
        if r.relation_kind is RelationKind.RETAINED_FROM
    }
    compacted_targets = {
        r.target_event_id
        for r in graph.relations
        if r.relation_kind is RelationKind.COMPACTED_RANGE
    }
    for event_id in sorted(retained_targets & compacted_targets):
        if by_id.get(event_id) and by_id[event_id].session_id == session_id:
            slots.append(
                ContradictionSlot(
                    make_contradiction_id("retained_and_compacted", event_id),
                    "retained_and_compacted",
                    (event_id,),
                )
            )
    # 2. same native id mapped to two different canonical event ids
    by_native: dict[str, str] = {}
    for e in session_events if session_events is not None else _session_events(graph, session_id):
        if e.provenance.native_event_id:
            prior = by_native.get(e.provenance.native_event_id)
            if prior is not None and prior != e.event_id:
                slots.append(
                    ContradictionSlot(
                        make_contradiction_id(
                            "native_id_collision",
                            prior,
                            e.event_id,
                        ),
                        "native_id_collision",
                        (prior, e.event_id),
                    )
                )
            by_native[e.provenance.native_event_id] = e.event_id
    return tuple(sorted(slots, key=lambda s: s.slot_id))


# --------------------------------------------------------- TurnView

def build_turn_views(graph: EventGraph) -> tuple[TurnView, ...]:
    """One view per explicit native turn/loop boundary (never fabricated)."""
    by_id = _by_event_id(graph)
    anchors = [
        e for e in graph.events
        if e.kind in _TURN_ANCHOR_KINDS and e.provenance.native_event_id
    ]
    views: list[TurnView] = []
    seen: set[tuple[str, ...]] = set()
    outgoing = _outgoing_index(graph)
    for anchor in _sorted_events(anchors):
        member_ids = {anchor.event_id}
        for rel in outgoing.get(anchor.event_id, ()):
            if rel.relation_kind is not RelationKind.TURN_MEMBERSHIP:
                continue
            member_ids.add(rel.target_event_id)
        members = [by_id[i] for i in sorted(member_ids)]
        evidence = _evidence_refs(members)
        if evidence in seen:
            continue
        seen.add(evidence)
        native = anchor.provenance.native_event_id is not None
        flags = "native_turn" if native else "derived"
        views.append(
            TurnView(
                view_id=make_view_id(
                    ViewType.TURN, graph.generation_id,
                    graph.builder_version, evidence,
                ),
                view_type=ViewType.TURN,
                generation_id=graph.generation_id,
                builder_version=graph.builder_version,
                session_id=anchor.session_id,
                members=evidence,
                evidence_event_refs=evidence,
                lineage=tuple("event:" + i for i in evidence),
                fidelity=_aggregate_fidelity(
                    members,
                    native_ids_ok=native,
                    relation_ok=True,
                ),
                contradictions=(),
                metadata=(
                    ("flags", flags),
                    ("anchor_kind", anchor.kind.value),
                ),
            )
        )
    return tuple(views)


# ----------------------------------------------------- NativeTraceView

def build_native_trace_views(graph: EventGraph) -> tuple[NativeTraceView, ...]:
    """Source-native bounded episodes from explicit native boundary ids.

    Adjacency guesses are never labeled native: without a boundary event that
    carries a native trace/turn/loop id, no native trace view is produced.
    """
    by_id = _by_event_id(graph)
    anchors = [
        e for e in graph.events
        if e.kind in _NATIVE_TRACE_ANCHOR_KINDS
        and e.provenance.native_event_id
    ]
    views: list[NativeTraceView] = []
    seen: set[str] = set()
    events_by_native: dict[str, list[TypedEvent]] = {}
    outgoing = _outgoing_index(graph)
    for event in graph.events:
        if event.provenance.native_event_id:
            events_by_native.setdefault(
                event.provenance.native_event_id, []
            ).append(event)
    for anchor in _sorted_events(anchors):
        native_id = anchor.provenance.native_event_id or ""
        member_ids = {anchor.event_id}
        allowed = frozenset({
            RelationKind.TURN_MEMBERSHIP, RelationKind.BRANCH,
            RelationKind.PARENT_CHILD, RelationKind.SUBAGENT,
            RelationKind.SIDECHAIN,
        })
        for rel in outgoing.get(anchor.event_id, ()):
            if rel.relation_kind not in allowed:
                continue
            member_ids.add(rel.target_event_id)
        for e in events_by_native.get(native_id, ()):
            if e.event_id != anchor.event_id:
                member_ids.add(e.event_id)
        members = [by_id[i] for i in sorted(member_ids)]
        evidence = _evidence_refs(members)
        key = "|".join([native_id, *evidence])
        if key in seen:
            continue
        seen.add(key)
        views.append(
            NativeTraceView(
                view_id=make_view_id(
                    ViewType.NATIVE_TRACE, graph.generation_id,
                    graph.builder_version, evidence,
                ),
                view_type=ViewType.NATIVE_TRACE,
                generation_id=graph.generation_id,
                builder_version=graph.builder_version,
                session_id=anchor.session_id,
                members=evidence,
                evidence_event_refs=evidence,
                lineage=tuple("event:" + i for i in evidence),
                fidelity=_aggregate_fidelity(
                    members, native_ids_ok=True, relation_ok=not _member_loss(members)
                ),
                contradictions=(),
                metadata=(("native_trace_id", native_id),),
                native_trace_id=native_id,
            )
        )
    return tuple(views)


# --------------------------------------------------------- EpisodeView

def build_episode_views(graph: EventGraph) -> tuple[EpisodeView, ...]:
    """Deterministic bounded execution episodes.

    Versioned heuristics (builder version 1): relation-connected components
    within a session first; a whole-session fallback (explicitly partial) when
    no episode relations exist. Native boundary anchors are preferred seeds.
    """
    session_ids = sorted({s.session_id for s in graph.sessions})
    if not session_ids:
        session_ids = sorted({e.session_id for e in graph.events})
    views: list[EpisodeView] = []
    events_by_session = _events_by_session(graph)
    relation_sessions: set[str] = set()
    event_session = {e.event_id: e.session_id for e in graph.events}
    for relation in graph.relations:
        if relation.relation_kind in _EPISODE_RELATIONS:
            if relation.source_event_id in event_session:
                relation_sessions.add(event_session[relation.source_event_id])
            if relation.target_event_id in event_session:
                relation_sessions.add(event_session[relation.target_event_id])
    for session_id in session_ids:
        session_events = events_by_session.get(session_id, [])
        if not session_events:
            continue
        session_event_ids = {e.event_id for e in session_events}
        has_episode_link = session_id in relation_sessions
        if has_episode_link:
            components = _relation_components(graph, session_events)
            fallback = False
        else:
            components = [list(session_events)]
            fallback = True
        for component in components:
            members = [by for by in _sorted_events(component)]
            evidence = _evidence_refs(members)
            heuristics: list[str] = ["relation_component"]
            has_native_anchor = any(
                e.kind in _NATIVE_TRACE_ANCHOR_KINDS
                and e.provenance.native_event_id
                for e in members
            )
            if has_native_anchor:
                heuristics.append("native_boundary")
            if fallback:
                heuristics = ["session_fallback"]
            views.append(
                EpisodeView(
                    view_id=make_view_id(
                        ViewType.EPISODE, graph.generation_id,
                        graph.builder_version, evidence,
                    ),
                    view_type=ViewType.EPISODE,
                    generation_id=graph.generation_id,
                    builder_version=graph.builder_version,
                    session_id=session_id,
                    members=evidence,
                    evidence_event_refs=evidence,
                    lineage=tuple("event:" + i for i in evidence),
                    fidelity=_aggregate_fidelity(
                        members,
                        native_ids_ok=has_native_anchor,
                        relation_ok=not fallback,
                        forced_partial=(
                            ("ordering_confidence", "relation_completeness")
                            if fallback
                            else ()
                        ),
                    ),
                    contradictions=(),
                    metadata=(("heuristics", ",".join(heuristics)),),
                    heuristics=tuple(heuristics),
                )
            )
    return tuple(views)


def _relation_components(
    graph: EventGraph, session_events: list[TypedEvent]
) -> list[list[TypedEvent]]:
    """Connected components over the episode relation kinds (deterministic)."""
    by_id = _by_event_id(graph)
    parents: dict[str, str] = {}
    for e in session_events:
        parents[e.event_id] = e.event_id

    def find(x: str) -> str:
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parents[ra] = rb

    for rel in graph.relations:
        if rel.relation_kind not in _EPISODE_RELATIONS:
            continue
        src, dst = rel.source_event_id, rel.target_event_id
        if src in parents and dst in parents:
            union(src, dst)
    grouped: dict[str, list[TypedEvent]] = {}
    for e in session_events:
        grouped.setdefault(find(e.event_id), []).append(e)
    return [grouped[k] for k in sorted(grouped)]


# ------------------------------------------------ CompactionWindowView

def build_compaction_window_views(graph: EventGraph) -> tuple[CompactionWindowView, ...]:
    """Relate each compaction summary to its compacted/retained event sets.

    A summary with no compacted/retained relations is exposed with partial
    compaction visibility and never claims full coverage (D-23/D-24).
    """
    by_id = _by_event_id(graph)
    summaries = [
        e for e in graph.events if e.kind is EventKind.COMPACTION_SUMMARY
    ]
    views: list[CompactionWindowView] = []
    outgoing = _outgoing_index(graph)
    for summary in _sorted_events(summaries):
        compacted = [
            rel.target_event_id
            for rel in outgoing.get(summary.event_id, ())
            if rel.relation_kind is RelationKind.COMPACTED_RANGE
        ]
        retained = [
            rel.target_event_id
            for rel in outgoing.get(summary.event_id, ())
            if rel.relation_kind is RelationKind.RETAINED_FROM
        ]
        compacted = sorted(set(compacted))
        retained = sorted(set(retained))
        members = [by_id[i] for i in sorted({summary.event_id, *compacted, *retained})]
        evidence = _evidence_refs(members)
        slots: list[ContradictionSlot] = []
        overlap = sorted(set(compacted) & set(retained))
        for event_id in overlap:
            slots.append(
                ContradictionSlot(
                    make_contradiction_id("retained_and_compacted", event_id),
                    "retained_and_compacted",
                    (event_id,),
                )
            )
        visible = bool(compacted or retained)
        views.append(
            CompactionWindowView(
                view_id=make_view_id(
                    ViewType.COMPACTION_WINDOW, graph.generation_id,
                    graph.builder_version, evidence,
                ),
                view_type=ViewType.COMPACTION_WINDOW,
                generation_id=graph.generation_id,
                builder_version=graph.builder_version,
                session_id=summary.session_id,
                members=evidence,
                evidence_event_refs=evidence,
                lineage=tuple("event:" + i for i in evidence),
                fidelity=_aggregate_fidelity(
                    members, compaction_visible=visible, relation_ok=visible
                ),
                contradictions=tuple(slots),
                metadata=(
                    ("summary_event_id", summary.event_id),
                    ("compacted_count", str(len(compacted))),
                    ("retained_count", str(len(retained))),
                ),
                summary_event_id=summary.event_id,
                compacted_event_refs=tuple(compacted),
                retained_event_refs=tuple(retained),
            )
        )
    return tuple(views)


# ---------------------------------------------------------- SessionView

def build_session_views(
    graph: EventGraph, member_views: tuple[DerivedView, ...]
) -> tuple[SessionView, ...]:
    """Whole-session views retaining member view/event lineage (D-24)."""
    by_id = _by_event_id(graph)
    session_ids = sorted({s.session_id for s in graph.sessions})
    if not session_ids:
        session_ids = sorted({e.session_id for e in graph.events})
    views: list[SessionView] = []
    events_by_session = _events_by_session(graph)
    views_by_session: dict[str, list[str]] = {}
    for view in member_views:
        if view.session_id is not None:
            views_by_session.setdefault(view.session_id, []).append(view.view_id)
    for session_id in session_ids:
        session_events = events_by_session.get(session_id, [])
        evidence = _evidence_refs(session_events)
        member_view_ids = tuple(sorted(views_by_session.get(session_id, ())))
        # lineage entries are raw identifiers: view ids already carry the
        # ``view:`` prefix and event ids are content hashes, so no extra prefix
        # is needed and member view/event lineage stays directly comparable.
        lineage = list(member_view_ids)
        lineage.extend(evidence)
        slots = _contradictions_for_session(
            graph, session_id, by_id, session_events=session_events
        )
        views.append(
            SessionView(
                view_id=make_view_id(
                    ViewType.SESSION, graph.generation_id,
                    graph.builder_version, evidence,
                ),
                view_type=ViewType.SESSION,
                generation_id=graph.generation_id,
                builder_version=graph.builder_version,
                session_id=session_id,
                members=evidence,
                evidence_event_refs=evidence,
                lineage=tuple(lineage),
                fidelity=_aggregate_fidelity(
                    session_events,
                    native_ids_ok=True,
                    relation_ok=True,
                    compaction_visible=bool(
                        any(
                            e.kind is EventKind.COMPACTION_SUMMARY
                            for e in session_events
                        )
                    ),
                ),
                contradictions=slots,
                metadata=(
                    ("member_view_count", str(len(member_view_ids))),
                    ("event_count", str(len(evidence))),
                ),
                member_view_ids=member_view_ids,
            )
        )
    return tuple(views)


# ----------------------------------------------------------- TopicView

def build_topic_views(graph: EventGraph) -> tuple[TopicView, ...]:
    """Deterministic topics anchored at compaction summaries / file contexts.

    A topic is only produced from an explicit anchor (summary text or file
    locator); without anchors no topics are fabricated.
    """
    by_id = _by_event_id(graph)
    topics: dict[str, dict] = {}
    _register_topic_anchors(graph, by_id, topics, _outgoing_index(graph))

    views: list[TopicView] = []
    for topic_key in sorted(topics):
        entry = topics[topic_key]
        members = [by_id[i] for i in sorted(entry["members"])]
        evidence = _evidence_refs(members)
        lineage = tuple(sorted(set(entry["lineage"] + ["event:" + i for i in evidence])))
        session_ids = sorted({m.session_id for m in members})
        views.append(
            TopicView(
                view_id=make_view_id(
                    ViewType.TOPIC, graph.generation_id,
                    graph.builder_version, evidence,
                ),
                view_type=ViewType.TOPIC,
                generation_id=graph.generation_id,
                builder_version=graph.builder_version,
                session_id=session_ids[0] if session_ids else None,
                members=evidence,
                evidence_event_refs=evidence,
                lineage=lineage,
                fidelity=_aggregate_fidelity(members),
                contradictions=(),
                metadata=(("topic_key", topic_key),),
                topic_key=topic_key,
            )
        )
    return tuple(views)


def _register_topic_anchors(
    graph: EventGraph,
    by_id: dict[str, TypedEvent],
    topics: dict[str, dict],
    outgoing: dict[str, list[EventRelation]],
) -> None:
    """Populate the topic registry from summary and file-context anchors."""

    def _register(topic_key: str, anchor: TypedEvent, members: list[TypedEvent]) -> None:
        existing = topics.setdefault(
            topic_key, {"anchor": anchor, "members": set(), "lineage": []}
        )
        existing["members"].update(e.event_id for e in members)
        existing["lineage"].append("event:" + anchor.event_id)
        existing["lineage"].append("native:" + (anchor.provenance.native_event_id or ""))

    for summary in _sorted_events(
        [e for e in graph.events if e.kind is EventKind.COMPACTION_SUMMARY]
    ):
        key_src = summary.summary or summary.provenance.native_event_id or summary.event_id
        topic_key = "summary:" + hashlib.sha256(
            key_src.encode("utf-8")
        ).hexdigest()[:16]
        _register(
            topic_key,
            summary,
            _topic_members(
                graph,
                summary,
                by_id,
                outgoing,
                frozenset(
                    {
                        RelationKind.COMPACTED_RANGE,
                        RelationKind.RETAINED_FROM,
                    }
                ),
            ),
        )

    for fc in _sorted_events(
        [e for e in graph.events if e.kind is EventKind.FILE_CONTEXT]
    ):
        locator = fc.native_payload_ref or fc.provenance.native_locator
        topic_key = "file:" + (locator or fc.event_id)
        _register(
            topic_key,
            fc,
            _topic_members(
                graph,
                fc,
                by_id,
                outgoing,
                frozenset(
                    {
                        RelationKind.PARENT_CHILD,
                        RelationKind.BRANCH,
                        RelationKind.SIDECHAIN,
                        RelationKind.SUBAGENT,
                    }
                ),
            ),
        )


def _topic_members(
    graph: EventGraph,
    anchor: TypedEvent,
    by_id: dict[str, TypedEvent],
    outgoing: dict[str, list[EventRelation]],
    relation_kinds: frozenset[RelationKind],
) -> list[TypedEvent]:
    """Anchor plus every relation-linked member used by a topic view."""
    member_ids = {
        rel.target_event_id
        for rel in outgoing.get(anchor.event_id, ())
        if rel.relation_kind in relation_kinds
    }
    member_ids.add(anchor.event_id)
    return [by_id[i] for i in member_ids if i in by_id]


# ---------------------------------------------------- CrossSessionView

def build_cross_session_views(graph: EventGraph) -> tuple[CrossSessionView, ...]:
    """Sessions linked by explicit cross-session relations or id collisions."""
    by_id = _by_event_id(graph)
    collisions = _native_collisions(graph)
    links = _cross_session_links(graph, collisions, by_id)

    parents: dict[str, str] = {}

    def _find(x: str) -> str:
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    for s1, s2 in links:
        parents.setdefault(s1, s1)
        parents.setdefault(s2, s2)
        r1, r2 = _find(s1), _find(s2)
        if r1 != r2:
            parents[r1] = r2

    components: dict[str, set[str]] = {}
    for session_id in parents:
        root = _find(session_id)
        components.setdefault(root, set()).add(session_id)

    views: list[CrossSessionView] = []
    for component in sorted(
        components.values(), key=lambda c: (min(c), max(c))
    ):
        session_ids = tuple(sorted(component))
        if len(session_ids) < 2:
            continue
        member_ids = {
            e.event_id
            for e in graph.events
            if e.session_id in session_ids
        }
        members = [by_id[i] for i in sorted(member_ids)]
        evidence = _evidence_refs(members)
        slots = _collision_slots_for(collisions, session_ids, by_id)
        views.append(
            CrossSessionView(
                view_id=make_view_id(
                    ViewType.CROSS_SESSION, graph.generation_id,
                    graph.builder_version, evidence,
                ),
                view_type=ViewType.CROSS_SESSION,
                generation_id=graph.generation_id,
                builder_version=graph.builder_version,
                session_id=None,
                members=evidence,
                evidence_event_refs=evidence,
                lineage=tuple("session:" + sid for sid in session_ids),
                fidelity=_aggregate_fidelity(members),
                contradictions=slots,
                metadata=(
                    ("session_count", str(len(session_ids))),
                    ("cross_link_count", str(len(links))),
                ),
                session_ids=session_ids,
            )
        )
    return tuple(views)


def _native_collisions(graph: EventGraph) -> list[tuple[str, str]]:
    """Pairs of canonical event ids sharing one native id (deterministic)."""
    by_native: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for e in _sorted_events(graph.events):
        if e.provenance.native_event_id:
            prior = by_native.get(e.provenance.native_event_id)
            if prior is not None and prior != e.event_id:
                collisions.append((prior, e.event_id))
            by_native.setdefault(e.provenance.native_event_id, e.event_id)
    return collisions


def _cross_session_links(
    graph: EventGraph,
    collisions: list[tuple[str, str]],
    by_id: dict[str, TypedEvent],
) -> list[tuple[str, str]]:
    """Session pairs linked by cross-session relations or id collisions."""
    links: list[tuple[str, str]] = []
    for rel in graph.relations:
        if rel.relation_kind not in _CROSS_SESSION_RELATIONS:
            continue
        src, dst = rel.source_event_id, rel.target_event_id
        if by_id.get(src) and by_id.get(dst):
            s1, s2 = by_id[src].session_id, by_id[dst].session_id
            if s1 != s2:
                links.append((s1, s2))
    for a, b in collisions:
        if by_id.get(a) and by_id.get(b):
            s1, s2 = by_id[a].session_id, by_id[b].session_id
            if s1 != s2:
                links.append((s1, s2))
    return links


def _collision_slots_for(
    collisions: list[tuple[str, str]],
    session_ids: tuple[str, ...],
    by_id: dict[str, TypedEvent],
) -> tuple[ContradictionSlot, ...]:
    """Contradiction slots for collisions inside a cross-session component."""
    slots: list[ContradictionSlot] = []
    for a, b in sorted(collisions):
        if (
            by_id[a].session_id in session_ids
            and by_id[b].session_id in session_ids
            and by_id[a].session_id != by_id[b].session_id
        ):
            slots.append(
                ContradictionSlot(
                    make_contradiction_id("native_id_collision", a, b),
                    "native_id_collision",
                    (a, b),
                )
            )
    return tuple(slots)


# --------------------------------------------------------- orchestration

def build_all_views(
    graph: EventGraph, builder_version: str = BUILDER_VERSION
) -> ViewBuildResult:
    """Build every derived view for one generation deterministically."""
    if graph.builder_version != builder_version:
        graph = EventGraph(
            generation_id=graph.generation_id,
            sessions=graph.sessions,
            events=graph.events,
            relations=graph.relations,
            builder_version=builder_version,
        )
    turn_views = build_turn_views(graph)
    native_trace_views = build_native_trace_views(graph)
    episode_views = build_episode_views(graph)
    compaction_views = build_compaction_window_views(graph)
    member_views = turn_views + native_trace_views + episode_views + compaction_views
    session_views = build_session_views(graph, member_views)
    topic_views = build_topic_views(graph)
    cross_session_views = build_cross_session_views(graph)
    views = (
        turn_views
        + native_trace_views
        + episode_views
        + compaction_views
        + session_views
        + topic_views
        + cross_session_views
    )
    result = ViewBuildResult(
        generation_id=graph.generation_id,
        builder_version=builder_version,
        views=views,
        digest="",
    )
    return ViewBuildResult(
        generation_id=graph.generation_id,
        builder_version=builder_version,
        views=views,
        digest=view_set_digest(result),
    )


__all__ = [
    "BUILDER_VERSION",
    "CompactionWindowView",
    "CrossSessionView",
    "ContradictionSlot",
    "DerivedView",
    "EpisodeView",
    "EventGraph",
    "NativeTraceView",
    "SessionView",
    "TopicView",
    "TurnView",
    "ViewBuildResult",
    "ViewBuilderError",
    "ViewType",
    "build_all_views",
    "build_compaction_window_views",
    "build_cross_session_views",
    "build_episode_views",
    "build_native_trace_views",
    "build_session_views",
    "build_topic_views",
    "build_turn_views",
    "make_contradiction_id",
    "make_view_id",
    "view_set_digest",
]
