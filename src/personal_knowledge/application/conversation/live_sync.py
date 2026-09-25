"""Milestone 2: one-generation incremental conversation sync (live slots).

Why this module exists
----------------------
``pk-sync conversations --v2-native`` stages a **new immutable generation** per
run: the cohort id is derived from the whole staged set, so one changed byte
produces a new generation id, every collected file is re-parsed, and every event
row is rewritten (measured 2026-09-14: 872,069 events / ~1.9 GiB per run, of
which ~11 % was genuinely new). Nothing ever prunes old generations.

Milestone 1 removed the root cause of that write amplification: ``artifact_id``
is now a **stable per-source-slot identity**
(``sha256("art|<family>|<mirror_path>")``, ``snapshots.make_slot_artifact_id``),
constant across content edits, while ``content_hash`` keeps holding
``sha256(bytes)`` and addresses the content-addressed blob store. Session and
event ids are derived from ``(family, artifact_id, contract_version, native id)``,
so a file edit no longer rotates the ids of the rows it previously produced.

This module is the engine that exploits it. ``live_sync_once`` keeps **one** live
generation and grows it **per source slot**::

    changed file -> re-capture + adapt that file alone -> refresh the rows that
    file now emits -> INSERT the rows it newly contributes

The store is a **collection**, not a mirror: what has been collected stays
collected.

Correctness notes
-----------------
* **One transaction.** The whole apply runs under a single ``BEGIN IMMEDIATE``;
  a crash rolls back completely, so there is no recovery logic and no partial
  slot. The DB is never left half-migrated.
* **Append-only.** No row of ``ce_sessions`` / ``ce_events`` /
  ``ce_event_relations`` / ``ce_field_dispositions`` is ever deleted, for any
  reason, and no row is ever rewritten with its previous value lost. A same id
  can still carry different values (an edited message body keeps its native id
  and locator; a session row's ``ended_at`` moves when a turn is appended; a
  relation's kind is a derivation of the same two endpoints; a disposition's
  verdict for one native field of one event can be re-decided), so the three
  buckets are:
  - **stale ids** (the id is stored but the source no longer emits it: a
    truncated/edited file, a vanished source file, a deactivated slot) — the
    collected row is kept exactly as it was captured and additionally marked
    with ``stale_at = now`` (P0-1: NULL = current, non-NULL = left the current
    computation); a later apply that sees the source emit the id again clears
    the marker back to NULL (dirty or unchanged, either way the row is current
    again);
  - **dirty ids** (the id is stored and its row values changed) — the row's
    *current* values are appended to ``ce_event_versions`` /
    ``ce_session_versions`` / ``ce_relation_versions`` /
    ``ce_disposition_versions`` (``version_seq`` from 0 per id,
    ``superseded_at`` = now) and the main row is then refreshed **in place**
    with an ``UPDATE``. ``INSERT OR IGNORE`` alone could not do that: it is a
    no-op for a present id, which is how stale *content* used to survive
    unnoticed;
  - **new ids** — ``INSERT OR IGNORE``.
  The only writes this engine performs are INSERT, UPDATE and state markers.
* **Cross-slot relation duplicates are counted, not silent (P1-5).** A
  relation id derived from native identity can reach the store twice — once
  per slot that collected the same native session — with different endpoint
  event ids. The second apply's ``INSERT OR IGNORE`` cannot land it; the
  swallowed row is counted into the apply report
  (``relations_ignored_duplicates``, per family too) and its endpoints are
  moved to the incoming copy only when that copy is fully current and the
  stored anchor is fully stale (``relations_endpoint_refreshed``; the replaced
  value is archived first). Otherwise the stored row — and therefore the copy
  the edge follows — is exactly what arrived first.
* **No FK cascade to worry about.** Because nothing is deleted, the deletes once
  ordered ``ce_event_relations -> ce_field_dispositions -> ce_events ->
  ce_sessions`` no longer exist; ``ce_source_artifacts`` rows are likewise kept
  (a vanished source slot is only marked ``active = 0``), so the provenance and
  every collected row of a removed source survive reconciliation.
* **Projection.** The compatibility rows are recomputed only for the sessions the
  apply touched, and are written with ``upsert_compatibility_projection``
  (insert missing, ``UPDATE`` changed — never delete). That is exact, not
  approximate: every projected row is a pure function of one session and that
  session's own events (``canonical_session_id = f(session_id)``,
  ``canonical_message_id = f(event_id)``). The projection reads filter
  ``stale_at IS NULL`` (P0-1), so a stale event no longer counts into
  ``message_count`` or reaches the canonical rows as current data. The whole
  generation is never re-projected, the rows of a deactivated slot keep the
  exact projection they were collected with, and no ``canonical_*`` rowid moves
  or gets recycled — which is the contract the rowid cursor in
  ``retrieval/conversation_fts.py`` (:28-33) depends on.
* **Fast path.** A ``(mtime_ns, size)`` fingerprint per collected path is kept in
  ``ce_live_state['mirror_fingerprints']``, so an unchanged file is neither
  hashed nor parsed.
* **Schema.** All DDL (including ``ce_live_slots`` / ``ce_live_state`` /
  ``ce_live_sync_log``, the four ``*_versions`` history tables and the three
  supporting indexes) is owned by ``event_schema``; this module declares no DDL
  of its own.

Known limits (deliberate, documented rather than hidden)
--------------------------------------------------------
* ``ce_source_artifacts`` is **global**, not per generation (its columns carry no
  ``generation_id``). A slot therefore has exactly one row whose
  ``content_hash`` is refreshed in place on every edit; the previous bytes stay
  reachable only through the content-addressed blob store. While two
  generations coexist in one database, a reader that resolves an *older*
  generation's artifact to bytes will find the newer content hash. Consequence
  and handling are spelled out at ``_refresh_artifact_row``.
* Rollback is the enclosing transaction, not a generation pointer: there is
  exactly one live generation, grown in place.
* Every row family now carries history: events, sessions, relations and
  dispositions each archive the value they are about to replace in their
  ``*_versions`` table (``ce_event_relations`` is keyed by ``relation_id`` and
  ``ce_field_dispositions`` by ``(event_id, field_name)``, so both are archived
  under exactly that key). A relation whose id survives but whose *kind*
  changed has not been observed from a shipped adapter yet — relation ids are
  derived per code path from the endpoints (``rel-dag:<child>:<parent>`` &c.),
  and kind is derived from the same link — but the store no longer depends on
  that: the same identity holding a different value is archived, not ignored.
  One caveat to know about a *changed* relation: ``ce_event_relations`` keeps a
  ``UNIQUE (generation_id, source_event_id, target_event_id, relation_kind)``,
  so a kind flip that lands on a value another relation row already holds for
  the same endpoints raises ``IntegrityError`` and rolls the whole apply back
  (fail-closed, nothing lost) instead of silently keeping the old value.
* Deactivating a slot records ``{slot_id, family, mirror_path, removed_at}`` in
  ``ce_live_state['removed_slots']`` (append-only) and in the run's
  ``ce_live_sync_log.detail``, so "which source disappeared and when" stays
  auditable without removing its rows.
* **Stale projection residue is a P4 boundary, not hidden debt.** A stale
  ``ce_events`` row that was already projected keeps its ``canonical_messages``
  row (the projection upsert never deletes), and a deactivated slot keeps the
  canonical rows it was collected with. This milestone buys: the *current*
  computation (projection reads, ``message_count``) is no longer fed stale
  events, and staleness itself is queryable (``stale_at``). Reclaiming the
  already-projected canonical rows is P4.

The CLI (``pk-sync conversations --live-sync``) and the watch loop are later
milestones; this module is the engine only. No live canonical store is ever
touched by the tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.discovery import (
    CAPTURE_TEMP_PREFIXES,
    SQLITE_ALLOWLISTS,
    mirror_path_for,
)
from personal_knowledge.adapters.conversation_sources.registry import (
    adapt_for,
    capability_for,
    resolve_family,
)
from personal_knowledge.adapters.conversation_sources.snapshots import (
    capture_file,
    capture_sqlite,
)
from personal_knowledge.application.conversation import event_schema
from personal_knowledge.application.conversation.compatibility_projection import (
    compute_projection,
    upsert_compatibility_projection,
)
from personal_knowledge.application.conversation.event_repository import (
    GenerationInput,
    _enrich_session_titles,
    _fidelity_json,
    _insert_artifacts,
    _insert_dispositions,
    _insert_events,
    _insert_relations,
    _insert_sessions,
)

# ``ce_live_state`` keys owned by this engine.
FINGERPRINT_STATE_KEY = "mirror_fingerprints"
LAST_SYNC_STATE_KEY = "last_sync"
REMOVED_SLOTS_STATE_KEY = "removed_slots"

# SQLite's default parameter ceiling is 999 (older builds) / 32766 (3.32+).
# Chunk well below both so a large id set can never fail on arity.
_PARAM_CHUNK = 400

_SQLITE_MAGIC = b"SQLite format 3\x00"

# Directory names that are never a source family: the content-addressed blob
# store (``snapshots``) and the SQLite capture staging dir.
_SKIP_DIR_NAMES = frozenset({"artifacts", ".staging"})

# Capture limits, mirroring the CLI defaults for the v2 native path.
_BYTE_LIMIT = 600_000_000
_COUNT_LIMIT = 2_000

# P2 guard: an apply that sees more than this share of the active slots vanish
# from the mirror in one pass is almost always looking at a staging accident (a
# wiped or re-pointed mirror root, a failed stage run) rather than that many
# real deletions at once — and deactivating every slot would mark the whole
# live store stale in a single transaction. The apply raises instead. The
# threshold is a fraction of the *currently* active slots, so a genuinely
# intended mass removal can still be expressed by applying successive batches,
# each below half of the slots still active at that point.
REMOVED_SLOT_GUARD_RATIO = 0.5


class LiveSyncError(RuntimeError):
    """Fail-closed live-sync contract violation."""


# ------------------------------------------------------------------ helpers


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _chunks(values: list, size: int = _PARAM_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def _file_digest(path: Path) -> str:
    """``sha256(bytes)`` — the same value ``capture_*`` stores as content_hash."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO ce_live_state(key, value) VALUES (?,?)",
        (key, value),
    )


