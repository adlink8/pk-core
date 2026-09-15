"""Milestone 2: one-generation incremental conversation sync (live slots).

Why this module exists
----------------------
``pk-sync conversations --v2-native`` stages a **new immutable generation** per
run: the cohort id is derived from the whole staged set, so one changed byte
produces a new generation id, every mirrored file is re-parsed, and every event
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
generation and replaces data **per source slot**::

    changed file -> re-capture + adapt that file alone -> prune exactly the rows
    of that slot that are no longer current -> INSERT OR IGNORE the current rows

Correctness notes
-----------------
* **One transaction.** The whole apply runs under a single ``BEGIN IMMEDIATE``;
  a crash rolls back completely, so there is no recovery logic and no partial
  slot. The DB is never left half-migrated.
* **Prune set.** Because a surviving event id can still carry changed values
  (an edited message body keeps its native id and locator, and a session row's
  ``ended_at`` moves when a turn is appended), the prune set is
  ``stale ids (no longer emitted) ∪ dirty ids (same id, different row)``.
  Deleting only the stale ids would leave stale *content* behind and
  ``INSERT OR IGNORE`` could not repair it. Unchanged rows are never touched,
  which is what keeps a one-file edit to a handful of written rows.
* **FK-safe order.** No v2 FK declares ``ON DELETE CASCADE``, so deletes are
  ordered ``ce_event_relations -> ce_field_dispositions -> ce_events ->
  ce_sessions``. Relations are deleted from **both** endpoint columns.
  ``ce_source_artifacts`` rows are **kept** (the slot is marked ``active = 0``)
  so provenance/history of a removed source survives reconciliation.
* **Projection.** The compatibility rows are recomputed only for the sessions
  the apply touched. That is exact, not approximate: every projected row is a
  pure function of one session and that session's own events
  (``canonical_session_id = f(session_id)``, ``canonical_message_id =
  f(event_id)``). The whole generation is never re-projected.
* **Fast path.** A ``(mtime_ns, size)`` fingerprint per mirror path is kept in
  ``ce_live_state['mirror_fingerprints']``, so an unchanged file is neither
  hashed nor parsed.
* **No new tables.** The schema (including ``ce_live_slots`` / ``ce_live_state``
  / ``ce_live_sync_log`` and the three supporting indexes) is owned by
  ``event_schema``; this module declares no DDL of its own.

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
  exactly one live generation, mutated in place.
* A relation that disappears while **both** endpoints survive is only pruned
  when both endpoints belong to the replaced slot (the normal case, since every
  adapter is handed a single-artifact set). A cross-artifact relation whose
  endpoints both live in other, untouched slots is preserved.

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
    _ensure_tables,
    _norm_hash,
    compute_projection,
    write_compatibility_projection,
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

# SQLite's default parameter ceiling is 999 (older builds) / 32766 (3.32+).
# Chunk well below both so a large prune can never fail on arity.
_PARAM_CHUNK = 400

_SQLITE_MAGIC = b"SQLite format 3\x00"

# Directory names that are never a source family: the content-addressed blob
# store (``snapshots``) and the SQLite capture staging dir.
_SKIP_DIR_NAMES = frozenset({"artifacts", ".staging"})

# Capture limits, mirroring the CLI defaults for the v2 native path.
_BYTE_LIMIT = 600_000_000
_COUNT_LIMIT = 2_000


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


# ------------------------------------------------------------- mirror scan


def scan_mirror(mirror_root: Path) -> dict[str, tuple[str, Path]]:
    """Enumerate the mirrored sources as ``{mirror_path: (family, path)}``.

    ``mirror_path`` is the path relative to ``mirror_root`` (``"<family>/<rel>"``,
    via ``discovery.mirror_path_for``) — the same string the full-rebuild path
    feeds to ``capture_*`` as ``mirror_path=``, which is what makes the slot id
    identical between an incremental apply and a fresh rebuild.

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
    """Capture one mirror file (WAL-safe for SQLite) and adapt it in isolation.

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
# A row signature is the stored column tuple minus the primary key. It exists so
# the prune set can include rows whose id survived but whose values did not —
# ``INSERT OR IGNORE`` alone cannot refresh those. The column order MUST mirror
# the corresponding ``_insert_*`` helper in ``event_repository``.

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


def _read_slot_sigs(
    con: sqlite3.Connection, generation_id: str, slot_id: str
) -> tuple[dict[str, tuple], dict[str, tuple]]:
    """Read the slot's current event/session row signatures (keyed by id)."""

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
    return events, sessions


