"""Phase 62-04: deterministic event-to-legacy compatibility projection seam.

Phase 62 CONTEXT D-17/D-19: the legacy ``canonical_sessions`` /
``canonical_messages`` / ``canonical_tool_events`` tables become a
deterministic compatibility projection of exactly one active v2 event
generation. Existing consumers continue through ``ConversationRepository``
while new consumers use the event-aware repository seam.

This module owns ONLY event-to-legacy mapping:

  - :func:`build_compatibility_projection` reads the typed generation and
    computes the lossy session/message/tool rows plus a deterministic
    :class:`ProjectionFingerprint` (generation lineage).
  - :func:`upsert_compatibility_projection` is the only writer: it persists
    those rows inside the caller's transaction, inserting the missing ones and
    refreshing the changed ones with an ``UPDATE`` that is first reconciled
    against the row already stored (P1-3: the richer copy wins, so applying
    one slot at a time can no longer let a poorer re-capture overwrite a
    richer body written by an earlier round). It never deletes. Both the
    incremental path and activation/rollback go through it, because the store is
    a **collection**: a row the sources no longer produce stays exactly as
    collected, and inserting, updating and deleting must all leave every other
    row's ``rowid`` untouched (the rowid cursor in
    ``retrieval/conversation_fts.py`` depends on it).
  - :func:`clear_compatibility_projection` restores the pre-v2 state during a
    rollback/deactivation (rollback owner only).
  - :func:`compute_projection` is the pure mapping used by both.

It never activates a generation and never touches ``ce_generation_authority``
(activation belongs to :mod:`.event_generations`). Projected message rows come
only from message-kind events; reasoning, usage, compaction summaries,
boundaries, file-context and unknown-native events are reported as excluded
and never flattened into user facts (D-23). Each event maps to at most one row,
so there is no double counting.

No I/O outside the caller-provided DB path; no network, no provider calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from personal_knowledge.core.conversation_events import EventKind
from personal_knowledge.application.conversation.uniform_id_migration import (
    adapter_address,
    make_message_id,
    make_session_id,
    make_tool_id,
)

# Kinds that project to canonical_messages rows, with the legacy role mapping.
MESSAGE_KINDS: dict[EventKind, str] = {
    EventKind.USER_MESSAGE: "user",
    EventKind.ASSISTANT_MESSAGE: "assistant",
    EventKind.DEVELOPER_MESSAGE: "developer",
    EventKind.SYSTEM_MESSAGE: "system",
}

# Kinds that project to canonical_tool_events rows, with the legacy source_kind.
TOOL_KINDS: dict[EventKind, str] = {
    EventKind.TOOL_CALL: "call",
    EventKind.TOOL_RESULT: "result",
}

# Event kinds intentionally not flattened into a compatibility row.
EXCLUDED_KINDS: frozenset[EventKind] = frozenset(
    kind
    for kind in EventKind
    if kind not in MESSAGE_KINDS and kind not in TOOL_KINDS
)

# SQLite's default parameter ceiling is 999 (older builds) /
# 32766 (3.32+). Chunk well below both so a large delete can
# never fail on arity.
_PARAM_CHUNK = 400

# P1-3: the writer reconciles each incoming row against the row already
# stored (read-merge-write) instead of blindly overwriting it. Escape hatch
# for tests that need the pre-P1-3 "candidate always wins" behaviour:
# ``compatibility_projection.MERGE_WITH_STORED = False``. Default on.
MERGE_WITH_STORED = True


def _chunks(values: list, size: int = _PARAM_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]

PROJECTED_TABLES: tuple[str, ...] = (
    "canonical_sessions",
    "canonical_messages",
    "canonical_tool_events",
)

_SESSION_COLUMNS = (
    "canonical_session_id", "primary_source", "agent", "started_at", "ended_at",
    "message_count", "user_message_count", "file_hash", "parent_canonical_id",
    "relationship_type", "cwd", "git_branch", "model", "evidence_eligible",
    "evidence_scope", "merged", "lifecycle", "superseded_by_canonical_id",
)

_MESSAGE_COLUMNS = (
    "canonical_message_id", "canonical_session_id", "source",
    "source_message_ref", "ordinal", "role", "content", "content_length",
    "timestamp", "model", "is_system", "is_sidechain", "content_hash",
    "evidence_scope",
)

_TOOL_COLUMNS = (
    "canonical_tool_id", "canonical_session_id", "source", "source_kind",
    "tool_name", "category", "status", "call_index", "subagent_session_id",
    "content_length", "timestamp",
)


class CompatibilityProjectionError(RuntimeError):
    """A v2 generation cannot be projected deterministically."""


@dataclass(frozen=True)
class ProjectionFingerprint:
    """Deterministic generation lineage of a compatibility projection."""

    generation_id: str
    session_count: int
    message_count: int
    tool_count: int
    digest: str

    def to_dict(self) -> dict:
        return {
            "generation_id": self.generation_id,
            "session_count": self.session_count,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class CompatibilityProjectionReport:
    """The lossy compatibility rows computed for one generation."""

    generation_id: str
    sessions: tuple[dict, ...]
    messages: tuple[dict, ...]
    tools: tuple[dict, ...]
    excluded: tuple[dict, ...]
    fingerprint: ProjectionFingerprint
    collapsed_duplicate_ids: int = 0

    def to_dict(self) -> dict:
        return {
            "generation_id": self.generation_id,
            "sessions": list(self.sessions),
            "messages": list(self.messages),
            "tools": list(self.tools),
            "excluded": list(self.excluded),
            "fingerprint": self.fingerprint.to_dict(),
        }


def _norm_hash(prefix: str, *parts: object) -> str:
    payload = "|".join(str(p) for p in parts)
    return f"{prefix}|{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _content_hash(content: str | None) -> str | None:
    if not content:
        return None
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()[:32]


def compute_projection(
    generation_id: str,
    session_rows: list[dict],
    event_rows: list[dict],
) -> CompatibilityProjectionReport:
    """Pure deterministic event-to-legacy mapping (D-17).

    ``session_rows`` are ``ce_sessions`` rows; ``event_rows`` are ``ce_events``
    rows as returned by the event repository. Neither input is mutated.
    """
    if not generation_id:
        raise CompatibilityProjectionError("projection requires a generation id")

    sessions_by_id = {s["session_id"]: s for s in session_rows}
    family_by_session = {s["session_id"]: s.get("family", "") for s in session_rows}
    key_by_session = {sid: _session_key(srow)
                      for sid, srow in sessions_by_id.items()}
    messages, tools, excluded = _classify_events(
        generation_id, sessions_by_id, event_rows
    )
    projected_sessions = _merge_session_copies(_project_sessions(
        sessions_by_id, family_by_session, key_by_session, messages
    ))
    collapsed = [0]
    seen_messages: dict = {}
    seen_tools: dict = {}
    projected_messages = _project_messages(key_by_session, messages,
                                           collapsed, seen_messages)
    projected_tools = _project_tools(key_by_session, tools, collapsed,
                                     seen_tools)

    fingerprint = _make_fingerprint(
        generation_id, projected_sessions, projected_messages, projected_tools
    )
    return CompatibilityProjectionReport(
        generation_id=generation_id,
        sessions=tuple(projected_sessions),
        messages=tuple(projected_messages),
        tools=tuple(projected_tools),
        excluded=tuple(excluded),
        fingerprint=fingerprint,
        collapsed_duplicate_ids=collapsed[0],
    )


def _classify_events(
    generation_id: str,
    sessions_by_id: dict[str, dict],
    event_rows: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], list[dict]]:
    """Group events into message rows, tool rows and excluded events.

    Raises :class:`CompatibilityProjectionError` when an event references a
    session that is absent from the generation (fail closed).
    """
    messages: dict[str, list[dict]] = {sid: [] for sid in sessions_by_id}
    tools: dict[str, list[dict]] = {sid: [] for sid in sessions_by_id}
    excluded: list[dict] = []
    for event in sorted(
        event_rows, key=lambda e: (e.get("ordinal") or 0, e.get("event_id") or "")
    ):
        kind = EventKind(event["kind"])
        sid = event["session_id"]
        if sid not in sessions_by_id:
            raise CompatibilityProjectionError(
                f"event {event.get('event_id')} references a session ({sid}) "
                "that is absent from this generation"
            )
        if kind in MESSAGE_KINDS:
            messages[sid].append(event)
        elif kind in TOOL_KINDS:
            tools[sid].append(event)
        else:
            excluded.append({
                "event_id": event.get("event_id"),
                "kind": kind.value,
                "session_id": sid,
                "native_locator": event.get("native_locator"),
            })
    return messages, tools, excluded


def _richer(candidate: dict, prior: dict) -> bool:
    """True when ``candidate`` carries more content than the prior copy.

    Same address, different captured bytes (a file captured twice at different
    moments): keep the richer copy. Comparison key is
    ``(content is not None, len(content if content is not None else summary))``:

      - A copy that carries an explicit ``content`` — including ``""``, which is
        a legitimate tool-only/empty native message (see _project_messages) —
        always beats a copy that only has the bounded ``summary`` fallback.
        Comparing bare lengths instead would let summary prose overwrite a
        legitimately empty body.
      - Among copies of the same kind, the longer body wins.
      - Deterministic tiebreak on event_id so the choice never depends on row
        order.
    """

    def _key(event: dict) -> tuple[bool, int]:
        content = event.get("content")
        body = content if content is not None else event.get("summary") or ""
        return content is not None, len(body)

    cand_key = _key(candidate)
    prior_key = _key(prior["_event"])
    if cand_key != prior_key:
        return cand_key > prior_key
    return str(candidate.get("event_id") or "") < str(
        prior["_event"].get("event_id") or "")


def _session_key(srow: dict) -> tuple[str, str]:
    """(family, native session key) — the same rule uniform_id_migration uses."""
    family = (srow.get("family") or "unknown").strip().lower()
    native = (srow.get("native_session_id") or "").strip() or f"ce:{srow['session_id']}"
    return family, native


def _project_sessions(
    sessions_by_id: dict[str, dict],
    family_by_session: dict[str, str],
    key_by_session: dict[str, tuple[str, str]],
    messages: dict[str, list[dict]],
) -> list[dict]:
    """Map each generation session to one lossy canonical session row."""
    projected: list[dict] = []
    for sid, srow in sorted(sessions_by_id.items()):
        msgs = messages.get(sid, [])
        user_count = sum(
            1 for m in msgs if MESSAGE_KINDS[EventKind(m["kind"])] == "user"
        )
        family, _native = key_by_session[sid]
        projected.append({
            # Origin-derived id (uniform_id_migration): a re-capture of the
            # same native session must reproduce this id so it updates the
            # existing row instead of creating a second one.
            "canonical_session_id": make_session_id(*key_by_session[sid]),
            # Live canonical_sessions has CHECK(primary_source IN
            # ('agentsview','legacy')); 'v2' is not admissible, so projection
            # rows are tagged 'legacy' (the v2|cs| session-id prefix
            # keeps them distinguishable from legacy-era rows).
            "primary_source": "legacy",
            "agent": family_by_session.get(sid) or None,
            "started_at": srow.get("started_at"),
            "ended_at": srow.get("ended_at"),
            "message_count": len(msgs),
            "user_message_count": user_count,
            "file_hash": None,
            "parent_canonical_id": None,
            "relationship_type": None,
            "cwd": srow.get("cwd"),
            "git_branch": srow.get("git_branch"),
            "model": srow.get("model"),
            "evidence_eligible": 1,
            "evidence_scope": "user",
            "merged": 0,
            "lifecycle": "active",
            "superseded_by_canonical_id": None,
        })
    return projected


def _merge_session_pair(prior: dict, other: dict) -> dict:
    """Merge one session copy into ``prior`` (in place); returns ``prior``.

    The single merge rule shared by ``_merge_session_copies`` (copies of one
    native session inside one ``compute_projection`` call) and — for the
    monotonic fields — by ``_merge_stored_session_row`` (an incoming
    candidate vs the canonical_sessions row already stored):

      - ``started_at``: earliest non-None value (None-safe).
      - ``ended_at``: latest non-None value (None-safe).
      - ``message_count`` / ``user_message_count``: max across copies (each
        copy counts only its own events, so the fullest capture wins).
      - ``cwd`` / ``model``: smallest non-empty value, so a multi-valued
        merge is still reproducible (and order-independent).
      - Everything else: ``prior``'s value is kept (copies share
        family/native key; at the writer the stored row is the incumbent).
    """
    for field in ("started_at", "ended_at"):
        value, base = other.get(field), prior.get(field)
        if value is None:
            continue
        if base is None or (field == "started_at" and value < base) \
                or (field == "ended_at" and value > base):
            prior[field] = value
    for field in ("message_count", "user_message_count"):
        value = other.get(field)
        if value is not None and (prior.get(field) is None
                                  or value > prior[field]):
            prior[field] = value
    for field in ("cwd", "model"):
        value = other.get(field)
        if value and (prior.get(field) is None or value < prior[field]):
            prior[field] = value
    return prior


def _merge_session_copies(projected: list[dict]) -> list[dict]:
    """Collapse session rows that share a canonical_session_id into one row.

    One native session can be discovered as several ce sessions in one
    generation (the same file staged twice, a session plus its subagent
    artifact); ``_project_sessions`` then emits one row per ce session, all
    carrying the same origin-derived ``canonical_session_id``. The writer
    (``_upsert_rows``) keys rows by id, so the last duplicate would silently
    overwrite the others — making started_at/ended_at/counts/cwd/model depend
    on slot-hash ordering and flip on every re-capture. Merge instead, so the
    outgoing rows carry a unique ``canonical_session_id``; the field rules
    live in :func:`_merge_session_pair` (shared with the writer's
    stored-row reconciliation).

    This merge touches only canonical_sessions rows; the messages/tools
    per-id collapse in ``_project_messages`` / ``_project_tools`` (``_richer``)
    is unchanged.
    """
    merged: dict[str, dict] = {}
    for row in projected:
        prior = merged.get(row["canonical_session_id"])
        if prior is None:
            merged[row["canonical_session_id"]] = dict(row)
            continue
        _merge_session_pair(prior, row)
    return list(merged.values())


def _merge_stored_session_row(prior: tuple, candidate: tuple) -> tuple:
    """Reconcile a candidate canonical_sessions row against the stored row.

    Monotonic fields follow the same rules as :func:`_merge_session_copies`
    (via the shared :func:`_merge_session_pair`): started_at=min,
    ended_at=max, cwd/model smallest non-empty; everything else keeps the
    stored row's value.

    ``message_count`` / ``user_message_count`` deliberately do NOT take the
    max here: they are *derived* counters of the round's stale-filtered
    event set and must be able to decrease — a truncated source marks its
    lost events stale and the projection must stop counting them (see
    ``test_truncation_marks_lost_events_stale_and_projection_drops_them``).
    The candidate's counts therefore win as-is; copies of one native session
    inside a single round already merged to the max in
    :func:`_merge_session_copies`. Honest boundary: across rounds that touch
    different mirror slots of one native session, the counts follow the last
    applied slot — only the richer-bytes guarantee for messages/tools (and
    the monotonic started/ended window) is stable across applies.
    """
    merged = dict(zip(_SESSION_COLUMNS, prior))
    cand = dict(zip(_SESSION_COLUMNS, candidate))
    _merge_session_pair(merged, cand)
    for field in ("message_count", "user_message_count"):
        if cand.get(field) is not None:
            merged[field] = cand[field]
    return tuple(merged[column] for column in _SESSION_COLUMNS)


def _placeholder_native_ids(
    messages: dict[str, list[dict]],
    key_by_session: dict[str, tuple[str, str]],
) -> set[tuple[str, str]]:
    """Canonical sessions whose native message ids are placeholders, not keys.

    The per-family rule addresses native-id-first families by their native
    message id, but that id is trusted only as long as it identifies one
    message *within its artifact*: a reliable client uuid never repeats inside
    one artifact, so an id that occurs several times in the same ce session
    (the way codex records the literal ``agent_message`` for 446 message
    events of one session) is a constant/placeholder, and id-first would
    collapse genuinely different messages onto one row. Such a session falls
    back to the locator-first rule.

    Repeats *across* artifacts are not a suspicion: one native message
    collected through several mirror paths carries the same uuid in each copy
    — collapsing those copies onto one id is exactly what the rule is for,
    with the ``seen``/``_richer`` mechanism keeping the richer bytes when the
    captures disagree.
    """
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for sid, events in messages.items():
        key = key_by_session[sid]
        for event in events:
            nid = (event.get("native_event_id") or "").strip()
            if nid:
                counts[(key, sid)][nid] += 1
    return {key for (key, _sid), ids in counts.items()
            if any(n > 1 for n in ids.values())}


def _project_messages(
    key_by_session: dict[str, tuple[str, str]],
    messages: dict[str, list[dict]],
    collapsed: list[int],
    seen: dict,
) -> list[dict]:
    """Map message-kind events to canonical_messages rows (documented lossy)."""
    projected: list[dict] = []
    placeholders = _placeholder_native_ids(messages, key_by_session)
    for sid, events in sorted(messages.items()):
        family, native = key_by_session[sid]
        # A session whose native ids proved to be placeholders keeps the
        # locator-first rule: pass a family the rule does not recognize.
        address_family = "" if (family, native) in placeholders else family
        for ordinal, event in enumerate(sorted(
            events, key=lambda e: (e.get("ordinal") or 0, e.get("event_id") or "")
        ), start=1):
            # ``content`` is the exact mapped source body.  ``None`` means an
            # older adapter did not emit the optional field, so the bounded
            # summary remains a backward-compatible fallback.  An explicit
            # empty string is a legitimate tool-only/empty native message and
            # must not be replaced with summary prose.
            content = event.get("content")
            if content is None:
                content = event.get("summary") or None
            role = MESSAGE_KINDS[EventKind(event["kind"])]
            new_id = make_message_id(
                family, native,
                adapter_address(event.get("native_event_id"),
                                event.get("native_locator"),
                                address_family)
                or event["event_id"])
            # One native session can be discovered as several ce sessions in
            # one generation (the same file staged twice, a session plus its
            # subagent artifact). Their rows then share an id, and a blind
            # INSERT OR REPLACE would silently drop whichever arrived last —
            # including, when the copies captured different bytes, the longer
            # one. Keep the richest copy per id.
            prior = seen.get(new_id)
            if prior is not None:
                collapsed[0] += 1
                if _richer(event, prior):
                    projected[prior["_index"]] = None  # dropped below
                else:
                    continue
            row_index = len(projected)
            projected.append({
                "_index": row_index,
                "canonical_message_id": new_id,
                "canonical_session_id": make_session_id(family, native),
                # Live CHECK(source IN ('agentsview','legacy')); 'v2' is not
                # admissible (see _project_sessions).
                "source": "legacy",
                "source_message_ref": event.get("native_locator"),
                "ordinal": ordinal,
                "role": role,
                "content": content,
                "content_length": len(content or ""),
                "timestamp": event.get("occurred_at"),
                "model": None,
                "is_system": 1 if role == "system" else 0,
                "is_sidechain": 0,
                "content_hash": _content_hash(content),
                "evidence_scope": "user",
            })
            seen[new_id] = {"_index": row_index, "_event": event}
    return [{k: v for k, v in r.items() if k != "_index"}
            for r in projected if r is not None]


def _project_tools(
    key_by_session: dict[str, tuple[str, str]],
    tools: dict[str, list[dict]],
    collapsed: list[int],
    seen: dict,
) -> list[dict]:
    """Map tool-kind events to canonical_tool_events rows (documented lossy)."""
    projected: list[dict] = []
    for sid, events in sorted(tools.items()):
        family, native = key_by_session[sid]
        for event in sorted(
            events, key=lambda e: (e.get("ordinal") or 0, e.get("event_id") or "")
        ):
            source_kind = TOOL_KINDS[EventKind(event["kind"])]
            summary = event.get("summary") or None
            new_id = make_tool_id(
                family, native,
                adapter_address(event.get("native_event_id"),
                                event.get("native_locator"))
                or event["event_id"])
            prior = seen.get(new_id)
            if prior is not None:
                collapsed[0] += 1
                if _richer(event, prior):
                    projected[prior["_index"]] = None
                else:
                    continue
            row_index = len(projected)
            projected.append({
                "_index": row_index,
                "canonical_tool_id": new_id,
                "canonical_session_id": make_session_id(family, native),
                # Live CHECK(source IN ('agentsview','legacy')); 'v2' is not
                # admissible (see _project_sessions).
                "source": "legacy",
                "source_kind": source_kind,
                "tool_name": summary if source_kind == "call" else None,
                "category": None,
                "status": "ok",
                "call_index": event.get("ordinal"),
                "subagent_session_id": None,
                "content_length": len(summary or ""),
                "timestamp": event.get("occurred_at"),
                "source_ref": event.get("native_locator"),
            })
            seen[new_id] = {"_index": row_index, "_event": event}
    return [{k: v for k, v in r.items() if k != "_index"}
            for r in projected if r is not None]


def _make_fingerprint(
    generation_id: str,
    sessions: list[dict],
    messages: list[dict],
    tools: list[dict],
) -> ProjectionFingerprint:
    """Deterministic digest over the exact projected rows (generation lineage)."""
    payload = {
        "generation_id": generation_id,
        "sessions": sorted(
            sessions, key=lambda r: r["canonical_session_id"]
        ),
        "messages": sorted(
            messages, key=lambda r: r["canonical_message_id"]
        ),
        "tools": sorted(tools, key=lambda r: r["canonical_tool_id"]),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return ProjectionFingerprint(
        generation_id=generation_id,
        session_count=len(sessions),
        message_count=len(messages),
        tool_count=len(tools),
        digest=digest,
    )


def _read_generation(db: Path, generation_id: str) -> tuple[list[dict], list[dict]]:
    if not db.exists():
        raise CompatibilityProjectionError(
            f"event database missing: {db}"
        )
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sessions = [
            dict(r) for r in con.execute(
                "SELECT session_id, family, native_session_id, started_at, "
                "ended_at, native_locator, contract_version, fidelity_json, "
                "cwd, git_branch, model, title, stop_reason "
                "FROM ce_sessions WHERE generation_id=? ORDER BY session_id",
                (generation_id,),
            )
        ]
        events = [
            dict(r) for r in con.execute(
                "SELECT event_id, session_id, kind, artifact_id, native_locator, "
                "native_event_id, occurred_at, ordinal, native_payload_ref, "
                "content, summary, contract_version, fidelity_json "
                "FROM ce_events WHERE generation_id=? "
                "ORDER BY ordinal, event_id",
                (generation_id,),
            )
        ]
    finally:
        con.close()
    return sessions, events


def build_compatibility_projection(
    db: Path, generation_id: str
) -> CompatibilityProjectionReport:
    """Compute the deterministic compatibility projection of one generation.

    Read-only: never writes the compatibility tables or the authority pointer.
    """
    sessions, events = _read_generation(db, generation_id)
    return compute_projection(generation_id, sessions, events)


# Column indexes used by the stored-row reconciliation below.
_MESSAGE_CONTENT_IDX = _MESSAGE_COLUMNS.index("content")
_TOOL_CONTENT_LENGTH_IDX = _TOOL_COLUMNS.index("content_length")
_TOOL_NAME_IDX = _TOOL_COLUMNS.index("tool_name")


def _merge_stored_row(
    table: str, prior: tuple, candidate: tuple
) -> tuple:
    """Reconcile an incoming candidate row against the row already stored.

    Dispatches on the table; unknown tables keep the pre-P1-3 behaviour
    (candidate wins wholesale).
    """
    if table == "canonical_sessions":
        return _merge_stored_session_row(prior, candidate)
    if table == "canonical_messages":
        return _merge_stored_message_row(prior, candidate)
    if table == "canonical_tool_events":
        return _merge_stored_tool_row(prior, candidate)
    return candidate


def _merge_stored_message_row(prior: tuple, candidate: tuple) -> tuple:
    """Reconcile a candidate canonical_messages row against the stored row.

    Row-level analog of ``_richer``: the projected row's ``content`` column
    already carries the resolved body (content or the summary fallback), so
    the comparison key ``(content is not None, len(content or ""))``
    reproduces the event-level ordering exactly — an explicit body
    (including ``""``) beats a summary-only copy, and among bodies of the
    same kind the longer one wins.

      - candidate richer → candidate.
      - stored richer → stored (the merged tuple equals the stored tuple, so
        no UPDATE is issued and the earlier round's longer body survives).
      - same content → candidate (equivalent; if nothing else differs the
        generic equality check still skips the write).
      - same richness but different bytes (same-length divergent captures —
        the row level has no event_id for ``_richer``'s tiebreak):
        lexicographically smaller content wins, which is order-independent
        and therefore cannot flap across apply rounds.
    """
    stored_content = prior[_MESSAGE_CONTENT_IDX]
    cand_content = candidate[_MESSAGE_CONTENT_IDX]
    stored_key = (stored_content is not None, len(stored_content or ""))
    cand_key = (cand_content is not None, len(cand_content or ""))
    if cand_key > stored_key:
        return candidate
    if cand_key < stored_key:
        return prior
    if stored_content == cand_content:
        return candidate
    return candidate if (cand_content or "") < (stored_content or "") else prior


def _merge_stored_tool_row(prior: tuple, candidate: tuple) -> tuple:
    """Reconcile a candidate canonical_tool_events row against the stored row.

    canonical_tool_events carries no content column — the captured body is
    the tool summary, stored as ``content_length`` (and ``tool_name`` for
    calls), so the ``_richer`` key degenerates to the summary length. A
    stored row with a NULL length (legacy row) counts as empty so any
    candidate carrying a summary wins.
    """
    stored_len = prior[_TOOL_CONTENT_LENGTH_IDX]
    cand_len = candidate[_TOOL_CONTENT_LENGTH_IDX]
    if (cand_len or 0) > (stored_len if stored_len is not None else -1):
        return candidate
    if (cand_len if cand_len is not None else -1) < \
            (stored_len if stored_len is not None else -1):
        return prior
    stored_name, cand_name = prior[_TOOL_NAME_IDX], candidate[_TOOL_NAME_IDX]
    if stored_name == cand_name:
        return candidate
    return candidate if (cand_name or "") < (stored_name or "") else prior


def _upsert_rows(
    con: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    rows: tuple[dict, ...],
    merge_with_stored: bool = True,
) -> tuple[int, int]:
    """Insert the missing rows and refresh the changed ones in place.

    ``columns[0]`` is the table's primary key. Before writing, the rows
    already stored under the incoming ids are SELECTed (batched ``IN``
    queries, chunked well below SQLite's variable ceiling via ``_chunks``)
    and each incoming row is reconciled against its stored twin:

      - absent id → INSERT (unchanged).
      - present id → :func:`_merge_stored_row` decides what is written.
        ``canonical_messages`` / ``canonical_tool_events`` keep the richer
        copy (same rule as ``_richer``); ``canonical_sessions`` merge
        started_at/ended_at/counts/cwd/model with the same rules as
        ``_merge_session_copies``. A candidate that is poorer than the
        stored row therefore writes nothing, so applying one slot per
        live-sync round can no longer bounce a row's body back and forth
        with the collection order (P1-3).
      - a merged row identical to the stored row is skipped entirely, so an
        idempotent replay of unchanged content still writes nothing at all.

    Honest boundary: this is an optimistic read-merge-write inside the
    caller's single transaction — it converges every row to the richest
    value seen so far, but it does NOT keep cross-round version history (a
    poorer candidate is dropped, not archived; a history table is future
    work). Returns ``(inserted, updated)``.
    """

    if not rows:
        return 0, 0
    id_column = columns[0]
    incoming = {
        str(row[id_column]): tuple(row.get(column) for column in columns)
        for row in rows
    }
    stored: dict[str, tuple] = {}
    for chunk in _chunks(sorted(incoming)):
        marks = ",".join("?" * len(chunk))
        for row in con.execute(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE {id_column} IN ({marks})",
            chunk,
        ):
            stored[str(row[0])] = tuple(row)

    to_insert = []
    to_update = []
    for row_id, row in incoming.items():
        prior = stored.get(row_id)
        if prior is None:
            to_insert.append(row)
            continue
        merged = (
            _merge_stored_row(table, prior, row)
            if merge_with_stored else row
        )
        if merged != prior:
            to_update.append(merged)
    if to_insert:
        con.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            to_insert,
        )
    if to_update:
        assignments = ", ".join(f"{c}=?" for c in columns[1:])
        con.executemany(
            f"UPDATE {table} SET {assignments} WHERE {id_column}=?",
            [(*row[1:], row[0]) for row in to_update],
        )
    return len(to_insert), len(to_update)


def upsert_compatibility_projection(
    con: sqlite3.Connection, report: CompatibilityProjectionReport
) -> dict[str, dict[str, int]]:
    """Persist the projected rows without ever deleting one (collection writer).

    This is the **only** projection writer: the incremental live path and the
    activation/rollback path both go through it. It inserts rows that are absent
    and refreshes rows whose reconciled values changed with an ``UPDATE``
    (``_upsert_rows``):

      - Reconciliation (P1-3, on by default via ``MERGE_WITH_STORED``): an
        incoming row whose id already exists is merged with the stored row —
        messages/tools keep the richer copy (same rule as ``_richer``),
        sessions merge started_at/ended_at/counts/cwd/model. Live sync applies
        only the slots a round touched, so without this a poorer re-capture of
        slot B would overwrite the richer body an earlier round stored from
        slot A, and the row would flap with the collection order.
      - ``canonical_sessions`` / ``canonical_messages`` /
        ``canonical_tool_events`` are read with a **monotonic rowid cursor** by
        ``retrieval/conversation_fts.py``, so rewriting a row must not move its
        rowid. ``INSERT OR REPLACE`` and delete-then-insert both do move it (and
        can recycle a low rowid behind a reader's watermark); an ``UPDATE``
        cannot.
      - A row this projection no longer produces (the source disappeared, the
        native id changed) is left exactly as collected. The store is a
        collection, not a mirror of whatever the sources currently contain.

    Projected ids are recorded in ``ce_projected_ids`` with ``INSERT OR IGNORE``
    so the activation/rollback owner still knows which rows the projection wrote;
    that table is never cleared here.
    """

    _ensure_tables(con)
    counts: dict[str, dict[str, int]] = {}
    for table, columns, rows in (
        ("canonical_sessions", _SESSION_COLUMNS, report.sessions),
        ("canonical_messages", _MESSAGE_COLUMNS, report.messages),
        ("canonical_tool_events", _TOOL_COLUMNS, report.tools),
    ):
        inserted, updated = _upsert_rows(
            con, table=table, columns=columns, rows=rows,
            # Read at call time so tests can flip the escape hatch by
            # monkeypatching the module constant.
            merge_with_stored=MERGE_WITH_STORED,
        )
        counts[table] = {"inserted": inserted, "updated": updated}
        if rows:
            con.executemany(
                "INSERT OR IGNORE INTO ce_projected_ids VALUES (?,?)",
                [(table, str(row[columns[0]])) for row in rows],
            )
    return counts


def clear_compatibility_projection(con: sqlite3.Connection) -> None:
    """Delete every row the projection ever wrote (rollback owner only).

    Deletes ONLY rows recorded in ``ce_projected_ids``, so rows the projection
    never wrote — migrated rows, snapshot rows, anything else in the store — are
    preserved. Activation must never discard the product's existing canonical
    conversation data (D-18/D-19). Never deletes the tables themselves (D-19).

    ``ce_projected_ids`` is an append-only ownership ledger: every writer records
    the ids it wrote with ``INSERT OR IGNORE`` and never clears the ledger, so a
    row that was written once and is now retained as part of the collection is
    still reported as owned here. That is the correct set for deactivation, which
    removes the whole projection rather than one round of it.
    """
    _ensure_tables(con)
    owned = con.execute(
        "SELECT table_name, row_id FROM ce_projected_ids").fetchall()
    by_table: dict[str, list[str]] = defaultdict(list)
    for table_name, row_id in owned:
        by_table[table_name].append(row_id)
    id_column = {
        "canonical_sessions": "canonical_session_id",
        "canonical_messages": "canonical_message_id",
        "canonical_tool_events": "canonical_tool_id",
    }
    for table_name, ids in by_table.items():
        column = id_column[table_name]
        for start in range(0, len(ids), _PARAM_CHUNK):
            chunk = ids[start:start + _PARAM_CHUNK]
            marks = ",".join("?" * len(chunk))
            con.execute(f"DELETE FROM {table_name} WHERE {column} IN ({marks})",
                        chunk)
    con.execute("DELETE FROM ce_projected_ids")


def _ensure_tables(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_sessions (
            canonical_session_id TEXT PRIMARY KEY, primary_source TEXT NOT NULL,
            agent TEXT, started_at TEXT, ended_at TEXT, message_count INTEGER,
            user_message_count INTEGER, file_hash TEXT, parent_canonical_id TEXT,
            relationship_type TEXT, cwd TEXT, git_branch TEXT, model TEXT,
            evidence_eligible INTEGER NOT NULL DEFAULT 1,
            evidence_scope TEXT NOT NULL DEFAULT 'user',
            merged INTEGER NOT NULL DEFAULT 0,
            lifecycle TEXT NOT NULL DEFAULT 'active',
            superseded_by_canonical_id TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_messages (
            canonical_message_id TEXT PRIMARY KEY,
            canonical_session_id TEXT NOT NULL, source TEXT NOT NULL,
            source_message_ref TEXT, ordinal INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT, content_length INTEGER, timestamp TEXT, model TEXT,
            is_system INTEGER NOT NULL DEFAULT 0,
            is_sidechain INTEGER NOT NULL DEFAULT 0, content_hash TEXT,
            evidence_scope TEXT NOT NULL DEFAULT 'user')"""
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS ce_projected_ids ("
        " table_name TEXT NOT NULL, row_id TEXT NOT NULL,"
        " PRIMARY KEY (table_name, row_id))")
    con.execute(
        """CREATE TABLE IF NOT EXISTS canonical_tool_events (
            canonical_tool_id TEXT PRIMARY KEY,
            canonical_session_id TEXT NOT NULL, source TEXT NOT NULL,
            source_kind TEXT NOT NULL, tool_name TEXT, category TEXT, status TEXT,
            call_index INTEGER, subagent_session_id TEXT, content_length INTEGER,
            timestamp TEXT)"""
    )
    # Session-scope lookups/deletes (readers select one session at a time; the
    # rollback owner deletes by ``canonical_session_id IN (...)``) would
    # otherwise be one full-table SCAN per session — measured 44.8 ms
    # (messages) + 36.1 ms (tool events) per session on the ~8 GB staging db.
    # Declared here, in the module that owns these tables, and idempotent, so an
    # existing database picks the indexes up on its next projection build.
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_canonical_messages_session "
        "ON canonical_messages(canonical_session_id)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_canonical_tool_events_session "
        "ON canonical_tool_events(canonical_session_id)"
    )


__all__ = [
    "CompatibilityProjectionError",
    "CompatibilityProjectionReport",
    "PROJECTED_TABLES",
    "ProjectionFingerprint",
    "build_compatibility_projection",
    "clear_compatibility_projection",
    "compute_projection",
    "upsert_compatibility_projection",
]