def _get_state(con: sqlite3.Connection, key: str) -> str | None:
    try:
        row = con.execute(
            "SELECT value FROM ce_live_state WHERE key=?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # schema not applied yet (read-only inspection)
    return None if row is None else row[0]


# ------------------------------------------------------------ source scan


def scan_mirror(mirror_root: Path) -> dict[str, tuple[str, Path]]:
    """Enumerate the collected sources as ``{mirror_path: (family, path)}``.

    ``mirror_path`` is the path relative to ``mirror_root`` (``"<family>/<rel>"``,
    via ``discovery.mirror_path_for``) — the same string the full-rebuild path
    feeds to ``capture_*`` as ``mirror_path=``, which is what makes the slot id
    identical between an incremental apply and a fresh rebuild. The name is
    historical (the source tree is a copy of the originals); this engine *adds*
    what it finds to the collected store and never removes anything.

    ``.hashes.json`` bookkeeping, capture intermediates (``.snap-`` /
    ``.filtered-`` / ``.tmp-``) and the blob/staging dirs are never sources:
    adapting a capture intermediate would emit a second copy of the very events
    it was derived from (and collide on ids).
    """

    found: dict[str, tuple[str, Path]] = {}
    root = Path(mirror_root)
    if not root.is_dir():
        return found
    for family_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if family_dir.name.startswith(".") or family_dir.name in _SKIP_DIR_NAMES:
            continue
        try:
            owner = resolve_family(family_dir.name)
        except KeyError:
            continue  # not a registered family: never guessed as a source
        for dirpath, dirnames, filenames in os.walk(family_dir):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".") and d not in _SKIP_DIR_NAMES
            )
            for name in sorted(filenames):
                if name.startswith(".") or name.startswith(CAPTURE_TEMP_PREFIXES):
                    continue
                path = Path(dirpath) / name
                if not path.is_file():
                    continue
                mirror_path = mirror_path_for(owner, path, source_root=root)
                found[mirror_path] = (owner, path)
    return found


def _fingerprint(path: Path) -> list:
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_size]


def _classify_removed(
    slots: dict[str, sqlite3.Row], scanned: dict[str, tuple[str, Path]]
) -> tuple[list[tuple], set[str]]:
    """Active slots whose mirror path vanished, plus unregistered-family strays.

    P2: a slot whose family is no longer resolvable (deregistered or renamed in
    the registry) never appears in ``scanned`` — ``scan_mirror`` refuses to
    guess a family — so its absence from a scan is *not* evidence that the
    source disappeared. Such slots are reported under the returned
    ``unregistered_families`` set and are NOT deactivated; when the family is
    registered again they line up with their files unchanged. A resolvable
    family whose mirror path is gone is a real removal.
    """

    removed: list[tuple] = []
    unregistered: set[str] = set()
    for mirror_path, slot in slots.items():
        if not slot["active"] or mirror_path in scanned:
            continue
        try:
            resolve_family(str(slot["family"]))
        except KeyError:
            unregistered.add(str(slot["family"]))
            continue
        removed.append((slot["slot_id"], slot["family"], mirror_path))
    return removed, unregistered


def _assert_removal_guard(
    removed: list[tuple], slots: dict[str, sqlite3.Row]
) -> None:
    """P2 guard: refuse to deactivate a suspicious share of the live slots.

    See ``REMOVED_SLOT_GUARD_RATIO``. The raised ``LiveSyncError`` is the
    operator's signal: nothing was written, the mirror must be inspected before
    the next apply.
    """

    if not removed:
        return
    n_active = sum(1 for slot in slots.values() if slot["active"])
    if n_active and len(removed) > n_active * REMOVED_SLOT_GUARD_RATIO:
        raise LiveSyncError(
            f"live-sync refused: {len(removed)} of {n_active} active slots "
            f"disappeared from the mirror in one pass "
            f"(> {REMOVED_SLOT_GUARD_RATIO:.0%} guard). A wiped or re-pointed "
            f"mirror root is the usual cause; nothing was deactivated. A "
            f"deliberate mass removal must be applied in successive batches, "
            f"each below the guard against the slots still active."
        )


# ---------------------------------------------------------------- capture


def _capture_and_adapt(
    path: Path,
    *,
    family: str,
    mirror_path: str,
    artifact_store: Path,
    byte_limit: int,
    count_limit: int,
):
    """Capture one collected source file (WAL-safe for SQLite) and adapt it.

    Mirrors ``v2_sync._adapt_source_file``: the same ``relative_path=path.name``
    and the same ``family`` / ``mirror_path`` arguments, so the emitted
    ``artifact_id`` (slot id) and every derived native locator are byte-for-byte
    the same as a full rebuild of the same corpus.
    """

    try:
        with path.open("rb") as handle:
            head = handle.read(len(_SQLITE_MAGIC))
    except OSError:
        head = b""

    allowlist = SQLITE_ALLOWLISTS.get(family)
    if head.startswith(_SQLITE_MAGIC) and allowlist is not None:
        tables, columns = allowlist
        artifact, blob = capture_sqlite(
            path,
            artifact_store,
            allowed_tables=tables,
            allowed_columns=columns,
            byte_limit=byte_limit,
            count_limit=count_limit,
            family=family,
            mirror_path=mirror_path,
        )
    else:
        artifact, blob = capture_file(
            path,
            artifact_store,
            relative_path=path.name,
            byte_limit=byte_limit,
            count_limit=count_limit,
            family=family,
            mirror_path=mirror_path,
        )
    result = adapt_for(
        family, SourceArtifactSet((artifact,)), artifact_root=blob.parent
    )
    return artifact, result


def _generation_input(result) -> GenerationInput:
    cap = capability_for(result.family)
    gen = GenerationInput(
        family=result.family,
        adapter_version=result.adapter_version,
        contract_version=result.contract_version,
        capability_digest=cap.digest(),
        source_manifest_id=f"live-{cap.digest()[:16]}",
        dataset_digest=result.dataset_digest,
        artifacts=result.artifacts,
        sessions=result.sessions,
        events=result.events,
        relations=result.relations,
        dispositions=result.field_dispositions,
        warnings=result.warnings,
    )
    # title 回退：空 title 会话用首条 user 消息回填（所有 family 统一生效）。
    return _enrich_session_titles(gen)


def _assert_adaptation_integrity(family: str, result) -> None:
    """Fail closed when one file emits rows the generation cannot carry.

    ``ce_events`` has FKs onto ``ce_sessions(generation_id, session_id)`` and
    ``ce_source_artifacts(artifact_id)``; a dangling reference would abort the
    whole transaction with an opaque ``IntegrityError``. Checking per file names
    the family and the offending id instead.
    """

    session_ids = {s.session_id for s in result.sessions}
    artifact_ids = {a.artifact_id for a in result.artifacts}
    for session in result.sessions:
        if session.provenance.artifact_id not in artifact_ids:
            raise LiveSyncError(
                f"{family}: session {session.session_id} references artifact "
                f"{session.provenance.artifact_id} outside this file"
            )
    for event in result.events:
        if event.session_id not in session_ids:
            raise LiveSyncError(
                f"{family}: event {event.event_id} references session "
                f"{event.session_id} with no session record"
            )
        if event.provenance.artifact_id not in artifact_ids:
            raise LiveSyncError(
                f"{family}: event {event.event_id} references artifact "
                f"{event.provenance.artifact_id} outside this file"
            )


# ------------------------------------------------------------ row signatures
#
# A row signature is the stored column tuple minus the primary key. Comparing the
# stored signature with the freshly adapted one is what separates the three
# buckets of an apply: unchanged (not touched at all), dirty (archived to
# ``ce_*_versions`` and refreshed with an UPDATE) and new (INSERT OR IGNORE).
# The column order MUST mirror the corresponding ``_insert_*`` helper in
# ``event_repository``.

_EVENT_COLUMNS = (
    "event_id", "session_id", "kind", "artifact_id", "native_locator",
    "native_event_id", "occurred_at", "ordinal", "native_payload_ref",
    "content", "summary", "contract_version", "fidelity_json",
)

_SESSION_COLUMNS = (
    "session_id", "family", "native_session_id", "started_at", "ended_at",
    "artifact_id", "native_locator", "contract_version", "fidelity_json",
    "cwd", "git_branch", "model", "title", "stop_reason",
)