def _slot_relation_ids(
    con: sqlite3.Connection, generation_id: str, slot_id: str
) -> set[str]:
    """Relation ids whose *both* endpoints are events of this slot.

    Adapters are handed a single-artifact set, so every emitted relation is
    intra-file; this is therefore the complete relation set the slot currently
    owns, obtained without a per-slot relation column.
    """

    return {
        str(row[0])
        for row in con.execute(
            "SELECT r.relation_id FROM ce_event_relations r "
            "JOIN ce_events a ON a.generation_id=r.generation_id "
            "  AND a.event_id=r.source_event_id "
            "JOIN ce_events b ON b.generation_id=r.generation_id "
            "  AND b.event_id=r.target_event_id "
            "WHERE r.generation_id=? AND a.artifact_id=? AND b.artifact_id=?",
            (generation_id, slot_id, slot_id),
        )
    }


def _relation_ids_touching(
    con: sqlite3.Connection, generation_id: str, event_ids: set[str]
) -> set[str]:
    """Relation ids with an endpoint in ``event_ids`` (either side)."""

    found: set[str] = set()
    for chunk in _chunks(sorted(event_ids)):
        marks = _placeholders(len(chunk))
        found.update(
            str(row[0])
            for row in con.execute(
                "SELECT relation_id FROM ce_event_relations "
                f"WHERE generation_id=? AND (source_event_id IN ({marks}) "
                f"OR target_event_id IN ({marks}))",
                (generation_id, *chunk, *chunk),
            )
        )
    return found


# ------------------------------------------------------------------ prune


def _prune(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    relation_ids: set[str],
    event_ids: set[str],
    session_ids: set[str],
) -> int:
    """Delete the given rows in FK-safe order; return the row count deleted."""

    deleted = 0
    for chunk in _chunks(sorted(relation_ids)):
        marks = _placeholders(len(chunk))
        cursor = con.execute(
            f"DELETE FROM ce_event_relations "
            f"WHERE generation_id=? AND relation_id IN ({marks})",
            (generation_id, *chunk),
        )
        deleted += max(cursor.rowcount, 0)
    for chunk in _chunks(sorted(event_ids)):
        marks = _placeholders(len(chunk))
        cursor = con.execute(
            f"DELETE FROM ce_field_dispositions "
            f"WHERE generation_id=? AND event_id IN ({marks})",
            (generation_id, *chunk),
        )
        deleted += max(cursor.rowcount, 0)
    for chunk in _chunks(sorted(event_ids)):
        marks = _placeholders(len(chunk))
        cursor = con.execute(
            f"DELETE FROM ce_events "
            f"WHERE generation_id=? AND event_id IN ({marks})",
            (generation_id, *chunk),
        )
        deleted += max(cursor.rowcount, 0)
    for chunk in _chunks(sorted(session_ids)):
        marks = _placeholders(len(chunk))
        # Defensive: a session still referenced by an event must survive, or the
        # FK check aborts the transaction. Sessions are slot-private by
        # construction, so this is a guard rather than a normal path.
        cursor = con.execute(
            f"DELETE FROM ce_sessions "
            f"WHERE generation_id=? AND session_id IN ({marks}) "
            "AND session_id NOT IN "
            "(SELECT session_id FROM ce_events WHERE generation_id=?)",
            (generation_id, *chunk, generation_id),
        )
        deleted += max(cursor.rowcount, 0)
    return deleted