# The two derived row families. A relation carries no ``artifact_id`` (it is
# identified by its id, and its slot by both endpoints); a disposition is
# identified by ``(event_id, field_name)`` and belongs to the slot of that
# event. Both column orders mirror ``event_repository._insert_relations`` /
# ``_insert_events`` + ``_insert_dispositions``.
_RELATION_COLUMNS = (
    "relation_id", "source_event_id", "target_event_id", "relation_kind",
)

_DISPOSITION_COLUMNS = ("event_id", "field_name", "disposition", "reason")

# The append-only history tables (``event_schema``): the main table's columns
# with the version sequence spliced in after the primary key and
# ``superseded_at`` appended, exactly the column order of their DDL. A single
# id column means a one-element key tuple at the generic primitives below; a
# two-column key (dispositions) means a two-element one.
_EVENT_VERSION_COLUMNS = (
    "generation_id", "event_id", "version_seq", *_EVENT_COLUMNS[1:],
    "superseded_at",
)

_SESSION_VERSION_COLUMNS = (
    "generation_id", "session_id", "version_seq", *_SESSION_COLUMNS[1:],
    "superseded_at",
)

_RELATION_VERSION_COLUMNS = (
    "generation_id", "relation_id", "version_seq", *_RELATION_COLUMNS[1:],
    "superseded_at",
)

_DISPOSITION_VERSION_COLUMNS = (
    "generation_id", "event_id", "field_name", "version_seq",
    *_DISPOSITION_COLUMNS[2:], "superseded_at",
)

_EVENT_KEY = ("event_id",)
_SESSION_KEY = ("session_id",)
_RELATION_KEY = ("relation_id",)
_DISPOSITION_KEY = ("event_id", "field_name")


def _new_event_sigs(result) -> dict[str, tuple]:
    return {
        event.event_id: (
            event.session_id,
            event.kind.value,
            event.provenance.artifact_id,
            event.provenance.native_locator,
            event.provenance.native_event_id,
            event.occurred_at,
            event.ordinal,
            event.native_payload_ref,
            event.content,
            event.summary,
            event.provenance.contract_version or result.contract_version,
            _fidelity_json(event.fidelity),
        )
        for event in result.events
    }


def _new_session_sigs(result) -> dict[str, tuple]:
    return {
        session.session_id: (
            result.family,
            session.native_session_id,
            session.started_at,
            session.ended_at,
            session.provenance.artifact_id,
            session.provenance.native_locator,
            session.provenance.contract_version or result.contract_version,
            _fidelity_json(session.fidelity),
            session.cwd,
            session.git_branch,
            session.model,
            session.title,
            session.stop_reason,
        )
        for session in result.sessions
    }


def _new_relation_sigs(result) -> dict[str, tuple]:
    return {
        relation.relation_id: (
            relation.source_event_id,
            relation.target_event_id,
            relation.relation_kind.value,
        )
        for relation in result.relations
    }


def _new_disposition_sigs(result) -> dict[tuple, tuple]:
    """Freshly adapted disposition row signatures, keyed by ``(event_id, field_name)``.

    Both write routes are mirrored, because both land in the same table: the
    per-event dispositions (written by ``_insert_events``) and the
    adaptation-level ones (``_insert_dispositions``), which fall back to the
    generation's first event when the record carries no ``event_id``. The
    per-event route wins on a duplicate key, exactly the order in which
    ``_apply_slot`` runs the two ``INSERT OR IGNORE`` helpers — the fallback is
    therefore applied with ``setdefault`` and never overwrites it.
    """

    sigs: dict[tuple, tuple] = {}
    for event in result.events:
        for disp in event.field_dispositions:
            sigs[(str(event.event_id), disp.field_name)] = (
                disp.disposition.value,
                disp.reason,
            )
    if not result.field_dispositions:
        return sigs
    default_event_id = (
        str(result.events[0].event_id) if result.events else None
    )
    for disp in result.field_dispositions:
        event_id = getattr(disp, "event_id", None) or default_event_id
        if event_id is None:
            continue
        sigs.setdefault(
            (str(event_id), disp.field_name),
            (disp.disposition.value, disp.reason),
        )
    return sigs


def _read_slot_sigs(
    con: sqlite3.Connection, generation_id: str, slot_id: str
) -> tuple[dict, dict, dict, dict]:
    """Read every row signature this slot currently owns, keyed by identity.

    One reader for the whole slot, because "what does the store hold for this
    slot right now" is one question and each answer has to be compared against
    the same file's fresh adaptation. The *scoping* differs per table and is
    stated once here:

    * ``ce_events`` / ``ce_sessions`` carry the slot's ``artifact_id``;
    * ``ce_event_relations`` carries no ``artifact_id`` (a relation is
      identified by its id, its owning slot by *both* endpoints), so the slot's
      relation ids come from ``_slot_relation_ids`` and are then read back;
    * ``ce_field_dispositions`` hangs off ``event_id``, so the slot's event ids
      (just read above) select its rows.
    """

    events = {
        str(row[0]): tuple(row[1:])
        for row in con.execute(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM ce_events "
            "WHERE generation_id=? AND artifact_id=?",
            (generation_id, slot_id),
        )
    }
    sessions = {
        str(row[0]): tuple(row[1:])
        for row in con.execute(
            f"SELECT {', '.join(_SESSION_COLUMNS)} FROM ce_sessions "
            "WHERE generation_id=? AND artifact_id=?",
            (generation_id, slot_id),
        )
    }
    relations = _read_relation_sigs(
        con, generation_id, _slot_relation_ids(con, generation_id, slot_id)
    )
    dispositions = _read_disposition_sigs(con, generation_id, set(events))
    return events, sessions, relations, dispositions


def _read_relation_sigs(
    con: sqlite3.Connection, generation_id: str, relation_ids: set[str]
) -> dict[str, tuple]:
    """The stored signatures of ``relation_ids`` (keyed by ``relation_id``)."""

    sigs: dict[str, tuple] = {}
    for chunk in _chunks(sorted(relation_ids)):
        marks = _placeholders(len(chunk))
        for row in con.execute(
            f"SELECT {', '.join(_RELATION_COLUMNS)} FROM ce_event_relations "
            f"WHERE generation_id=? AND relation_id IN ({marks})",
            (generation_id, *chunk),
        ):
            sigs[str(row[0])] = tuple(row[1:])
    return sigs


def _read_disposition_sigs(
    con: sqlite3.Connection, generation_id: str, event_ids: set[str]
) -> dict[tuple, tuple]:
    """The stored dispositions of a slot's events, keyed ``(event_id, field_name)``."""

    sigs: dict[tuple, tuple] = {}
    for chunk in _chunks(sorted(event_ids)):
        marks = _placeholders(len(chunk))
        for row in con.execute(
            f"SELECT {', '.join(_DISPOSITION_COLUMNS)} FROM ce_field_dispositions "
            f"WHERE generation_id=? AND event_id IN ({marks})",
            (generation_id, *chunk),
        ):
            sigs[(str(row[0]), str(row[1]))] = tuple(row[2:])
    return sigs


def _slot_relation_ids(
    con: sqlite3.Connection, generation_id: str, slot_id: str
) -> set[str]:
    """Relation ids whose *both* endpoints are events of this slot.

    Adapters are handed a single-artifact set, so every emitted relation is
    intra-file; this is therefore the complete relation set the slot currently
    owns, obtained without a per-slot relation column. It is the scoping step
    ``_read_slot_sigs`` uses to read a slot's relation signatures and the
    "how many relations did this file newly contribute" step of ``_apply_slot``.

    Written as a single SELECT joining ``ce_events`` twice, the plan could only
    constrain the relation scan by ``generation_id``
    (``ix_ce_rel_gen_target (generation_id=?)``): every slot, however small or
    even empty, enumerated every relation row of the generation and probed
    ``ce_events`` twice per row (measured 6.7 s per call on the 8 GB staging db,
    ~88% of a live run's wall time). The slot's own event ids are read through
    ``ix_ce_events_gen_art`` and drive the relation scan through
    ``ce_relations_generation_source``; the far endpoint is then settled against
    that same id set, which is exactly "both endpoints are slot events" and
    visits no more relation rows than the slot itself emits. Asking for both
    endpoints as an ``IN (SELECT ...)`` predicate instead makes the planner treat
    the two lists as a cross-product driver and degrades badly on large slots.
    """

    event_ids = {
        str(row[0])
        for row in con.execute(
            "SELECT event_id FROM ce_events WHERE generation_id=? AND artifact_id=?",
            (generation_id, slot_id),
        )
    }
    if not event_ids:
        # No events, so no relation can have both endpoints inside the slot.
        return set()
    return {
        str(row[0])
        for row in con.execute(
            "SELECT r.relation_id, r.target_event_id FROM ce_event_relations r "
            "WHERE r.generation_id=? AND r.source_event_id IN "
            "(SELECT event_id FROM ce_events "
            " WHERE generation_id=? AND artifact_id=?)",
            (generation_id, generation_id, slot_id),
        )
        if str(row[1]) in event_ids
    }


def _next_version_seqs(
    con: sqlite3.Connection,
    table: str,
    key_columns: tuple[str, ...],
    generation_id: str,
    keys: set[tuple],
) -> dict[tuple, int]:
    """``{key: next version_seq}`` — one past the highest archived version.

    A key is a tuple of the row's identifying column values: one element for
    events / sessions / relations, ``(event_id, field_name)`` for dispositions,
    i.e. exactly the main table's primary key. Keys that were never archived get
    ``0``, so a history always starts at version 0 and only ever grows.
    ``table`` / ``key_columns`` are module constants, never caller input.
    """

    columns = ", ".join(key_columns)
    arity = _placeholders(len(key_columns))
    seqs: dict[tuple, int] = {}
    for chunk in _chunks(sorted(keys)):
        marks = ", ".join(f"({arity})" for _ in chunk)
        params: list = [generation_id]
        for key in chunk:
            params.extend(key)
        for row in con.execute(
            f"SELECT {columns}, MAX(version_seq) FROM {table} "
            f"WHERE generation_id=? AND ({columns}) IN (VALUES {marks}) "
            f"GROUP BY {columns}",
            params,
        ):
            seqs[tuple(str(value) for value in row[:-1])] = int(row[-1]) + 1
    return seqs


def _archive_rows(
    con: sqlite3.Connection,
    *,
    table: str,
    key_columns: tuple[str, ...],
    columns: tuple[str, ...],
    generation_id: str,
    rows: dict[tuple, tuple],
) -> int:
    """Append the *current* values of ``rows`` to an append-only history table.

    ``rows`` maps a row key to the stored non-key column tuple (the signature
    the slot was read with). Nothing is overwritten: the version sequence
    continues where the previous archive for that key stopped. Returns the
    number of rows appended.
    """

    if not rows:
        return 0
    superseded_at = _now()
    seqs = _next_version_seqs(
        con, table, key_columns, generation_id, set(rows)
    )
    payload = [
        (generation_id, *key, seqs.get(key, 0), *rows[key], superseded_at)
        for key in sorted(rows)
    ]
    con.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({_placeholders(len(columns))})",
        payload,
    )
    return len(payload)


def _update_rows(
    con: sqlite3.Connection,
    *,
    table: str,
    key_columns: tuple[str, ...],
    payload_columns: tuple[str, ...],
    generation_id: str,
    rows: dict[tuple, tuple],
) -> int:
    """Refresh the given rows **in place** (no INSERT, no DELETE).

    An ``UPDATE`` keeps the row's identity and its rowid, which is what lets a
    reader that walks ``canonical_*`` by rowid (and any future rowid cursor over
    ``ce_*``) see a value change without the row appearing to move or to be
    re-created. Returns the number of rows refreshed.
    """

    if not rows:
        return 0
    assignments = ", ".join(f"{column}=?" for column in payload_columns)
    matches = " AND ".join(f"{column}=?" for column in key_columns)
    payload = [
        (*rows[key], generation_id, *key) for key in sorted(rows)
    ]
    con.executemany(
        f"UPDATE {table} SET {assignments} "
        f"WHERE generation_id=? AND {matches}",
        payload,
    )
    return len(payload)


# ---- P0-1 staleness markers ------------------------------------------------
#
# The store is a collection, so the stale bucket still keeps every row it ever
# captured — but "kept as collected evidence" must be distinguishable from
# "still current", or the projection would keep counting messages the source no
# longer emits. Both markers are plain in-place UPDATEs on the row's ``stale_at``
# column (``event_schema`` DDL): NULL = current, non-NULL = the moment the apply
# observed the source no longer emitting it. They run inside the same
# ``BEGIN IMMEDIATE`` transaction as every other write of the apply, so a crash
# rolls the marker back with the rows it describes.
#
# Honest boundary (P4, deliberately out of scope here): canonical_* rows that
# were already projected from a now-stale event are not removed by this engine
# (the projection upsert is insert/refresh, never delete). What this buys is
# that the *current* computation is no longer fed stale events, and that
# staleness itself is queryable.

def _mark_stale_rows(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    slot_id: str,
    event_ids: set[str],
    session_ids: set[str],
) -> int:
    """Mark the slot's rows that the source no longer emits as stale.

    ``stale_at`` is set only where it is still NULL, so a re-apply of the same
    truncated file does not rewrite the timestamp every run (the first
    observation is the one that stays). Rows already marked keep their original
    marker; the collected values are never rewritten.
    """

    now = _now()
    marked = 0
    for chunk in _chunks(sorted(event_ids)):
        marks = _placeholders(len(chunk))
        cur = con.execute(
            f"UPDATE ce_events SET stale_at=? "
            f"WHERE generation_id=? AND artifact_id=? AND stale_at IS NULL "
            f"AND event_id IN ({marks})",
            (now, generation_id, slot_id, *chunk),
        )
        marked += max(cur.rowcount, 0)
    for chunk in _chunks(sorted(session_ids)):
        marks = _placeholders(len(chunk))
        cur = con.execute(
            f"UPDATE ce_sessions SET stale_at=? "
            f"WHERE generation_id=? AND artifact_id=? AND stale_at IS NULL "
            f"AND session_id IN ({marks})",
            (now, generation_id, slot_id, *chunk),
        )
        marked += max(cur.rowcount, 0)
    return marked


def _clear_stale_rows(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    slot_id: str,
    event_ids: set[str],
    session_ids: set[str],
) -> int:
    """Clear the stale marker of the slot's rows the source emits again.

    Round-trip of ``_mark_stale_rows``: a file that regrows (or reappears after
    a removal) re-emits ids the store already holds as stale rows. Whatever the
    bucket — dirty (values changed, archived + refreshed above) or unchanged
    (identical signature, no other write touched it) — the row is current
    again, so ``stale_at`` goes back to NULL. Only rows actually marked are
    written, so a healthy apply never touches this path.
    """

    cleared = 0
    for chunk in _chunks(sorted(event_ids)):
        marks = _placeholders(len(chunk))
        cur = con.execute(
            f"UPDATE ce_events SET stale_at=NULL "
            f"WHERE generation_id=? AND artifact_id=? AND stale_at IS NOT NULL "
            f"AND event_id IN ({marks})",
            (generation_id, slot_id, *chunk),
        )
        cleared += max(cur.rowcount, 0)
    for chunk in _chunks(sorted(session_ids)):
        marks = _placeholders(len(chunk))
        cur = con.execute(
            f"UPDATE ce_sessions SET stale_at=NULL "
            f"WHERE generation_id=? AND artifact_id=? AND stale_at IS NOT NULL "
            f"AND session_id IN ({marks})",
            (generation_id, slot_id, *chunk),
        )
        cleared += max(cur.rowcount, 0)
    return cleared


# ---- P1-5 cross-slot relation duplicates -----------------------------------
#
# A relation id is derived from native identity (claude's call/result id, e.g.
# ``rel-call:<call_id>:<n>``) while its endpoint event ids carry the per-copy
# record locator, so the *same* native session collected through two slots
# (two mirror paths) can present the store with the *same* relation id anchored
# at *different* endpoints. The second slot classifies the relation as new (its
# own endpoint set has never held the id), but ``_insert_relations`` is
# ``INSERT OR IGNORE``: the id is already stored, anchored at whichever copy
# applied first — the second copy's endpoints vanish without a trace and
# ``rows_inserted`` overcounts. Which copy a relation edge follows was
# therefore decided by apply order alone.
#
# The minimal honest repair has two halves, both inside the apply transaction:
# count what the ignore swallowed, and let the edge follow the *current* copy —
# but only when that is unambiguous (the incoming endpoints are all current and
# the stored ones are all stale). Anything else keeps the stored row and only
# counts. No replay, no relation rewrite beyond the two endpoint columns.

def _endpoint_staleness(
    con: sqlite3.Connection, generation_id: str, event_ids: list[str]
) -> tuple[str, ...]:
    """``("current" | "stale" | "absent", ...)`` for the given event ids.

    Relations carry no ``stale_at`` of their own (P0-1 marked events and
    sessions only), so a relation's currency is inferred from its endpoints:
    an edge pointing at stale events has left the current computation even
    though the row itself is unmarked.
    """

    states: list[str] = []
    for chunk in _chunks(event_ids):
        found = {
            str(row[0]): ("stale" if row[1] is not None else "current")
            for row in con.execute(
                "SELECT event_id, stale_at FROM ce_events "
                f"WHERE generation_id=? AND event_id IN ({_placeholders(len(chunk))})",
                (generation_id, *chunk),
            )
        }
        states.extend(found.get(event_id, "absent") for event_id in chunk)
    return tuple(states)