def _apply_slot(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    mirror_path: str,
    artifact,
    result,
) -> dict:
    """Replace one slot's rows in place (added / changed).

    ``artifact`` / ``result`` are the already-captured, already-adapted outputs
    of this file: the caller needed them to classify added vs changed, so they
    are never recomputed here.
    """

    family = result.family
    slot_id = artifact.artifact_id

    old_events, old_sessions = _read_slot_sigs(con, generation_id, slot_id)
    new_events = _new_event_sigs(result)
    new_sessions = _new_session_sigs(result)

    # Prune set: ids that disappeared ("no longer emitted") UNION ids whose row
    # values changed (an edit that keeps a native id would otherwise leave stale
    # content behind, because INSERT OR IGNORE is a no-op for a present id).
    stale_events = set(old_events) - set(new_events)
    dirty_events = {
        eid
        for eid in set(old_events) & set(new_events)
        if old_events[eid] != new_events[eid]
    }
    stale_sessions = set(old_sessions) - set(new_sessions)
    dirty_sessions = {
        sid
        for sid in set(old_sessions) & set(new_sessions)
        if old_sessions[sid] != new_sessions[sid]
    }
    sessions_to_prune = stale_sessions | dirty_sessions
    # A pruned session row must not be left with events pointing at it, so its
    # old events are replaced too (FK: ce_events -> ce_sessions).
    events_to_prune = stale_events | dirty_events | {
        eid
        for eid, sig in old_events.items()
        if sig[0] in sessions_to_prune
    }

    old_relations = _slot_relation_ids(con, generation_id, slot_id)
    new_relations = {r.relation_id for r in result.relations}
    relations_to_prune = (old_relations - new_relations) | _relation_ids_touching(
        con, generation_id, events_to_prune
    )

    rows_pruned = _prune(
        con,
        generation_id,
        relation_ids=relations_to_prune,
        event_ids=events_to_prune,
        session_ids=sessions_to_prune,
    )
    # Rows actually written: ids that were never present, plus the pruned rows
    # that are re-inserted (a pruned stale row is NOT re-inserted).
    new_event_ids = set(new_events)
    new_session_ids = set(new_sessions)
    old_event_ids = set(old_events)
    old_session_ids = set(old_sessions)
    rows_inserted = (
        len(new_event_ids - old_event_ids)
        + len(new_session_ids - old_session_ids)
        + len(new_relations - old_relations)
        + len(events_to_prune & new_event_ids)
        + len(sessions_to_prune & new_session_ids)
        + len(relations_to_prune & new_relations)
    )

    gen = _generation_input(result)
    _insert_artifacts(con, gen, generation_id)
    _refresh_artifact_row(con, artifact, family)
    _insert_sessions(con, gen, generation_id)
    _insert_events(con, gen, generation_id)
    _insert_relations(con, gen, generation_id)
    _insert_dispositions(con, gen, generation_id)

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
        "rows_pruned": rows_pruned,
        "rows_inserted": rows_inserted,
        "old_sessions": set(old_sessions),
        "new_sessions": set(new_sessions),
        "new_event_ids": set(new_events),
        "new_session_ids": set(new_sessions),
        "new_relation_ids": new_relations,
    }