def _resolve_swallowed_relations(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    candidates: dict[str, tuple],
) -> tuple[int, int]:
    """Count (and prefer-current) the relations ``INSERT OR IGNORE`` swallowed.

    Runs *between* the event and relation inserts of :func:`_apply_slot`: the
    slot's own endpoint events are already stored (their ``stale_at`` state is
    final for this apply, and a repoint's UPDATE is FK-checked against them),
    while the fresh relation rows are not yet written — so a candidate found in
    ``ce_event_relations`` here was stored by an earlier apply of another slot,
    never by this one. ``candidates`` maps relation id to the freshly adapted
    ``(source, target, kind)`` signature. Returns ``(swallowed, repointed)``.

    A repoint refreshes only the two endpoint columns, and only under the
    unambiguous rule of the section comment; the replaced value is archived to
    ``ce_relation_versions`` first (the same "no value is rewritten away"
    contract the dirty bucket follows). A mixed endpoint state — one side
    current, one stale, an endpoint absent — keeps the stored row untouched.
    """

    swallowed = 0
    repointed = 0
    for chunk in _chunks(sorted(candidates)):
        for rid, src_stored, tgt_stored, kind in con.execute(
            "SELECT relation_id, source_event_id, target_event_id, relation_kind "
            "FROM ce_event_relations WHERE generation_id=? "
            f"AND relation_id IN ({_placeholders(len(chunk))})",
            (generation_id, *chunk),
        ).fetchall():
            swallowed += 1
            src_new, tgt_new, _kind = candidates[str(rid)]
            stored_state = _endpoint_staleness(
                con, generation_id, [src_stored, tgt_stored]
            )
            fresh_state = _endpoint_staleness(
                con, generation_id, [src_new, tgt_new]
            )
            if stored_state != ("stale", "stale") or fresh_state != ("current", "current"):
                continue
            _archive_rows(
                con,
                table="ce_relation_versions",
                key_columns=_RELATION_KEY,
                columns=_RELATION_VERSION_COLUMNS,
                generation_id=generation_id,
                rows={(str(rid),): (src_stored, tgt_stored, kind)},
            )
            con.execute(
                "UPDATE ce_event_relations SET source_event_id=?, target_event_id=? "
                "WHERE generation_id=? AND relation_id=?",
                (src_new, tgt_new, generation_id, str(rid)),
            )
            repointed += 1
    return swallowed, repointed


def _apply_slot(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    mirror_path: str,
    artifact,
    result,
) -> dict:
    """Grow one slot's rows in place (added / changed / marked stale).

    ``artifact`` / ``result`` are the already-captured, already-adapted outputs
    of this file: the caller needed them to classify added vs changed, so they
    are never recomputed here.
    """

    family = result.family
    slot_id = artifact.artifact_id

    old_events, old_sessions, old_relations, old_dispositions = _read_slot_sigs(
        con, generation_id, slot_id
    )
    new_events = _new_event_sigs(result)
    new_sessions = _new_session_sigs(result)
    new_relations = _new_relation_sigs(result)
    new_dispositions = _new_disposition_sigs(result)

    # Three buckets, and no deletion anywhere:
    #   stale  — stored id, no longer emitted -> kept as collected, but marked
    #            with ``stale_at = now`` (step 4 below) so the projection stops
    #            feeding on it;
    #   dirty  — stored id, different row values -> archived, then UPDATEd;
    #   new    — id the store has never seen -> INSERT OR IGNORE below.
    dirty_events = {
        eid
        for eid in set(old_events) & set(new_events)
        if old_events[eid] != new_events[eid]
    }
    dirty_sessions = {
        sid
        for sid in set(old_sessions) & set(new_sessions)
        if old_sessions[sid] != new_sessions[sid]
    }
    dirty_relations = {
        rid
        for rid in set(old_relations) & set(new_relations)
        if old_relations[rid] != new_relations[rid]
    }
    dirty_dispositions = {
        key
        for key in set(old_dispositions) & set(new_dispositions)
        if old_dispositions[key] != new_dispositions[key]
    }

    # 1) Keep what was collected: append the current values of every row that is
    #    about to be refreshed, so no value the store has ever seen is lost.
    rows_versioned = _archive_rows(
        con,
        table="ce_event_versions",
        key_columns=_EVENT_KEY,
        columns=_EVENT_VERSION_COLUMNS,
        generation_id=generation_id,
        rows={(eid,): old_events[eid] for eid in dirty_events},
    ) + _archive_rows(
        con,
        table="ce_session_versions",
        key_columns=_SESSION_KEY,
        columns=_SESSION_VERSION_COLUMNS,
        generation_id=generation_id,
        rows={(sid,): old_sessions[sid] for sid in dirty_sessions},
    ) + _archive_rows(
        con,
        table="ce_relation_versions",
        key_columns=_RELATION_KEY,
        columns=_RELATION_VERSION_COLUMNS,
        generation_id=generation_id,
        rows={(rid,): old_relations[rid] for rid in dirty_relations},
    ) + _archive_rows(
        con,
        table="ce_disposition_versions",
        key_columns=_DISPOSITION_KEY,
        columns=_DISPOSITION_VERSION_COLUMNS,
        generation_id=generation_id,
        rows={key: old_dispositions[key] for key in dirty_dispositions},
    )

    # 2) Rows the store sees for the first time. These must be written BEFORE the
    #    in-place refreshes below and never after: a refreshed relation carries
    #    its two endpoints, and both are FK-checked against ``ce_events`` at
    #    UPDATE time. A relation's identity can outlive an endpoint move (claude's
    #    call/result relation id comes from the native ``call_id``, while the
    #    endpoint ids carry the record's line number), so refreshing first would
    #    point at an event row this same apply has not inserted yet — which SQLite
    #    rejects, aborting the whole cycle. A row that disappeared upstream is
    #    kept as collected evidence in every one of the four families; a row whose
    #    identity survived with different values gets archived below and then
    #    refreshed, never silently kept at its first captured value.
    new_event_ids = set(new_events)
    new_session_ids = set(new_sessions)
    old_event_ids = set(old_events)
    old_session_ids = set(old_sessions)
    rows_inserted = (
        len(new_event_ids - old_event_ids)
        + len(new_session_ids - old_session_ids)
        + len(set(new_relations) - set(old_relations))
        + len(set(new_dispositions) - set(old_dispositions))
    )

    gen = _generation_input(result)
    _insert_artifacts(con, gen, generation_id)
    _refresh_artifact_row(con, artifact, family)
    _insert_sessions(con, gen, generation_id)
    _insert_events(con, gen, generation_id)

    # 2b) P1-5: relations the ignore below would silently drop (same relation
    #     id already stored from another slot's copy of the same native
    #     session). Must run here — after the event inserts (the slot's
    #     endpoints exist for the staleness check and for a repoint's FK) but
    #     before the relation inserts (a candidate found in the table was
    #     stored by an earlier slot's apply, never by this one). Count them,
    #     and prefer the current copy's endpoints when the stored anchor is
    #     fully stale. ``rows_inserted`` above was set arithmetic and assumed
    #     every new id landed; the swallowed ones did not, so they are
    #     subtracted to keep the counter honest.
    relations_ignored, relations_repointed = _resolve_swallowed_relations(
        con,
        generation_id,
        candidates={
            rid: new_relations[rid]
            for rid in set(new_relations) - set(old_relations)
        },
    )
    rows_inserted -= relations_ignored

    _insert_relations(con, gen, generation_id)
    _insert_dispositions(con, gen, generation_id)

    # 3) Refresh the dirty rows in place (an UPDATE, never a delete + re-insert).
    #    ``INSERT OR IGNORE`` above was a no-op for these ids, so an id that was
    #    already stored is refreshed here and nowhere else.
    rows_updated = _update_rows(
        con,
        table="ce_events",
        key_columns=_EVENT_KEY,
        payload_columns=_EVENT_COLUMNS[1:],
        generation_id=generation_id,
        rows={(eid,): new_events[eid] for eid in dirty_events},
    ) + _update_rows(
        con,
        table="ce_sessions",
        key_columns=_SESSION_KEY,
        payload_columns=_SESSION_COLUMNS[1:],
        generation_id=generation_id,
        rows={(sid,): new_sessions[sid] for sid in dirty_sessions},
    ) + _update_rows(
        con,
        table="ce_event_relations",
        key_columns=_RELATION_KEY,
        payload_columns=_RELATION_COLUMNS[1:],
        generation_id=generation_id,
        rows={(rid,): new_relations[rid] for rid in dirty_relations},
    ) + _update_rows(
        con,
        table="ce_field_dispositions",
        key_columns=_DISPOSITION_KEY,
        payload_columns=_DISPOSITION_COLUMNS[2:],
        generation_id=generation_id,
        rows={key: new_dispositions[key] for key in dirty_dispositions},
    )

    # 4) P0-1 staleness markers, in the same transaction as every write above.
    #    ``stale``  = stored ids this slot no longer emits (truncated/edited
    #    file): marked, never removed. ``resurrect`` = stored ids the source
    #    emits again (a file that regrows or reappears): the marker goes back to
    #    NULL — for dirty ids the row was just refreshed in place, for unchanged
    #    ids this is the only write they get. Read scoping note: ``old_events`` /
    #    ``old_sessions`` were read WITHOUT a stale filter, on purpose — a stale
    #    row must stay visible to this comparison or a re-emitted id could never
    #    be recognized and resurrected.
    stale_event_ids = old_event_ids - new_event_ids
    stale_session_ids = old_session_ids - new_session_ids
    rows_stale_marked = _mark_stale_rows(
        con,
        generation_id,
        slot_id=slot_id,
        event_ids=stale_event_ids,
        session_ids=stale_session_ids,
    )
    rows_stale_cleared = _clear_stale_rows(
        con,
        generation_id,
        slot_id=slot_id,
        event_ids=old_event_ids & new_event_ids,
        session_ids=old_session_ids & new_session_ids,
    )

    _touch_slot(
        con,
        slot_id=slot_id,
        family=family,
        mirror_path=mirror_path,
        content_hash=artifact.content_hash,
        byte_size=artifact.byte_size,
    )

    return {
        "slot_id": slot_id,
        "rows_inserted": rows_inserted,
        "rows_updated": rows_updated,
        "rows_versioned": rows_versioned,
        "rows_stale_marked": rows_stale_marked,
        "rows_stale_cleared": rows_stale_cleared,
        "relations_ignored_duplicates": relations_ignored,
        "relations_endpoint_refreshed": relations_repointed,
        "new_sessions": set(new_sessions),
        "new_event_ids": new_event_ids,
        "new_session_ids": new_session_ids,
        "new_relation_ids": set(new_relations),
    }


def _remove_slot(
    con: sqlite3.Connection,
    generation_id: str,
    slot_id: str,
    *,
    family: str,
    mirror_path: str,
) -> None:
    """Mark a vanished slot inactive and stale. No row of it is deleted.

    A source that disappeared is a *state* change, not a reason to erase what was
    collected from it: the slot's sessions, events (and their dispositions),
    relations and ``ce_source_artifacts`` provenance row all stay exactly as
    they were. But they are all marked ``stale_at = now`` (P0-1): the slot can
    no longer re-emit anything, so every row it owns has left the current
    computation and must stop being read as if it had not. The compatibility
    projection rows that were derived from them are NOT re-projected or removed
    here (the caller does not ask for this slot's sessions at all) — clearing
    those already-projected canonical rows is the P4 boundary.

    Only ``ce_live_slots.active`` flips to 0, and the removal — with the mirror
    path it happened to — is appended to ``ce_live_state['removed_slots']``
    (plus the run's sync-log detail) so it stays auditable. If the source
    reappears, the slot reactivates and :func:`_apply_slot` clears the stale
    markers of every id it emits again.
    """

    now = _now()
    con.execute(
        "UPDATE ce_events SET stale_at=? "
        "WHERE generation_id=? AND artifact_id=? AND stale_at IS NULL",
        (now, generation_id, slot_id),
    )
    con.execute(
        "UPDATE ce_sessions SET stale_at=? "
        "WHERE generation_id=? AND artifact_id=? AND stale_at IS NULL",
        (now, generation_id, slot_id),
    )
    con.execute(
        "UPDATE ce_live_slots SET active=0, last_synced_at=? WHERE slot_id=?",
        (now, slot_id),
    )
    _record_removal(
        con, slot_id=slot_id, family=family, mirror_path=mirror_path
    )


def _removed_slots(con: sqlite3.Connection) -> list[dict]:
    """The append-only removal record; never truncated, never rewritten."""

    raw = _get_state(con, REMOVED_SLOTS_STATE_KEY)
    try:
        history = json.loads(raw) if raw else []
    except ValueError:
        history = []
    return history if isinstance(history, list) else []


def _record_removal(
    con: sqlite3.Connection, *, slot_id: str, family: str, mirror_path: str
) -> None:
    """Append ``{slot_id, family, mirror_path, removed_at}`` for a deactivated slot.

    Appended, not replaced: the store keeps the history of which sources were
    deactivated and when. Entries accumulate one per removal event (a slot that
    is reactivated and removed again records a second entry) and no data row of
    the slot is involved.
    """

    history = _removed_slots(con)
    history.append(
        {
            "slot_id": slot_id,
            "family": family,
            "mirror_path": mirror_path,
            "removed_at": _now(),
        }
    )
    _set_state(con, REMOVED_SLOTS_STATE_KEY, json.dumps(history, sort_keys=True))


# ------------------------------------------------------------- slot rows


def _touch_slot(
    con: sqlite3.Connection,
    *,
    slot_id: str,
    family: str,
    mirror_path: str,
    content_hash: str,
    byte_size: int,
) -> None:
    """Upsert the slot: the slot id never changes, only its content version."""

    now = _now()
    con.execute(
        "INSERT OR IGNORE INTO ce_live_slots"
        "(slot_id, family, mirror_path, content_hash, byte_size, first_seen_at, "
        " last_synced_at, active) VALUES (?,?,?,?,?,?,?,1)",
        (slot_id, family, mirror_path, content_hash, byte_size, now, now),
    )
    con.execute(
        "UPDATE ce_live_slots SET content_hash=?, byte_size=?, last_synced_at=?, "
        "active=1 WHERE slot_id=?",
        (content_hash, byte_size, now, slot_id),
    )


def _refresh_artifact_row(con: sqlite3.Connection, artifact, family: str) -> None:
    """Refresh the slot's single ``ce_source_artifacts`` row in place.

    Investigation result (milestone 2): ``ce_source_artifacts`` is **global**, not
    per generation — its columns carry no ``generation_id`` and its primary key
    is ``artifact_id`` alone. With a stable slot id, one slot therefore has
    exactly ONE row, whose ``content_hash`` must mutate as the source edits;
    ``_insert_artifacts`` is ``INSERT OR IGNORE`` and would silently keep the
    first hash forever. So the row is refreshed explicitly here.

    Consequence while two generations coexist in one database: a reader that
    resolves an *older* generation's ``ce_events.artifact_id`` through
    ``ce_source_artifacts.content_hash`` will get the NEWEST bytes for that slot,
    because the row no longer remembers which hash that older generation saw.
    The old bytes are still in the content-addressed blob store, but only a
    retained reference can find them. Nothing downstream of *this* engine breaks:
    the compatibility projection is derived from ``ce_events`` (which keeps its
    own frozen ``content``/``summary`` text), not from the artifact hash. The
    stale-hash window is bounded by the migration in the design note — the old
    generation is dropped once the live generation is validated — and is the
    reason a per-generation artifact-hash history is the natural follow-up if
    two generations ever need to stay queryable at once.

    Nothing is deleted here: the ``ce_events``/``ce_sessions`` FK still points at
    this ``artifact_id``, so the row must exist for every live slot.
    """

    con.execute(
        "UPDATE ce_source_artifacts SET family=?, source_kind=?, content_hash=?, "
        "capture_method=?, relative_path=?, byte_size=?, schema_digest=?, "
        "privacy_dispositions=? WHERE artifact_id=?",
        (
            artifact.family or family,
            artifact.source_kind,
            artifact.content_hash,
            artifact.capture_method,
            artifact.relative_path,
            artifact.byte_size,
            artifact.schema_digest,
            json.dumps(list(artifact.privacy_dispositions), sort_keys=True),
            artifact.artifact_id,
        ),
    )


# ------------------------------------------------------------- projection


def _project_sessions(
    con: sqlite3.Connection, generation_id: str, session_ids: set[str]
) -> dict:
    """Upsert the compatibility rows for ``session_ids`` only.

    Exact because every projected row depends solely on one session and that
    session's own events (see the module docstring). Rows are matched by the
    origin-derived ``canonical_session_id`` / ``canonical_message_id``, missing
    rows are inserted and changed rows are refreshed in place — a row is never
    deleted. Two consequences are deliberate:

    * a session whose source disappeared keeps exactly the projection it was
      collected with, which is why the caller never asks for a deactivated
      slot's sessions here;
    * every ``canonical_*`` rowid stays stable (and is never recycled behind a
      reader's watermark), which is what the rowid cursor in
      ``retrieval/conversation_fts.py`` relies on.

    Reads go through ``con`` so rows written earlier in this transaction are
    visible.

    P0-1: both reads filter ``stale_at IS NULL`` — events (and sessions) the
    source no longer emits must not be counted into ``message_count`` or reach
    the canonical projection as current data. The stale ``canonical_*`` rows
    this leaves behind are the documented P4 boundary (see module docstring).
    """

    if not session_ids:
        return {
            "sessions": 0, "messages": 0, "tools": 0, "inserted": 0, "updated": 0,
        }

    ordered = sorted(session_ids)
    session_rows: list[dict] = []
    event_rows: list[dict] = []
    for chunk in _chunks(ordered):
        marks = _placeholders(len(chunk))
        session_rows.extend(
            dict(row)
            for row in con.execute(
                "SELECT session_id, family, native_session_id, started_at, "
                "ended_at, native_locator, contract_version, fidelity_json, "
                "cwd, git_branch, model, title, stop_reason "
                f"FROM ce_sessions WHERE generation_id=? AND session_id IN ({marks}) "
                "AND stale_at IS NULL "
                "ORDER BY session_id",
                (generation_id, *chunk),
            )
        )
        event_rows.extend(
            dict(row)
            for row in con.execute(
                "SELECT event_id, session_id, kind, artifact_id, native_locator, "
                "native_event_id, occurred_at, ordinal, native_payload_ref, "
                "content, summary, contract_version, fidelity_json "
                f"FROM ce_events WHERE generation_id=? AND session_id IN ({marks}) "
                "AND stale_at IS NULL "
                "ORDER BY ordinal, event_id",
                (generation_id, *chunk),
            )
        )

    report = compute_projection(generation_id, session_rows, event_rows)
    written = upsert_compatibility_projection(con, report)
    return {
        "sessions": len(report.sessions),
        "messages": len(report.messages),
        "tools": len(report.tools),
        # Rows actually written: unchanged rows are left untouched, so a repeat
        # apply of the same content reports zeros here.
        "inserted": sum(entry["inserted"] for entry in written.values()),
        "updated": sum(entry["updated"] for entry in written.values()),
    }