def _remove_slot(
    con: sqlite3.Connection, generation_id: str, slot_id: str
) -> dict:
    """Prune a vanished slot's rows and mark the slot inactive.

    ``ce_source_artifacts`` is deliberately NOT deleted: the slot's provenance
    row is retained so "this artifact existed and belonged to this family/path"
    survives reconciliation. Nothing references it once its rows are pruned, so
    keeping it is FK-safe and cheap.
    """

    old_events, old_sessions = _read_slot_sigs(con, generation_id, slot_id)
    event_ids = set(old_events)
    session_ids = set(old_sessions)
    relations = _slot_relation_ids(con, generation_id, slot_id) | (
        _relation_ids_touching(con, generation_id, event_ids)
    )
    rows_pruned = _prune(
        con,
        generation_id,
        relation_ids=relations,
        event_ids=event_ids,
        session_ids=session_ids,
    )
    con.execute(
        "UPDATE ce_live_slots SET active=0, last_synced_at=? WHERE slot_id=?",
        (_now(), slot_id),
    )
    return {
        "rows_pruned": rows_pruned,
        "rows_inserted": 0,
        "old_sessions": session_ids,
        "new_sessions": set(),
        "new_event_ids": set(),
        "new_session_ids": set(),
        "new_relation_ids": set(),
    }


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
    """Rebuild the compatibility rows for ``session_ids`` only.

    Exact because every projected row depends solely on one session and that
    session's own events (see the module docstring). Rows are deleted by
    ``canonical_session_id``, which is a pure function of the ce ``session_id``,
    so a session that disappeared is dropped and a session whose content changed
    is rewritten under the same canonical id. Reads go through ``con`` so rows
    written earlier in this transaction are visible.
    """

    if not session_ids:
        return {"sessions": 0, "messages": 0, "tools": 0}
    _ensure_tables(con)

    ordered = sorted(session_ids)
    canonical = [_norm_hash("v2|cs", sid) for sid in ordered]
    for chunk in _chunks(canonical):
        marks = _placeholders(len(chunk))
        con.execute(
            f"DELETE FROM canonical_tool_events "
            f"WHERE canonical_session_id IN ({marks})",
            chunk,
        )
        con.execute(
            f"DELETE FROM canonical_messages "
            f"WHERE canonical_session_id IN ({marks})",
            chunk,
        )
        con.execute(
            f"DELETE FROM canonical_sessions "
            f"WHERE canonical_session_id IN ({marks})",
            chunk,
        )

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
                "ORDER BY ordinal, event_id",
                (generation_id, *chunk),
            )
        )
    report = compute_projection(generation_id, session_rows, event_rows)
    write_compatibility_projection(con, report)
    return {
        "sessions": len(report.sessions),
        "messages": len(report.messages),
        "tools": len(report.tools),
    }


# ------------------------------------------------------------- invariants