# ------------------------------------------------------------- invariants


def _assert_invariants(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    emitted_session_ids: list[str],
    emitted_event_ids: list[str],
) -> None:
    """Pre-commit gate: a violation must roll the whole apply back."""

    if len(emitted_session_ids) != len(set(emitted_session_ids)):
        raise LiveSyncError("duplicate session ids emitted in this apply")
    if len(emitted_event_ids) != len(set(emitted_event_ids)):
        raise LiveSyncError("duplicate event ids emitted in this apply")

    orphans = con.execute(
        "SELECT COUNT(*) FROM ce_events e WHERE e.generation_id=? AND NOT EXISTS "
        "(SELECT 1 FROM ce_sessions s WHERE s.generation_id=e.generation_id "
        " AND s.session_id=e.session_id)",
        (generation_id,),
    ).fetchone()[0]
    if orphans:
        # Cannot regress (nothing is ever deleted) but still guards a bad insert.
        raise LiveSyncError(f"{orphans} event(s) have no session row")

    missing = con.execute(
        "SELECT COUNT(*) FROM ce_events e WHERE e.generation_id=? AND NOT EXISTS "
        "(SELECT 1 FROM ce_source_artifacts a WHERE a.artifact_id=e.artifact_id)",
        (generation_id,),
    ).fetchone()[0]
    missing += con.execute(
        "SELECT COUNT(*) FROM ce_sessions s WHERE s.generation_id=? AND NOT EXISTS "
        "(SELECT 1 FROM ce_source_artifacts a WHERE a.artifact_id=s.artifact_id)",
        (generation_id,),
    ).fetchone()[0]
    if missing:
        raise LiveSyncError(
            f"{missing} row(s) reference an artifact missing from ce_source_artifacts"
        )

    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise LiveSyncError(
            f"foreign_key_check reported {len(violations)} violation(s): "
            f"{[list(row) for row in violations[:5]]}"
        )


# ------------------------------------------------------------------ engine


def _connect(db: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=60000")
    return con


def _load_slots(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    try:
        rows = con.execute(
            "SELECT slot_id, family, mirror_path, content_hash, byte_size, active "
            "FROM ce_live_slots"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # schema not applied yet (read-only inspection)
    return {str(row["mirror_path"]): row for row in rows}


def _empty_report(status: str, generation_id: str, started: float, **extra) -> dict:
    report = {
        "status": status,
        "generation_id": generation_id,
        "n_added": 0,
        "n_changed": 0,
        "n_removed": 0,
        "n_unchanged": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "rows_versioned": 0,
        "rows_stale_marked": 0,
        "rows_stale_cleared": 0,
        "relations_ignored_duplicates": 0,
        "relations_endpoint_refreshed": 0,
        "per_family": {},
        "touched_sessions": [],
        "duration_s": round(time.monotonic() - started, 3),
    }
    report.update(extra)
    return report


def live_sync_once(
    *,
    db: Path,
    mirror_root: Path,
    generation_id: str,
    dry_run: bool = False,
) -> dict:
    """Run one incremental pass over the collected sources.

    Enumerates ``mirror_root/<family>/...``, classifies every file against the
    ``ce_live_slots`` watermark (added / changed / removed, with an
    ``(mtime_ns, size)`` fingerprint fast path in ``ce_live_state``), and — in a
    single ``BEGIN IMMEDIATE`` transaction — grows the store with what each
    changed slot newly emits (inserting new rows, archiving + refreshing the rows
    whose values changed, leaving everything else exactly as collected) and
    refreshes the compatibility projection for the sessions the apply touched.
    Nothing is removed: the store is a collection, not a mirror of the sources.

    ``dry_run=True`` computes and returns the plan (classification counts) while
    writing nothing at all: no schema, no slot row, no log row, no blob, not even
    a transaction.
    """

    started = time.monotonic()
    db = Path(db)
    mirror_root = Path(mirror_root)
    generation_id = str(generation_id)
    # Convention shared with the CLI defaults (``--v2-stage`` ->
    # ``--v2-artifact-store``): the content-addressed blob store is the sibling
    # ``artifacts`` dir of the stage root.
    artifact_store = mirror_root.parent / "artifacts"

    if dry_run:
        if not db.exists():
            return _empty_report("dry-run", generation_id, started)
        con = _connect(db, readonly=True)
        try:
            scanned = scan_mirror(mirror_root)
            slots = _load_slots(con)
            fingerprints = json.loads(_get_state(con, FINGERPRINT_STATE_KEY) or "{}")
            plan = _plan(scanned, slots, fingerprints)
        finally:
            con.close()
        return _empty_report(
            "dry-run",
            generation_id,
            started,
            n_added=plan["n_added"],
            n_changed=plan["n_changed"],
            n_removed=plan["n_removed"],
            n_unchanged=plan["n_unchanged"],
            per_family=plan["per_family"],
            slots=plan["slots"],
            unregistered_families=plan["unregistered_families"],
        )

    # The schema (including every ce_live_* table) is owned by event_schema.
    event_schema.create_v2_schema(db)

    con = _connect(db)
    try:
        scanned = scan_mirror(mirror_root)
        slots = _load_slots(con)
        fingerprints = json.loads(_get_state(con, FINGERPRINT_STATE_KEY) or "{}")

        removed, unregistered_families = _classify_removed(slots, scanned)
        candidates = [
            (mirror_path, family, path)
            for mirror_path, (family, path) in sorted(scanned.items())
            if not _slot_is_current(slots.get(mirror_path), fingerprints, mirror_path, path)
        ]
        _assert_removal_guard(removed, slots)
        if not candidates and not removed:
            # Idempotent no-op: nothing moved, so nothing is written at all.
            return _empty_report(
                "no-op",
                generation_id,
                started,
                n_unchanged=len(scanned),
                unregistered_families=sorted(unregistered_families),
            )

        # Capture/adapt outside the write lock; only blob files are produced and
        # they are content-addressed, so a rollback leaves at most a deduped
        # blob that the next run reuses.
        #
        # P1-13: fingerprint every candidate BEFORE capturing it. The rows this
        # pass is about to write describe the file as it stood at that instant,
        # so only that pre-capture (mtime_ns, size) may be persisted as the
        # fast-path fingerprint (see ``_write_fingerprints``). Re-statting after
        # the capture would freeze a later stat over bytes the store does not
        # hold, permanently hiding a tail appended in the capture window.
        prepared: list[dict] = []
        pre_captured: dict[str, list] = {}
        for mirror_path, family, path in candidates:
            try:
                pre_captured[mirror_path] = _fingerprint(path)
            except OSError:
                pass  # the capture below fails the run the same way it did before
            artifact, result = _capture_and_adapt(
                path,
                family=family,
                mirror_path=mirror_path,
                artifact_store=artifact_store,
                byte_limit=_BYTE_LIMIT,
                count_limit=_COUNT_LIMIT,
            )
            _assert_adaptation_integrity(family, result)
            slot = slots.get(mirror_path)
            if _slot_active(slot) and slot["content_hash"] == artifact.content_hash:
                continue  # fingerprint moved, bytes identical (e.g. touch)
            prepared.append(
                {
                    "mirror_path": mirror_path,
                    "family": family,
                    "kind": "changed" if _slot_active(slot) else "added",
                    "artifact": artifact,
                    "result": result,
                }
            )

        if not prepared and not removed:
            # Only touch-only fingerprint movement: refresh the fast-path cache.
            con.execute("BEGIN IMMEDIATE")
            try:
                _write_fingerprints(con, scanned, overrides=pre_captured)
                con.execute("COMMIT")
            except BaseException:
                _rollback(con)
                raise
            return _empty_report(
                "no-op",
                generation_id,
                started,
                n_unchanged=len(scanned),
                unregistered_families=sorted(unregistered_families),
            )

        n_added = sum(1 for item in prepared if item["kind"] == "added")
        n_changed = len(prepared) - n_added
        per_family: dict[str, dict] = {}
        rows_inserted = 0
        rows_updated = 0
        rows_versioned = 0
        rows_stale_marked = 0
        rows_stale_cleared = 0
        relations_ignored_duplicates = 0
        relations_endpoint_refreshed = 0
        touched: set[str] = set()
        projected_sessions: list[str] = []
        emitted_events: list[str] = []
        projection = {
            "sessions": 0, "messages": 0, "tools": 0, "inserted": 0, "updated": 0,
        }

        con.execute("BEGIN IMMEDIATE")
        try:
            _ensure_generation(con, generation_id)

            for slot_id, family, mirror_path in removed:
                _remove_slot(
                    con,
                    generation_id,
                    slot_id,
                    family=family,
                    mirror_path=mirror_path,
                )
                _bump(per_family, family, removed=1)

            for item in prepared:
                family = item["family"]
                outcome = _apply_slot(
                    con,
                    generation_id,
                    mirror_path=item["mirror_path"],
                    artifact=item["artifact"],
                    result=item["result"],
                )
                # Only the sessions this file still emits are re-projected: the
                # rows of a deactivated slot (and of a session the source stopped
                # emitting) keep exactly the projection they were collected with.
                touched |= outcome["new_sessions"]
                rows_inserted += outcome["rows_inserted"]
                rows_updated += outcome["rows_updated"]
                rows_versioned += outcome["rows_versioned"]
                rows_stale_marked += outcome["rows_stale_marked"]
                rows_stale_cleared += outcome["rows_stale_cleared"]
                relations_ignored_duplicates += outcome[
                    "relations_ignored_duplicates"
                ]
                relations_endpoint_refreshed += outcome[
                    "relations_endpoint_refreshed"
                ]
                projected_sessions.extend(sorted(outcome["new_session_ids"]))
                emitted_events.extend(sorted(outcome["new_event_ids"]))
                _bump(
                    per_family,
                    family,
                    added=1 if item["kind"] == "added" else 0,
                    changed=1 if item["kind"] == "changed" else 0,
                    rows_inserted=outcome["rows_inserted"],
                    rows_updated=outcome["rows_updated"],
                    rows_versioned=outcome["rows_versioned"],
                    rows_stale_marked=outcome["rows_stale_marked"],
                    rows_stale_cleared=outcome["rows_stale_cleared"],
                    relations_ignored_duplicates=outcome[
                        "relations_ignored_duplicates"
                    ],
                    relations_endpoint_refreshed=outcome[
                        "relations_endpoint_refreshed"
                    ],
                )

            projection = _project_sessions(con, generation_id, touched)
            _assert_invariants(
                con,
                generation_id,
                emitted_session_ids=projected_sessions,
                emitted_event_ids=emitted_events,
            )
            _write_fingerprints(con, scanned, overrides=pre_captured)
            report = {
                "status": "ok",
                "generation_id": generation_id,
                "n_added": n_added,
                "n_changed": n_changed,
                "n_removed": len(removed),
                "n_unchanged": len(scanned) - len(prepared),
                "rows_inserted": rows_inserted,
                "rows_updated": rows_updated,
                "rows_versioned": rows_versioned,
                "rows_stale_marked": rows_stale_marked,
                "rows_stale_cleared": rows_stale_cleared,
                "relations_ignored_duplicates": relations_ignored_duplicates,
                "relations_endpoint_refreshed": relations_endpoint_refreshed,
                "per_family": per_family,
                "touched_sessions": sorted(touched),
                "removed_slots": [
                    {"slot_id": slot_id, "family": family,
                     "mirror_path": mirror_path}
                    for slot_id, family, mirror_path in removed
                ],
                "unregistered_families": sorted(unregistered_families),
                "projection": projection,
                "duration_s": round(time.monotonic() - started, 3),
            }
            _write_sync_log(con, report)
            _set_state(
                con, LAST_SYNC_STATE_KEY, json.dumps(report, sort_keys=True)
            )
            con.execute("COMMIT")
        except BaseException:
            _rollback(con)
            raise
        return report
    finally:
        con.close()


def _slot_active(slot: sqlite3.Row | None) -> bool:
    return slot is not None and bool(slot["active"])


def _slot_is_current(
    slot: sqlite3.Row | None,
    fingerprints: dict,
    mirror_path: str,
    path: Path,
) -> bool:
    """True when the slot is active and its ``(mtime_ns, size)`` did not move.

    This is the fast path: an unchanged file is neither hashed nor parsed. The
    fingerprint is only ever an accelerator — a miss falls through to a real
    content-hash comparison, so a stale cache can never hide a content change.
    """

    if not _slot_active(slot):
        return False
    cached = fingerprints.get(mirror_path)
    if not isinstance(cached, list) or len(cached) != 2:
        return False
    try:
        return cached == _fingerprint(path)
    except OSError:
        return False


def _plan(
    scanned: dict[str, tuple[str, Path]],
    slots: dict[str, sqlite3.Row],
    fingerprints: dict,
) -> dict:
    """Read-only classification used by ``dry_run`` (hashes, never captures).

    A candidate is classified by hashing the file (read-only) and comparing with
    the slot's stored ``content_hash``; nothing is captured, parsed or written.
    """

    n_added = n_changed = n_unchanged = 0
    per_family: dict[str, dict] = {}
    slots_out: list[dict] = []
    for mirror_path, (family, path) in sorted(scanned.items()):
        slot = slots.get(mirror_path)
        if _slot_is_current(slot, fingerprints, mirror_path, path):
            n_unchanged += 1
            continue
        digest = _file_digest(path)
        if not _slot_active(slot):
            n_added += 1
            _bump(per_family, family, added=1)
            slots_out.append({"mirror_path": mirror_path, "family": family, "kind": "added"})
        elif digest != slot["content_hash"]:
            n_changed += 1
            _bump(per_family, family, changed=1)
            slots_out.append({"mirror_path": mirror_path, "family": family, "kind": "changed"})
        else:
            n_unchanged += 1  # fingerprint moved, bytes identical (e.g. touch)
    removed, unregistered = _classify_removed(slots, scanned)
    for mirror_path in removed:
        _bump(per_family, str(slots[mirror_path]["family"]), removed=1)
    return {
        "n_added": n_added,
        "n_changed": n_changed,
        "n_removed": len(removed),
        "n_unchanged": n_unchanged,
        "per_family": per_family,
        "slots": slots_out,
        "unregistered_families": sorted(unregistered),
    }


def _bump(per_family: dict, family: str, **counts: int) -> None:
    entry = per_family.setdefault(
        family,
        {
            "added": 0, "changed": 0, "removed": 0,
            "rows_inserted": 0, "rows_updated": 0, "rows_versioned": 0,
        },
    )
    for key, value in counts.items():
        entry[key] = entry.get(key, 0) + value


def _ensure_generation(con: sqlite3.Connection, generation_id: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO ce_event_generations"
        "(generation_id, status, source_manifest_id, dataset_digest, created_at) "
        "VALUES (?,?,?,?,?)",
        (generation_id, "validated", "live-sync", "live-sync", _now()),
    )


def _write_fingerprints(
    con: sqlite3.Connection,
    scanned: dict,
    overrides: dict[str, list] | None = None,
) -> None:
    """Persist the fast-path ``(mtime_ns, size)`` fingerprints of the scan.

    P1-13: ``overrides`` carries the fingerprint stat taken **before** each
    captured candidate. The rows this pass wrote describe the file as it stood
    at that instant, so the stored fingerprint must describe that same instant:
    a file appended again during (or after) the capture then differs from the
    stored fingerprint and is re-collected by the next pass. Re-statting after
    the capture — the old behaviour — would freeze the post-capture stat over
    rows that do not hold the appended tail, hiding it from every later pass.
    Paths without an override were never captured; their fresh stat is exactly
    the state the store already agrees with.
    """

    overrides = overrides or {}
    fingerprints: dict[str, list] = {}
    for mirror_path, (_family, path) in sorted(scanned.items()):
        if mirror_path in overrides:
            fingerprints[mirror_path] = overrides[mirror_path]
            continue
        try:
            fingerprints[mirror_path] = _fingerprint(path)
        except OSError:
            continue
    _set_state(
        con, FINGERPRINT_STATE_KEY, json.dumps(fingerprints, sort_keys=True)
    )


def _write_sync_log(con: sqlite3.Connection, report: dict) -> None:
    """Append one row per apply. ``rows_pruned`` is always 0 (legacy column name:
    this engine never removes a collected row); the per-run counters and the
    deactivated slots are carried in ``detail``.
    """

    sync_id = hashlib.sha256(
        f"{report['generation_id']}|{_now()}|{uuid.uuid4().hex}".encode("utf-8")
    ).hexdigest()[:24]
    now = _now()
    con.execute(
        "INSERT INTO ce_live_sync_log"
        "(sync_id, started_at, finished_at, n_added, n_changed, n_removed, "
        " rows_pruned, rows_inserted, per_family, status, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            sync_id,
            now,
            now,
            report["n_added"],
            report["n_changed"],
            report["n_removed"],
            0,
            report["rows_inserted"],
            json.dumps(report["per_family"], sort_keys=True),
            report["status"],
            json.dumps(
                {
                    "touched_sessions": report["touched_sessions"],
                    "removed_slots": report.get("removed_slots", []),
                    "rows_updated": report.get("rows_updated", 0),
                    "rows_versioned": report.get("rows_versioned", 0),
                    "rows_stale_marked": report.get("rows_stale_marked", 0),
                    "rows_stale_cleared": report.get("rows_stale_cleared", 0),
                    "relations_ignored_duplicates": report.get(
                        "relations_ignored_duplicates", 0
                    ),
                    "relations_endpoint_refreshed": report.get(
                        "relations_endpoint_refreshed", 0
                    ),
                    "projection": report["projection"],
                    "duration_s": report["duration_s"],
                },
                sort_keys=True,
            ),
        ),
    )


def _rollback(con: sqlite3.Connection) -> None:
    try:
        con.execute("ROLLBACK")
    except sqlite3.Error:
        pass


__all__ = [
    "FINGERPRINT_STATE_KEY",
    "LAST_SYNC_STATE_KEY",
    "REMOVED_SLOTS_STATE_KEY",
    "LiveSyncError",
    "live_sync_once",
    "scan_mirror",
]