def _assert_invariants(
    con: sqlite3.Connection,
    generation_id: str,
    *,
    inserted_session_ids: list[str],
    inserted_event_ids: list[str],
) -> None:
    """Pre-commit gate: a violation must roll the whole apply back."""

    if len(inserted_session_ids) != len(set(inserted_session_ids)):
        raise LiveSyncError("duplicate session ids emitted in this apply")
    if len(inserted_event_ids) != len(set(inserted_event_ids)):
        raise LiveSyncError("duplicate event ids emitted in this apply")

    orphans = con.execute(
        "SELECT COUNT(*) FROM ce_events e WHERE e.generation_id=? AND NOT EXISTS "
        "(SELECT 1 FROM ce_sessions s WHERE s.generation_id=e.generation_id "
        " AND s.session_id=e.session_id)",
        (generation_id,),
    ).fetchone()[0]
    if orphans:
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
        "rows_pruned": 0,
        "rows_inserted": 0,
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
    """Run one incremental pass over the mirrored sources.

    Enumerates ``mirror_root/<family>/...``, classifies every file against the
    ``ce_live_slots`` watermark (added / changed / removed, with an
    ``(mtime_ns, size)`` fingerprint fast path in ``ce_live_state``), and — in a
    single ``BEGIN IMMEDIATE`` transaction — prunes + re-inserts only the rows
    each changed slot no longer / now emits, refreshing the compatibility
    projection for the touched sessions only.

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
        )

    # The schema (including every ce_live_* table) is owned by event_schema.
    event_schema.create_v2_schema(db)

    con = _connect(db)
    try:
        scanned = scan_mirror(mirror_root)
        slots = _load_slots(con)
        fingerprints = json.loads(_get_state(con, FINGERPRINT_STATE_KEY) or "{}")

        removed = [
            (slot["slot_id"], slot["family"], mirror_path)
            for mirror_path, slot in slots.items()
            if slot["active"] and mirror_path not in scanned
        ]
        candidates = [
            (mirror_path, family, path)
            for mirror_path, (family, path) in sorted(scanned.items())
            if not _slot_is_current(slots.get(mirror_path), fingerprints, mirror_path, path)
        ]
        if not candidates and not removed:
            # Idempotent no-op: nothing moved, so nothing is written at all.
            return _empty_report(
                "no-op",
                generation_id,
                started,
                n_unchanged=len(scanned),
            )

        # Capture/adapt outside the write lock; only blob files are produced and
        # they are content-addressed, so a rollback leaves at most a deduped
        # blob that the next run reuses.
        prepared: list[dict] = []
        for mirror_path, family, path in candidates:
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
                _write_fingerprints(con, scanned)
                con.execute("COMMIT")
            except BaseException:
                _rollback(con)
                raise
            return _empty_report(
                "no-op", generation_id, started, n_unchanged=len(scanned)
            )

        n_added = sum(1 for item in prepared if item["kind"] == "added")
        n_changed = len(prepared) - n_added
        per_family: dict[str, dict] = {}
        rows_pruned = 0
        rows_inserted = 0
        touched: set[str] = set()
        inserted_sessions: list[str] = []
        inserted_events: list[str] = []
        projection = {"sessions": 0, "messages": 0, "tools": 0}

        con.execute("BEGIN IMMEDIATE")
        try:
            _ensure_generation(con, generation_id)

            for slot_id, family, mirror_path in removed:
                outcome = _remove_slot(con, generation_id, slot_id)
                touched |= outcome["old_sessions"]
                rows_pruned += outcome["rows_pruned"]
                _bump(per_family, family, removed=1, rows_pruned=outcome["rows_pruned"])

            for item in prepared:
                family = item["family"]
                outcome = _apply_slot(
                    con,
                    generation_id,
                    mirror_path=item["mirror_path"],
                    artifact=item["artifact"],
                    result=item["result"],
                )
                touched |= outcome["old_sessions"] | outcome["new_sessions"]
                rows_pruned += outcome["rows_pruned"]
                rows_inserted += outcome["rows_inserted"]
                inserted_sessions.extend(sorted(outcome["new_session_ids"]))
                inserted_events.extend(sorted(outcome["new_event_ids"]))
                _bump(
                    per_family,
                    family,
                    added=1 if item["kind"] == "added" else 0,
                    changed=1 if item["kind"] == "changed" else 0,
                    rows_pruned=outcome["rows_pruned"],
                    rows_inserted=outcome["rows_inserted"],
                )

            projection = _project_sessions(con, generation_id, touched)
            _assert_invariants(
                con,
                generation_id,
                inserted_session_ids=inserted_sessions,
                inserted_event_ids=inserted_events,
            )
            _write_fingerprints(con, scanned)
            report = {
                "status": "ok",
                "generation_id": generation_id,
                "n_added": n_added,
                "n_changed": n_changed,
                "n_removed": len(removed),
                "n_unchanged": len(scanned) - len(prepared),
                "rows_pruned": rows_pruned,
                "rows_inserted": rows_inserted,
                "per_family": per_family,
                "touched_sessions": sorted(touched),
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
    removed = [
        mirror_path
        for mirror_path, slot in slots.items()
        if slot["active"] and mirror_path not in scanned
    ]
    for mirror_path in removed:
        _bump(per_family, str(slots[mirror_path]["family"]), removed=1)
    return {
        "n_added": n_added,
        "n_changed": n_changed,
        "n_removed": len(removed),
        "n_unchanged": n_unchanged,
        "per_family": per_family,
        "slots": slots_out,
    }


def _bump(per_family: dict, family: str, **counts: int) -> None:
    entry = per_family.setdefault(
        family,
        {"added": 0, "changed": 0, "removed": 0, "rows_pruned": 0, "rows_inserted": 0},
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


def _write_fingerprints(con: sqlite3.Connection, scanned: dict) -> None:
    fingerprints = {}
    for mirror_path, (_family, path) in sorted(scanned.items()):
        try:
            fingerprints[mirror_path] = _fingerprint(path)
        except OSError:
            continue
    _set_state(
        con, FINGERPRINT_STATE_KEY, json.dumps(fingerprints, sort_keys=True)
    )


def _write_sync_log(con: sqlite3.Connection, report: dict) -> None:
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
            report["rows_pruned"],
            report["rows_inserted"],
            json.dumps(report["per_family"], sort_keys=True),
            report["status"],
            json.dumps(
                {
                    "touched_sessions": report["touched_sessions"],
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
    "LiveSyncError",
    "live_sync_once",
    "scan_mirror",
]
