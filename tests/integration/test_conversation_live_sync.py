"""Milestone 2: incremental live-sync (stable slot identity, append-only store).

RED/GREEN tests for :func:`personal_knowledge.application.conversation.live_sync.
live_sync_once`. The engine keeps ONE live generation and, per source slot,
collects what that file newly emits and refreshes in place the rows whose values
changed. Rows already collected are **never removed** — not when the source
stops emitting a message, not when the source file vanishes, not when a slot is
deactivated (the store is a collection, not a mirror of the current source).

The load-bearing oracle is the **equality oracle**: after an incremental apply
of an in-place edit (same native ids, one changed body) the compatibility
projection (``canonical_sessions`` / ``canonical_messages``) must be row-for-row
identical to a fresh full rebuild of the same corpus, while a removed source
makes the live store a strict **superset** of the rebuild (the rebuild drops it,
the live store keeps it). Any missed insert or missed in-place refresh shows up
there and nowhere else.

All tests run against temporary SQLite files under ``tmp_path``. No live
database, no ``data/``, no network, no provider calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources.claude_qoder import (
    CONTRACT_VERSION as CLAUDE_CONTRACT_VERSION,
)
from personal_knowledge.adapters.conversation_sources.snapshots import (
    make_slot_artifact_id,
)
from personal_knowledge.application.conversation import (
    live_sync as live_sync_module,
)
from personal_knowledge.application.conversation.compatibility_projection import (
    MESSAGE_KINDS,
    build_compatibility_projection,
    clear_compatibility_projection,
    upsert_compatibility_projection,
)
from personal_knowledge.application.conversation.event_schema import (
    create_v2_schema,
)
from personal_knowledge.application.conversation.live_sync import (
    LiveSyncError,
    _slot_relation_ids,
    live_sync_once,
)
from personal_knowledge.application.run_pipeline import shadow_conversation_generation
from personal_knowledge.core.conversation_events import make_event_id

FAMILY = "codex"
GENERATION = "live-gen-1"
DEFAULT_FILES = ("a.jsonl", "b.jsonl", "c.jsonl", "d.jsonl")


# --------------------------------------------------------------- synthetic corpus


def _codex_session(
    session_id: str, turns: int, *, revised: bool = False,
    revision: str | None = None,
) -> str:
    """A synthetic Codex JSONL export: meta, then per-turn context/user/answer.

    ``revised=True`` / ``revision="…"`` rewrite the FIRST answer body only,
    keeping the line count and every native id/locator unchanged. That is the
    hardest edit shape for an incremental engine: the event id survives while its
    row content does not, so it exercises the "same id, different row" path — an
    engine that only handled *vanished* ids would silently keep the stale text.
    """

    records: list[dict] = [
        {
            "type": "session_meta",
            "session_id": session_id,
            "timestamp": "2026-07-01T10:00:00Z",
            "model": "gpt-5",
        }
    ]
    for turn in range(1, turns + 1):
        records.append(
            {
                "type": "turn_context",
                "session_id": session_id,
                "turn_id": f"turn_{turn}",
                "prompt": f"prompt {turn} of {session_id}",
                "timestamp": f"2026-07-01T10:{turn:02d}:00Z",
            }
        )
        records.append(
            {
                "type": "event_msg",
                "session_id": session_id,
                "timestamp": f"2026-07-01T10:{turn:02d}:01Z",
                "payload": {
                    "type": "user_message",
                    "message": f"question {turn} of {session_id}",
                },
            }
        )
        answer = f"answer {turn} of {session_id}"
        if turn == 1 and revision is not None:
            answer += f" ({revision})"
        elif revised and turn == 1:
            answer += " (revised)"
        records.append(
            {
                "type": "response_item",
                "session_id": session_id,
                "turn_id": f"turn_{turn}",
                "item_id": f"resp_{session_id}_{turn}",
                "role": "assistant",
                "content": answer,
                "timestamp": f"2026-07-01T10:{turn:02d}:05Z",
            }
        )
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _write(mirror_root: Path, name: str, text: str) -> Path:
    family_dir = mirror_root / FAMILY
    family_dir.mkdir(parents=True, exist_ok=True)
    path = family_dir / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------- second family, for derived-row history
#
# ``claude`` is the family where both *derived* row families are reachable from
# one small file, and where each of them can hold a different value under the
# same identity:
#
# * the ``parentUuid`` link is a relation whose id is derived from the two
#   endpoints but whose *kind* is the record's ``isSidechain`` flag, so flipping
#   that flag changes the kind while the relation id stays exactly the same;
# * a ``thinking`` content block always carries a ``reasoning_content``
#   disposition whose *value* flips between UNAVAILABLE (no plaintext) and
#   MAPPED (plaintext), while the event id (built from the locator and the
#   native id) does not move when only the text changes.

CLAUDE = "claude"


def _claude_session(
    session_id: str, *, sidechain: bool = False, thinking: str | None = None
) -> str:
    """A minimal Claude/Qoder JSONL DAG: one user record and one assistant record.

    The assistant record carries the ``parentUuid`` link (one relation per
    content block) and one ``thinking`` block (one disposition), so a single
    edit can move either derived row's value without touching any native id.
    """

    records = [
        {
            "type": "user",
            "uuid": f"{session_id}-u1",
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": "2026-07-01T10:00:00Z",
            "stop_reason": None,
            "message": {"role": "user", "content": f"question of {session_id}"},
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-u2",
            "parentUuid": f"{session_id}-u1",
            "isSidechain": sidechain,
            "sessionId": session_id,
            "timestamp": "2026-07-01T10:00:01Z",
            "stop_reason": "end_turn",
            "message": {
                "role": "assistant",
                "model": "claude-x",
                "content": [
                    {"type": "thinking"} if thinking is None
                    else {"type": "thinking", "thinking": thinking},
                    {"type": "text", "text": f"answer of {session_id}"},
                ],
            },
        },
    ]
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _claude_user_only(session_id: str) -> str:
    """The same file with the linked assistant record (and its block) removed."""

    return json.dumps(
        {
            "type": "user",
            "uuid": f"{session_id}-u1",
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": "2026-07-01T10:00:00Z",
            "stop_reason": None,
            "message": {"role": "user", "content": f"question of {session_id}"},
        }
    ) + "\n"


def _write_claude(mirror_root: Path, name: str, text: str) -> Path:
    family_dir = mirror_root / CLAUDE
    family_dir.mkdir(parents=True, exist_ok=True)
    path = family_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _claude_slot(name: str) -> str:
    """The expected stable slot identity for ``claude/<name>``."""

    return make_slot_artifact_id(CLAUDE, f"{CLAUDE}{os.sep}{name}")


# ``gemini`` is the family that declares dispositions at the *adaptation* level
# (no ``event_id`` on the record), which is the third route into
# ``ce_field_dispositions`` and is attached to the generation's first event.

GEMINI = "gemini"


def _gemini_doc(*, usage: bool) -> str:
    assistant: dict = {"role": "assistant", "content": "answer of gemini-doc"}
    if usage:
        assistant["input_tokens"] = 5
    return json.dumps(
        {
            "session_id": "gemini-sess",
            "model": "gemini-x",
            "messages": [
                {"role": "user", "content": "question of gemini-doc"},
                assistant,
            ],
        }
    )


def _write_gemini(mirror_root: Path, name: str, text: str) -> Path:
    family_dir = mirror_root / GEMINI
    family_dir.mkdir(parents=True, exist_ok=True)
    path = family_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _build_corpus(
    mirror_root: Path, names: tuple[str, ...] = DEFAULT_FILES, turns: int = 4
) -> None:
    for name in names:
        session = f"sess_{name.split('.')[0]}"
        _write(mirror_root, name, _codex_session(session, turns))


def _slot_id(name: str) -> str:
    """The expected stable slot identity for ``codex/<name>``.

    The mirror path is produced by ``discovery.mirror_path_for`` (the shared
    milestone-1 seam), so it uses the native separator; the identity is
    ``sha256("art|<family>|<mirror_path>")`` over exactly that string.
    """

    return make_slot_artifact_id(FAMILY, f"{FAMILY}{os.sep}{name}")


def _slot_row(db: Path, name: str) -> tuple:
    """``(slot_id, family, mirror_path, active)`` for the slot owning ``name``."""

    rows = _rows(
        db,
        "SELECT slot_id, family, mirror_path, active FROM ce_live_slots "
        "WHERE mirror_path LIKE ?",
        (f"%{name}",),
    )
    assert len(rows) == 1, rows
    return rows[0]


# ------------------------------------------------------------------- db probes


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = _connect(db)
    try:
        return [tuple(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def _table(db: Path, table: str) -> list[tuple]:
    return _rows(db, f"SELECT * FROM {table} ORDER BY 1")


def _counts(db: Path, tables: tuple[str, ...]) -> dict[str, int]:
    return {
        table: len(_rows(db, f"SELECT 1 FROM {table}"))
        for table in tables
    }


def _event_ids_for_slot(db: Path, slot_id: str) -> list[tuple]:
    return _rows(
        db,
        "SELECT event_id FROM ce_events WHERE artifact_id=? ORDER BY event_id",
        (slot_id,),
    )


def _slot_row_total(db: Path, slot_id: str) -> int:
    return len(
        _rows(
            db,
            "SELECT event_id FROM ce_events WHERE artifact_id=?"
            " UNION ALL SELECT session_id FROM ce_sessions WHERE artifact_id=?",
            (slot_id, slot_id),
        )
    )


def _slot_event_count(db: Path, slot_id: str) -> int:
    return _rows(
        db, "SELECT COUNT(*) FROM ce_events WHERE artifact_id=?", (slot_id,)
    )[0][0]


def _slot_session_count(db: Path, slot_id: str) -> int:
    return _rows(
        db, "SELECT COUNT(*) FROM ce_sessions WHERE artifact_id=?", (slot_id,)
    )[0][0]


def _rowids(db: Path, table: str, id_column: str) -> dict[str, int]:
    """``{id: rowid}`` — the physical row identity a rowid cursor walks."""

    return {
        str(row[0]): int(row[1])
        for row in _rows(db, f"SELECT {id_column}, rowid FROM {table}")
    }


def _version_rows(db: Path, table: str, id_column: str, row_id: str) -> list[tuple]:
    return _rows(
        db,
        f"SELECT version_seq, superseded_at FROM {table} WHERE {id_column}=? "
        "ORDER BY version_seq",
        (row_id,),
    )


REMOVED_SLOTS_KEY = "removed_slots"


def _removal_records(db: Path) -> list[dict]:
    """The append-only record of deactivated slots (mirror path + when)."""

    rows = _rows(db, "SELECT value FROM ce_live_state WHERE key=?",
                 (REMOVED_SLOTS_KEY,))
    return json.loads(rows[0][0]) if rows else []


def _canonical_session_for_content(db: Path, marker: str) -> str:
    """Identify a projected session by a marker in its projected message text."""

    found = _rows(
        db,
        "SELECT DISTINCT canonical_session_id FROM canonical_messages "
        "WHERE content LIKE ?",
        (f"%{marker}%",),
    )
    assert len(found) == 1, found
    return found[0][0]


def _full_rebuild_projection(
    mirror_root: Path, tmp_path: Path, tag: str, *, family: str = FAMILY
) -> Path:
    """Stage a complete fresh generation and project it (the oracle reference).

    Deliberately the *other* pipeline: ``shadow_conversation_generation``
    (discovery -> capture -> adapt -> one full generation) plus the projection
    seam. It shares only the milestone-1 capture signature with the engine under
    test, so agreement is real evidence and not a tautology.
    """

    ref_db = tmp_path / f"rebuild-{tag}.sqlite"
    report = shadow_conversation_generation(
        source_root=mirror_root,
        db=ref_db,
        artifact_store=tmp_path / f"ref-artifacts-{tag}",
        report_path=tmp_path / f"report-{tag}.json",
    )
    entry = report["generations"][family]
    assert entry["status"] in ("full", "partial"), entry.get("reason")
    projection = build_compatibility_projection(ref_db, entry["generation_id"])
    con = _connect(ref_db)
    try:
        upsert_compatibility_projection(con, projection)
        con.commit()
    finally:
        con.close()
    return ref_db


# ------------------------------------------------------------------ 1. oracle


def test_equality_oracle_incremental_matches_full_rebuild(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"

    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"
    assert first["n_added"] == len(DEFAULT_FILES)
    assert first["n_changed"] == 0 and first["n_removed"] == 0
    assert first["rows_inserted"] > 0 and first["rows_versioned"] == 0
    assert len(_table(db, "canonical_sessions")) == len(DEFAULT_FILES)

    # One file is edited in place: same line count, same native ids, one changed
    # answer body.
    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["n_changed"] == 1
    assert second["n_added"] == 0 and second["n_removed"] == 0

    # The edited text really did land in the live event authority...
    assert _rows(
        db,
        "SELECT event_id FROM ce_events WHERE content LIKE '%(revised)%'",
    )
    # ...and the edit is untouched in the other files.
    assert len(_rows(db, "SELECT 1 FROM ce_events WHERE content LIKE '%revised%'")) == 1

    ref_db = _full_rebuild_projection(mirror, tmp_path, "oracle")
    assert any(
        "(revised)" in (row[6] or "")
        for row in _rows(ref_db, "SELECT * FROM canonical_messages")
    )

    assert _table(db, "canonical_sessions") == _table(ref_db, "canonical_sessions")
    assert _table(db, "canonical_messages") == _table(ref_db, "canonical_messages")
    assert _table(db, "canonical_tool_events") == _table(ref_db, "canonical_tool_events")


def test_oracle_holds_after_a_source_is_removed(tmp_path: Path) -> None:
    """A removal must not corrupt the projection of the surviving sources."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    (mirror / FAMILY / "c.jsonl").unlink()
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_removed"] == 1

    # The rebuild sees the corpus without b.jsonl's sibling c.jsonl, so every row
    # it produces must exist in the live store too; the live store additionally
    # keeps the collected rows of the removed source (the append-only contract).
    ref_db = _full_rebuild_projection(mirror, tmp_path, "oracle-removed")
    live_sessions = set(_table(db, "canonical_sessions"))
    live_messages = set(_table(db, "canonical_messages"))
    assert set(_table(ref_db, "canonical_sessions")) <= live_sessions
    assert set(_table(ref_db, "canonical_messages")) <= live_messages
    assert len(live_sessions) == len(set(_table(ref_db, "canonical_sessions"))) + 1


# ------------------------------------------------------------- 2. idempotency


def test_second_run_without_a_source_change_writes_nothing(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    tables = (
        "ce_events", "ce_sessions", "ce_event_relations", "ce_field_dispositions",
        "ce_source_artifacts", "ce_live_slots", "ce_live_sync_log",
        "ce_event_versions", "ce_session_versions",
        "ce_relation_versions", "ce_disposition_versions",
        "canonical_sessions", "canonical_messages",
    )
    before = _counts(db, tables)
    before_bytes = db.read_bytes()

    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["status"] == "no-op"
    assert second["rows_inserted"] == 0
    assert second["rows_updated"] == 0 and second["rows_versioned"] == 0
    assert second["n_added"] == second["n_changed"] == second["n_removed"] == 0
    assert second["n_unchanged"] == len(DEFAULT_FILES)

    assert _counts(db, tables) == before
    assert db.read_bytes() == before_bytes  # literally zero writes

    third = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert third["status"] == "no-op"
    assert _counts(db, tables) == before


def test_touch_without_a_content_change_is_not_a_rewrite(tmp_path: Path) -> None:
    """A moved mtime with identical bytes must not rewrite the slot's rows."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    row_total = _slot_row_total(db, _slot_id("b.jsonl"))
    (mirror / FAMILY / "b.jsonl").touch()
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_changed"] == 0 and report["n_added"] == 0
    assert report["rows_inserted"] == 0 and report["rows_updated"] == 0
    assert report["rows_versioned"] == 0
    assert row_total == _slot_row_total(db, _slot_id("b.jsonl"))


# ---------------------------------------------------------- 3. one-file edit


def test_one_file_edit_is_a_few_row_write(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_a, slot_b = _slot_id("a.jsonl"), _slot_id("b.jsonl")
    # The recorded slot id IS the deterministic slot identity (family +
    # mirror path) that milestone 1 defined, not a content hash.
    assert _slot_row(db, "a.jsonl")[0] == slot_a
    assert _slot_row(db, "b.jsonl")[0] == slot_b
    slots_before = {
        row[0]: row[1]
        for row in _rows(db, "SELECT slot_id, mirror_path FROM ce_live_slots")
    }
    unrelated_before = _event_ids_for_slot(db, slot_a)
    assert unrelated_before
    file_rows = _slot_row_total(db, slot_b)

    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    # The slot identity is stable across the edit: same slot id, same mirror path
    # watermark, no new slot.
    slots_after = {
        row[0]: row[1]
        for row in _rows(db, "SELECT slot_id, mirror_path FROM ce_live_slots")
    }
    assert slots_after == slots_before
    assert slots_after[slot_b] == _slot_row(db, "b.jsonl")[2]
    assert slots_after[slot_b].endswith("b.jsonl")

    # An unrelated slot's event ids are untouched.
    assert _event_ids_for_slot(db, slot_a) == unrelated_before

    # The write is proportional to the edited file, nowhere near the corpus: one
    # answer body changed, so exactly one event row is refreshed in place and
    # exactly one previous value is appended to the history. Nothing is inserted
    # (every native id survived the edit) and nothing is removed.
    assert report["n_changed"] == 1
    assert report["rows_updated"] == 1
    assert report["rows_versioned"] == 1
    assert report["rows_inserted"] == 0
    assert report["rows_updated"] + report["rows_inserted"] < file_rows
    assert report["rows_updated"] + report["rows_inserted"] < first["rows_inserted"] / 4

    # The edited body is visible both in the event authority and in the
    # compatibility projection (proves the same-id/different-row refresh).
    assert len(_rows(
        db,
        "SELECT event_id FROM ce_events WHERE artifact_id=? AND content LIKE ?",
        (slot_b, "%(revised)%"),
    )) == 1
    session = _canonical_session_for_content(db, "of sess_b")
    assert len(_rows(
        db,
        "SELECT canonical_message_id FROM canonical_messages "
        "WHERE canonical_session_id=? AND content LIKE ?",
        (session, "%(revised)%"),
    )) == 1


# ------------------------------------------------- 4. append-only retention
#
# The chain is a *collection* store: a row already collected is never removed,
# for any reason (the source stops emitting a message, the source file vanishes,
# the slot is deactivated), and a row whose values changed keeps its previous
# values in an append-only history table instead of being overwritten. Every
# expectation below is measured on the database itself (count the rows before,
# count them after) or derived from the text this test wrote — never from the
# engine's own set arithmetic.


def test_stale_event_is_retained_in_ce_events(tmp_path: Path) -> None:
    """A message the source no longer emits stays in the collected store."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    before_ids = {row[0] for row in _event_ids_for_slot(db, slot_b)}
    before_events = _slot_event_count(db, slot_b)
    assert before_ids and before_events == len(before_ids)
    # The last turn is collected, and is about to disappear upstream.
    assert len(_rows(db, "SELECT event_id FROM ce_events WHERE content LIKE ?",
                     ("%answer 4 of sess_b%",))) == 1

    # The source is truncated in place: turns 3 and 4 are gone from the file...
    _write(mirror, "b.jsonl", _codex_session("sess_b", 2))
    assert "answer 4 of sess_b" not in (
        mirror / FAMILY / "b.jsonl"
    ).read_text(encoding="utf-8")

    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_changed"] == 1

    # ...but nothing previously collected disappears from the store.
    assert {row[0] for row in _event_ids_for_slot(db, slot_b)} >= before_ids
    assert _slot_event_count(db, slot_b) >= before_events
    assert len(_rows(db, "SELECT event_id FROM ce_events WHERE content LIKE ?",
                     ("%answer 4 of sess_b%",))) == 1
    assert len(_rows(db, "SELECT canonical_message_id FROM canonical_messages "
                         "WHERE content LIKE ?", ("%answer 4 of sess_b%",))) == 1


# --------------------------------------------- 4b. P0-1 staleness markers
#
# The retention tests above prove the collection contract: nothing is deleted.
# P0-1 adds the other half: a row the source no longer emits is *marked*
# (``stale_at``), so readers can tell collected evidence from current data, and
# the projection stops counting stale events into ``message_count``. The stale
# ``canonical_*`` rows that were already projected stay (the upsert never
# deletes) — that reclaim is the documented P4 boundary.


def _projected_message_count(db: Path, session_b: str) -> int:
    return _rows(
        db,
        "SELECT message_count FROM canonical_sessions "
        "WHERE canonical_session_id=?",
        (session_b,),
    )[0][0]


def test_truncation_marks_lost_events_stale_and_projection_drops_them(
    tmp_path: Path,
) -> None:
    """A truncated file: its lost events are marked, the projection follows."""

    mirror = tmp_path / "mirror"
    db = tmp_path / "live.sqlite"
    _write(mirror, "b.jsonl", _codex_session("sess_b", 3))
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    session_b = _canonical_session_for_content(db, "of sess_b")
    count_before = _projected_message_count(db, session_b)
    assert count_before > 0

    # The source is truncated: turn 3 is gone from the file.
    _write(mirror, "b.jsonl", _codex_session("sess_b", 2))
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_changed"] == 1
    assert report["rows_stale_marked"] > 0

    # Every turn-3 row the source stopped emitting is still collected (the
    # append-only contract) but is now marked stale...
    lost = _rows(
        db,
        "SELECT event_id, kind, stale_at FROM ce_events WHERE artifact_id=? AND "
        "(content LIKE '%question 3 of sess_b%' "
        " OR content LIKE '%answer 3 of sess_b%')",
        (slot_b,),
    )
    assert lost and all(stale_at is not None for _, _, stale_at in lost)
    # ...while the rows the source still emits stay current.
    assert _rows(
        db,
        "SELECT event_id FROM ce_events WHERE artifact_id=? "
        "AND stale_at IS NULL AND content LIKE '%of sess_b%'",
        (slot_b,),
    )

    # And the projection no longer counts the stale events: message_count drops
    # by exactly the stale events that project as messages (measured on the
    # store, not derived from the engine's own arithmetic).
    stale_message_ids = {
        event_id
        for event_id, kind, _ in lost
        if kind in MESSAGE_KINDS
    }
    count_after = _projected_message_count(db, session_b)
    assert count_after == count_before - len(stale_message_ids)
    # The already-projected canonical_messages rows are the P4 boundary: they
    # stay until P4 reclaims them.
    assert len(_rows(
        db,
        "SELECT canonical_message_id FROM canonical_messages "
        "WHERE canonical_session_id=?",
        (session_b,),
    )) >= count_before


def test_restored_turns_clear_the_stale_marker(tmp_path: Path) -> None:
    """A file that regrows re-emits the stale ids: their marker goes back NULL."""

    mirror = tmp_path / "mirror"
    db = tmp_path / "live.sqlite"
    full = _codex_session("sess_b", 3)
    _write(mirror, "b.jsonl", full)
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    session_b = _canonical_session_for_content(db, "of sess_b")
    count_before = _projected_message_count(db, session_b)

    _write(mirror, "b.jsonl", _codex_session("sess_b", 2))
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _rows(
        db,
        "SELECT COUNT(*) FROM ce_events WHERE artifact_id=? AND stale_at IS NOT NULL",
        (slot_b,),
    )[0][0] > 0

    # The file is restored to its exact former content: every stale id is
    # re-emitted unchanged (dirty or unchanged bucket — either way current).
    _write(mirror, "b.jsonl", full)
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["rows_stale_cleared"] > 0

    # Nothing is stale anymore, and nothing was duplicated.
    assert _rows(
        db,
        "SELECT COUNT(*) FROM ce_events WHERE artifact_id=? AND stale_at IS NOT NULL",
        (slot_b,),
    ) == [(0,)]
    assert _rows(
        db,
        "SELECT COUNT(*) FROM ce_sessions WHERE artifact_id=? AND stale_at IS NOT NULL",
        (slot_b,),
    ) == [(0,)]
    # No duplicates were created by the restore: every id was already collected.
    assert report["rows_inserted"] == 0
    # And the projection is back to the pre-truncation count.
    assert _projected_message_count(db, session_b) == count_before


def test_removed_slot_marks_all_its_rows_stale(tmp_path: Path) -> None:
    """A vanished slot: every event and session row it owns is marked stale."""

    mirror = tmp_path / "mirror"
    db = tmp_path / "live.sqlite"
    _build_corpus(mirror)
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    assert _slot_event_count(db, slot_b) > 0
    assert _slot_session_count(db, slot_b) > 0

    (mirror / FAMILY / "b.jsonl").unlink()
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_removed"] == 1

    # Not one row lost, every one of them marked.
    assert _rows(
        db,
        "SELECT COUNT(*) FROM ce_events WHERE artifact_id=? AND stale_at IS NULL",
        (slot_b,),
    ) == [(0,)]
    assert _rows(
        db,
        "SELECT COUNT(*) FROM ce_sessions WHERE artifact_id=? AND stale_at IS NULL",
        (slot_b,),
    ) == [(0,)]
    assert _slot_event_count(db, slot_b) > 0
    assert _slot_session_count(db, slot_b) > 0
    # P4 boundary: the already-projected canonical rows of the removed slot
    # keep their projection (nothing is deleted).
    session_b = _canonical_session_for_content(db, "of sess_b")
    assert _rows(
        db,
        "SELECT canonical_session_id FROM canonical_sessions "
        "WHERE canonical_session_id=?",
        (session_b,),
    )


def test_removed_slot_rows_are_retained_and_still_queryable(
    tmp_path: Path,
) -> None:
    """A vanished source file is a state marker, not a deletion."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    session_b = _canonical_session_for_content(db, "of sess_b")
    events_before = _slot_event_count(db, slot_b)
    sessions_before = _slot_session_count(db, slot_b)
    canonical_sessions_before = len(_table(db, "canonical_sessions"))
    messages_before = len(_rows(
        db, "SELECT canonical_message_id FROM canonical_messages "
            "WHERE canonical_session_id=?", (session_b,)))
    assert events_before and sessions_before and messages_before

    (mirror / FAMILY / "b.jsonl").unlink()
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_removed"] == 1

    # Not one collected row of that slot was lost.
    assert _slot_event_count(db, slot_b) == events_before
    assert _slot_session_count(db, slot_b) == sessions_before

    # The slot itself is retained but marked inactive, and so is its provenance
    # row (ce_source_artifacts is never deleted).
    assert _rows(
        db, "SELECT active FROM ce_live_slots WHERE slot_id=?", (slot_b,)
    ) == [(0,)]
    assert _rows(
        db, "SELECT artifact_id FROM ce_source_artifacts WHERE artifact_id=?",
        (slot_b,),
    )
    # The removal is recorded with its mirror path and when it happened.
    removals = _removal_records(db)
    assert [entry["mirror_path"] for entry in removals] == [
        f"{FAMILY}{os.sep}b.jsonl"
    ]
    assert removals[0]["slot_id"] == slot_b
    assert removals[0]["removed_at"]

    # The session is still reachable through the compatibility projection.
    assert len(_table(db, "canonical_sessions")) == canonical_sessions_before
    assert _rows(
        db,
        "SELECT canonical_session_id FROM canonical_sessions "
        "WHERE canonical_session_id=?",
        (session_b,),
    )
    assert len(_rows(
        db, "SELECT canonical_message_id FROM canonical_messages "
            "WHERE canonical_session_id=?", (session_b,))) == messages_before

    # And the store is still referentially clean.
    assert _rows(db, "PRAGMA foreign_key_check") == []
    assert _rows(db, "SELECT * FROM pragma_integrity_check") == [("ok",)]


def test_dirty_event_keeps_its_old_value_in_ce_event_versions(
    tmp_path: Path,
) -> None:
    """A rewritten body refreshes the row in place and archives the old value."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _rows(db, "SELECT COUNT(*) FROM ce_event_versions") == [(0,)]

    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_changed"] == 1
    assert report["rows_updated"] == 1 and report["rows_versioned"] == 1
    assert report["rows_inserted"] == 0

    # The main row carries the new value...
    revised = _rows(
        db,
        "SELECT event_id, content FROM ce_events WHERE content LIKE ?",
        ("%answer 1 of sess_b (revised)%",),
    )
    assert len(revised) == 1
    event_id = revised[0][0]

    # ...and the previous value is in the history, as version 0 of that event.
    versions = _rows(
        db,
        "SELECT version_seq, content, superseded_at FROM ce_event_versions "
        "WHERE event_id=? ORDER BY version_seq",
        (event_id,),
    )
    assert [row[0] for row in versions] == [0]
    assert versions[0][1] == "answer 1 of sess_b"  # the body written first
    assert versions[0][2]
    assert len(_rows(db, "SELECT 1 FROM ce_events WHERE content=?", ("answer 1 of sess_b",))) == 0

    # A second rewrite appends version 1 (the first revision) and leaves the
    # already-collected version 0 untouched.
    _write(
        mirror,
        "b.jsonl",
        _codex_session("sess_b", 4, revision="revised twice"),
    )
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["rows_updated"] == 1 and second["rows_versioned"] == 1
    assert len(_rows(
        db, "SELECT event_id FROM ce_events WHERE content LIKE ?",
        ("%answer 1 of sess_b (revised twice)%",))) == 1
    versions = _rows(
        db,
        "SELECT version_seq, content FROM ce_event_versions WHERE event_id=? "
        "ORDER BY version_seq",
        (event_id,),
    )
    assert [row[0] for row in versions] == [0, 1]
    assert [row[1] for row in versions] == [
        "answer 1 of sess_b",
        "answer 1 of sess_b (revised)",
    ]


def test_dirty_session_keeps_its_old_value_in_ce_session_versions(
    tmp_path: Path,
) -> None:
    """The session row follows the same archive-then-update rule as events."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _rows(db, "SELECT COUNT(*) FROM ce_session_versions") == [(0,)]

    slot_b = _slot_id("b.jsonl")
    ended_before = _rows(
        db, "SELECT session_id, ended_at FROM ce_sessions WHERE artifact_id=?",
        (slot_b,),
    )
    assert len(ended_before) == 1
    session_id, old_ended_at = ended_before[0]

    # A turn is appended: the session row's end timestamp moves.
    _write(mirror, "b.jsonl", _codex_session("sess_b", 5))
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_changed"] == 1
    assert report["rows_updated"] == 1 and report["rows_versioned"] == 1

    new_ended_at = _rows(
        db, "SELECT ended_at FROM ce_sessions WHERE session_id=?", (session_id,)
    )[0][0]
    assert new_ended_at != old_ended_at
    versions = _rows(
        db,
        "SELECT version_seq, ended_at, superseded_at FROM ce_session_versions "
        "WHERE session_id=? ORDER BY version_seq",
        (session_id,),
    )
    assert [row[0] for row in versions] == [0]
    assert versions[0][1] == old_ended_at
    assert versions[0][2]


def test_second_run_keeps_canonical_rowids_stable(tmp_path: Path) -> None:
    """The projection inserts missing rows and refreshes changed ones in place.

    ``retrieval/conversation_fts.py`` (:28-33) uses ``canonical_messages.rowid``
    as its incremental cursor and relies on the authority table being append-only
    with monotonic rowids. A DELETE + re-insert (or ``INSERT OR REPLACE``) would
    move the rowids of rows a reader has already consumed, so the rowids are
    measured directly here, before and after.
    """

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    messages_before = _rowids(db, "canonical_messages", "canonical_message_id")
    sessions_before = _rowids(db, "canonical_sessions", "canonical_session_id")
    tools_before = _rowids(db, "canonical_tool_events", "canonical_tool_id")
    assert messages_before and sessions_before

    # A repeat run over unchanged sources writes nothing at all.
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _rowids(db, "canonical_messages", "canonical_message_id") == messages_before
    assert _rowids(db, "canonical_sessions", "canonical_session_id") == sessions_before

    # An edit that keeps every native id and rewrites one body (a dirty row) must
    # refresh in place: same rows, same rowids, new text.
    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _rowids(db, "canonical_messages", "canonical_message_id") == messages_before
    assert _rowids(db, "canonical_sessions", "canonical_session_id") == sessions_before
    assert _rowids(db, "canonical_tool_events", "canonical_tool_id") == tools_before
    assert report["rows_updated"] >= 1
    # One projected message row changed and it was refreshed, not rewritten.
    assert report["projection"]["inserted"] == 0
    assert report["projection"]["updated"] == 1
    assert len(_rows(
        db, "SELECT canonical_message_id FROM canonical_messages "
            "WHERE content LIKE ?", ("%answer 1 of sess_b (revised)%",))) == 1


# -------------------------------------------------------------- 5. removal


def test_removed_source_returning_reactivates_without_duplicates(
    tmp_path: Path,
) -> None:
    """A re-appearing file reuses its slot id (inactive -> active), no dups."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    body = (mirror / FAMILY / "b.jsonl").read_text(encoding="utf-8")
    events_before = _slot_event_count(db, slot_b)
    session_b = _canonical_session_for_content(db, "of sess_b")
    messages_before = len(_rows(
        db, "SELECT canonical_message_id FROM canonical_messages "
            "WHERE canonical_session_id=?", (session_b,)))

    (mirror / FAMILY / "b.jsonl").unlink()
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _slot_event_count(db, slot_b) == events_before  # nothing removed

    _write(mirror, "b.jsonl", body)
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_added"] == 1
    assert report["rows_inserted"] == 0  # every row was already collected
    assert _rows(
        db, "SELECT active FROM ce_live_slots WHERE slot_id=?", (slot_b,)
    ) == [(1,)]
    # Same slot, same rows, no second copy of anything.
    assert _slot_event_count(db, slot_b) == events_before
    assert _slot_session_count(db, slot_b) == 1
    assert len(_rows(
        db, "SELECT canonical_message_id FROM canonical_messages "
            "WHERE canonical_session_id=?", (session_b,))) == messages_before
    assert len(_table(db, "canonical_sessions")) == len(DEFAULT_FILES)


# --------------------------------------------------------------- 6. dry run


def test_dry_run_reports_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    store = tmp_path / "artifacts"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    _write(mirror, "e.jsonl", _codex_session("sess_e", 4))

    tables = (
        "ce_events", "ce_sessions", "ce_source_artifacts", "ce_live_slots",
        "ce_live_sync_log", "ce_event_versions", "ce_session_versions",
        "canonical_sessions", "canonical_messages",
    )
    before_counts = _counts(db, tables)
    before_bytes = db.read_bytes()
    before_blobs = sorted(p.name for p in (store / "artifacts").glob("*"))

    plan = live_sync_once(
        db=db, mirror_root=mirror, generation_id=GENERATION, dry_run=True
    )

    assert plan["status"] == "dry-run"
    assert plan["n_changed"] == 1
    assert plan["n_added"] == 1
    assert plan["n_removed"] == 0
    assert plan["n_unchanged"] == len(DEFAULT_FILES) - 1  # a, c, d; b and e moved
    assert plan["rows_inserted"] == 0
    assert plan["rows_updated"] == 0 and plan["rows_versioned"] == 0
    assert plan["per_family"][FAMILY]["changed"] == 1

    assert _counts(db, tables) == before_counts
    assert db.read_bytes() == before_bytes
    assert sorted(p.name for p in (store / "artifacts").glob("*")) == before_blobs


def test_dry_run_on_a_missing_database_writes_nothing(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "never-created.sqlite"

    plan = live_sync_once(
        db=db, mirror_root=mirror, generation_id=GENERATION, dry_run=True
    )
    assert plan["status"] == "dry-run"
    assert not db.exists()


def test_dry_run_plan_lists_the_new_slot(tmp_path: Path) -> None:
    """A dry run against an existing db reports the exact slot plan."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    _write(mirror, "e.jsonl", _codex_session("sess_e", 2))

    plan = live_sync_once(
        db=db, mirror_root=mirror, generation_id=GENERATION, dry_run=True
    )
    assert plan["n_added"] == 1
    assert [entry["mirror_path"] for entry in plan["slots"]] == [
        f"{FAMILY}{os.sep}e.jsonl"
    ]
    assert plan["per_family"] == {
        FAMILY: {
            "added": 1, "changed": 0, "removed": 0,
            "rows_inserted": 0, "rows_updated": 0, "rows_versioned": 0,
        }
    }


# ------------------------------------------------- 7. session-scope query plans
#
# The two projection tables are looked up by ``canonical_session_id`` (reads in
# ``core.conversation_repository``, and the activation/rollback path's
# ``clear_compatibility_projection``), but their DDL declared no index on that
# column, so every session paid one full-table SCAN (measured 44.8 ms messages /
# 36.1 ms tool events on the 8 GB staging db). These tests pin the fix at the
# seam that creates the tables (``compatibility_projection._ensure_tables``):
# the session column must be index-backed, and the plans of the real query shapes
# must stop scanning.

_SESSION_INDEXES = {
    "canonical_messages": "ix_canonical_messages_session",
    "canonical_tool_events": "ix_canonical_tool_events_session",
}


def _plan_text(con: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    return " | ".join(
        str(row[3])
        for row in con.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


def test_projection_session_lookups_are_index_backed(tmp_path: Path) -> None:
    db = tmp_path / "projected.sqlite"
    con = _connect(db)
    try:
        # Public schema seam: clearing a fresh projection creates the tables.
        clear_compatibility_projection(con)
        con.commit()

        for table, index_name in _SESSION_INDEXES.items():
            names = {row[1] for row in con.execute(f"PRAGMA index_list({table})")}
            assert index_name in names, (table, sorted(names))
            key_columns = [
                row[2] for row in con.execute(f"PRAGMA index_info({index_name})")
            ]
            assert key_columns[:1] == ["canonical_session_id"], key_columns

            # The read shape used by consumers (``conversation_repository``).
            select = f"SELECT 1 FROM {table} WHERE canonical_session_id=?"
            select_plan = _plan_text(con, select, ("probe-session",))
            assert "SCAN" not in select_plan, (table, select_plan)
            assert "SEARCH" in select_plan, (table, select_plan)
            assert index_name in select_plan, (table, select_plan)

            # The per-session delete shape used by the rollback owner
            # (``clear_compatibility_projection``); the live path no longer
            # deletes projection rows at all.
            delete = (
                f"DELETE FROM {table} WHERE canonical_session_id IN (?,?,?)"
            )
            delete_plan = _plan_text(con, delete, ("a", "b", "c"))
            assert "SCAN" not in delete_plan, (table, delete_plan)
            assert "SEARCH" in delete_plan, (table, delete_plan)
            assert index_name in delete_plan, (table, delete_plan)
    finally:
        con.close()


# --------------------------------- 8. slot-relation lookup query shape
#
# ``live_sync._slot_relation_ids`` answers "which relations have BOTH endpoints
# inside this slot" and is called once per slot by ``_apply_slot`` (to report how
# many relations that file newly contributed). Written as a two-JOIN form
# (``ce_events`` joined twice), the
# planner could only constrain the relation table by ``generation_id``
# (``ix_ce_rel_gen_target (generation_id=?)``), so every slot enumerated ALL
# relation rows of the generation and probed ``ce_events`` twice per row:
# measured 6.7-9.7 s per slot on the 8 GB staging db — including for an empty
# slot — which was ~88% of a live run's wall time. The fix drives the relation
# scan with the slot's own event ids through ``ce_relations_generation_source``
# and settles the far endpoint against that same id set.

_SLOT_GEN = "gen-slot-rel"
_SLOT_DECOY_GEN = "gen-slot-rel-decoy"
_SLOT_A = "slot-art-a"
_SLOT_B = "slot-art-b"


def _slot_relation_fixture(db: Path) -> None:
    """Two slots in one generation, a cross-slot relation and a decoy generation."""

    create_v2_schema(db)
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executemany(
            "INSERT INTO ce_source_artifacts (artifact_id, family, source_kind,"
            " content_hash, capture_method, relative_path, byte_size)"
            " VALUES (?,?,?,?,?,?,?)",
            [(slot, FAMILY, "file", "h", "mirror-copy", f"{FAMILY}/{slot}", 1)
             for slot in (_SLOT_A, _SLOT_B)],
        )
        con.executemany(
            "INSERT INTO ce_event_generations (generation_id, status, created_at)"
            " VALUES (?,?,?)",
            [(gen, "staged", "2026-07-01T00:00:00Z")
             for gen in (_SLOT_GEN, _SLOT_DECOY_GEN)],
        )
        sessions: list[tuple] = []
        events: list[tuple] = []
        for gen in (_SLOT_GEN, _SLOT_DECOY_GEN):
            for slot, ids in (
                (_SLOT_A, ("ev-a1", "ev-a2", "ev-a3")),
                (_SLOT_B, ("ev-b1", "ev-b2")),
            ):
                session = f"sess-{gen}-{slot}"
                sessions.append(
                    (gen, session, FAMILY, "nat", None, None, slot,
                     f"loc-{session}", "v1", "{}")
                )
                events += [
                    (gen, eid, session, "message", slot, f"{session}/{eid}", None,
                     "2026-07-01T00:00:00Z", ordinal, None, None, None, "v1", "{}")
                    for ordinal, eid in enumerate(ids)
                ]
        con.executemany(
            "INSERT INTO ce_sessions (generation_id, session_id, family,"
            " native_session_id, started_at, ended_at, artifact_id,"
            " native_locator, contract_version, fidelity_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", sessions)
        con.executemany(
            "INSERT INTO ce_events (generation_id, event_id, session_id, kind,"
            " artifact_id, native_locator, native_event_id, occurred_at, ordinal,"
            " native_payload_ref, content, summary, contract_version, fidelity_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", events)
        con.executemany(
            "INSERT INTO ce_event_relations (generation_id, relation_id,"
            " source_event_id, target_event_id, relation_kind) VALUES (?,?,?,?,?)",
            [
                (_SLOT_GEN, "rel-a-1", "ev-a1", "ev-a2", "k"),      # inside A
                (_SLOT_GEN, "rel-a-2", "ev-a2", "ev-a3", "k"),      # inside A
                (_SLOT_GEN, "rel-cross", "ev-a1", "ev-b1", "k"),    # A -> B
                (_SLOT_GEN, "rel-b-1", "ev-b1", "ev-b2", "k"),      # inside B
                (_SLOT_DECOY_GEN, "rel-decoy", "ev-a1", "ev-a2", "k"),
            ],
        )
        con.commit()
    finally:
        con.close()


def _slot_relation_ids_reference(
    con: sqlite3.Connection, generation_id: str, slot_id: str
) -> set[str]:
    """The pre-optimisation two-JOIN form, kept as the equivalence oracle."""

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


def test_slot_relation_lookup_is_equivalent_and_index_backed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "slot-relations.sqlite"
    _slot_relation_fixture(db)
    con = _connect(db)
    try:
        traced: list[str] = []
        con.set_trace_callback(traced.append)
        found = _slot_relation_ids(con, _SLOT_GEN, _SLOT_A)
        con.set_trace_callback(None)

        # (a) Independent expectation. Only the two relations fully inside slot A
        # qualify: the A->B relation and the decoy generation must not leak in.
        assert found == {"rel-a-1", "rel-a-2"}, sorted(found)
        assert _slot_relation_ids_reference(con, _SLOT_GEN, _SLOT_A) == {
            "rel-a-1", "rel-a-2"
        }

        # (b) Equivalence with the pre-optimisation query, both slots.
        assert found == _slot_relation_ids_reference(con, _SLOT_GEN, _SLOT_A)
        assert _slot_relation_ids(con, _SLOT_GEN, _SLOT_B) == {"rel-b-1"}
        # An empty slot answers the empty set (the old form still paid a full
        # generation scan here).
        assert _slot_relation_ids(con, _SLOT_GEN, "slot-art-absent") == set()

        # (c) Semantics: every returned relation has BOTH endpoints in the slot.
        endpoint_rows = _rows(
            db,
            "SELECT r.relation_id, a.artifact_id, b.artifact_id"
            " FROM ce_event_relations r"
            " JOIN ce_events a ON a.generation_id=r.generation_id"
            "   AND a.event_id=r.source_event_id"
            " JOIN ce_events b ON b.generation_id=r.generation_id"
            "   AND b.event_id=r.target_event_id"
            " WHERE r.generation_id=?",
            (_SLOT_GEN,),
        )
        inside = {
            rel
            for rel, src_slot, tgt_slot in endpoint_rows
            if (src_slot, tgt_slot) == (_SLOT_A, _SLOT_A)
        }
        assert inside == found, (sorted(inside), sorted(found))
        assert "rel-cross" in {rel for rel, *_ in endpoint_rows}
        assert "rel-cross" not in found

        # (d) Plan. The relation scan must be reached through the slot's own
        # event ids, and no step may enumerate the generation's relations on
        # ``generation_id`` alone.
        selects = [
            sql for sql in traced if sql.lstrip().upper().startswith("SELECT")
        ]
        assert selects, traced
        plans = [_plan_text(con, sql) for sql in selects]
        joined = " | ".join(plans)
        assert (
            "ce_relations_generation_source"
            " (generation_id=? AND source_event_id=?)" in joined
        ), joined
        for plan in plans:
            assert "SCAN ce_event_relations" not in plan, plan
            assert "ix_ce_rel_gen_target (generation_id=?)" not in plan, plan
            assert "ce_relations_generation_source (generation_id=?)" not in plan, plan
    finally:
        con.close()


# ----------------------------- 9. relation / disposition row history
#
# The same contract as events and sessions, for the two derived row families:
# a relation and a disposition are identified by values that survive an edit
# (``relation_id``; ``(event_id, field_name)``), so "the same identity now holds
# a different value" has to be *archived* before the main row is refreshed in
# place. ``INSERT OR IGNORE`` alone silently keeps the first captured value, so
# every expectation below is measured on the database (which value the main row
# holds, which values the history holds, how the version sequence grows) or on
# a second, independent pipeline (the full rebuild), never on the engine's own
# arithmetic.

CLAUDE_FILE = "s.jsonl"


def _relation_state(db: Path) -> list[tuple]:
    return _rows(
        db,
        "SELECT relation_id, source_event_id, target_event_id, relation_kind "
        "FROM ce_event_relations ORDER BY relation_id",
    )


def _relation_versions(db: Path, relation_id: str) -> list[tuple]:
    return _rows(
        db,
        "SELECT version_seq, relation_kind, source_event_id, target_event_id, "
        "superseded_at FROM ce_relation_versions WHERE relation_id=? "
        "ORDER BY version_seq",
        (relation_id,),
    )


def _disposition_state(db: Path, event_id: str, field_name: str) -> list[tuple]:
    return _rows(
        db,
        "SELECT disposition, reason FROM ce_field_dispositions "
        "WHERE event_id=? AND field_name=?",
        (event_id, field_name),
    )


def _disposition_versions(
    db: Path, event_id: str, field_name: str
) -> list[tuple]:
    return _rows(
        db,
        "SELECT version_seq, disposition, reason, superseded_at "
        "FROM ce_disposition_versions WHERE event_id=? AND field_name=? "
        "ORDER BY version_seq",
        (event_id, field_name),
    )


def test_dirty_relation_keeps_its_old_value_in_ce_relation_versions(
    tmp_path: Path,
) -> None:
    """A relation whose kind changes refreshes in place, old kind archived."""

    mirror = tmp_path / "mirror"
    _write_claude(mirror, CLAUDE_FILE, _claude_session("sess_s"))
    db = tmp_path / "live.sqlite"
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"
    assert _table(db, "ce_relation_versions") == []

    before = _relation_state(db)
    assert len(before) == 2  # one relation per assistant content block
    assert {row[3] for row in before} == {"parent_child"}

    # One flag flips. The link (and its relation id, derived from the two
    # endpoints) is untouched; only the kind of that same link changes.
    _write_claude(
        mirror, CLAUDE_FILE, _claude_session("sess_s", sidechain=True)
    )
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_changed"] == 1
    assert report["rows_inserted"] == 0
    assert report["rows_updated"] == 2 and report["rows_versioned"] == 2
    # ...and no *event* row changed: this edit is about the derived row only.
    assert _table(db, "ce_event_versions") == []

    # The main rows now carry the new kind, under the same ids and endpoints.
    after = _relation_state(db)
    assert [row[0] for row in after] == [row[0] for row in before]
    assert [(row[1], row[2]) for row in after] == [
        (row[1], row[2]) for row in before
    ]
    assert {row[3] for row in after} == {"sidechain"}
    assert _rows(
        db, "SELECT 1 FROM ce_event_relations WHERE relation_kind='parent_child'"
    ) == []

    # The replaced kind is version 0 of that very relation id.
    for relation_id, source_event_id, target_event_id, _kind in before:
        versions = _relation_versions(db, relation_id)
        assert [row[0] for row in versions] == [0]
        assert versions[0][1] == "parent_child"
        assert (versions[0][2], versions[0][3]) == (
            source_event_id, target_event_id
        )
        assert versions[0][4]

    # A second flip archives version 1 and leaves version 0 exactly as it was.
    _write_claude(mirror, CLAUDE_FILE, _claude_session("sess_s"))
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["rows_inserted"] == 0
    assert {row[3] for row in _relation_state(db)} == {"parent_child"}
    for relation_id, *_rest in before:
        versions = _relation_versions(db, relation_id)
        assert [row[0] for row in versions] == [0, 1]
        assert [row[1] for row in versions] == ["parent_child", "sidechain"]

    assert _rows(db, "PRAGMA foreign_key_check") == []
    assert _rows(db, "SELECT * FROM pragma_integrity_check") == [("ok",)]


def test_dirty_disposition_keeps_its_old_value_in_ce_disposition_versions(
    tmp_path: Path,
) -> None:
    """A re-decided disposition refreshes in place, old verdict archived."""

    mirror = tmp_path / "mirror"
    _write_claude(mirror, CLAUDE_FILE, _claude_session("sess_s"))
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _table(db, "ce_disposition_versions") == []

    field = "reasoning_content"
    stored = _rows(
        db,
        "SELECT event_id, disposition, reason FROM ce_field_dispositions "
        "WHERE field_name=?",
        (field,),
    )
    assert len(stored) == 1
    event_id, old_value, old_reason = stored[0]
    assert old_value == "unavailable"
    assert old_reason

    # The thinking block gains plaintext: the event id is built from the locator
    # and the native block id, so it survives the edit; the verdict for that same
    # (event, field) is re-decided.
    _write_claude(
        mirror, CLAUDE_FILE, _claude_session("sess_s", thinking="why sess_s")
    )
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["rows_inserted"] == 0
    # One dirty event (the reasoning body) and one dirty disposition.
    assert report["rows_updated"] == 2 and report["rows_versioned"] == 2

    assert _disposition_state(db, event_id, field) == [
        ("mapped", "reasoning text mapped exactly to content")
    ]
    versions = _disposition_versions(db, event_id, field)
    assert [row[0] for row in versions] == [0]
    assert versions[0][1] == "unavailable"
    assert versions[0][2] == old_reason
    assert versions[0][3]

    # Dropping the plaintext again re-decides it back: version 1 records the
    # verdict that was current, version 0 stays as collected.
    _write_claude(mirror, CLAUDE_FILE, _claude_session("sess_s"))
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["rows_inserted"] == 0
    assert [row[0] for row in _disposition_state(db, event_id, field)] == [
        "unavailable"
    ]
    versions = _disposition_versions(db, event_id, field)
    assert [row[0] for row in versions] == [0, 1]
    assert [row[1] for row in versions] == ["unavailable", "mapped"]


def test_second_edit_appends_the_next_version_seq_for_both_derived_tables(
    tmp_path: Path,
) -> None:
    """The version sequence continues past version 0; no archived row is touched."""

    mirror = tmp_path / "mirror"
    _write_claude(
        mirror, CLAUDE_FILE, _claude_session("sess_s", sidechain=True)
    )
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    field = "reasoning_content"
    (event_id, _value, _reason) = _rows(
        db,
        "SELECT event_id, disposition, reason FROM ce_field_dispositions "
        "WHERE field_name=?",
        (field,),
    )[0]
    relation_ids = [row[0] for row in _relation_state(db)]
    assert len(relation_ids) == 2

    # First edit: both derived rows change value under their own identity.
    _write_claude(
        mirror,
        CLAUDE_FILE,
        _claude_session("sess_s", sidechain=False, thinking="first verdict"),
    )
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["rows_versioned"] == 4  # 2 relations + 1 disposition + 1 event

    version_zero = {
        "relations": {
            relation_id: _relation_versions(db, relation_id)
            for relation_id in relation_ids
        },
        "disposition": _disposition_versions(db, event_id, field),
    }
    assert [row[0] for row in version_zero["disposition"]] == [0]
    for relation_id in relation_ids:
        assert [row[0] for row in version_zero["relations"][relation_id]] == [0]

    # Second edit: both identities change value again, in the other direction.
    _write_claude(
        mirror, CLAUDE_FILE, _claude_session("sess_s", sidechain=True)
    )
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["rows_inserted"] == 0
    assert second["rows_versioned"] == 4

    for relation_id in relation_ids:
        versions = _relation_versions(db, relation_id)
        assert [row[0] for row in versions] == [0, 1]
        assert [row[1] for row in versions] == ["sidechain", "parent_child"]
        # Version 0 is byte-for-byte what it was before the second edit.
        assert versions[:1] == version_zero["relations"][relation_id]
        assert len(versions) == len(set(versions)) == 2

    versions = _disposition_versions(db, event_id, field)
    assert [row[0] for row in versions] == [0, 1]
    assert [row[1] for row in versions] == ["unavailable", "mapped"]
    assert versions[:1] == version_zero["disposition"]
    assert _rows(db, "SELECT * FROM pragma_integrity_check") == [("ok",)]


def test_adaptation_level_disposition_is_keyed_like_the_full_rebuild(
    tmp_path: Path,
) -> None:
    """The third write route (adaptation-level dispositions) is keyed identically.

    ``ce_field_dispositions`` has two write routes: per-event dispositions and
    the adaptation-level ones, which carry no ``event_id`` and are attached to
    the generation's first event. The engine has to read a slot's stored rows
    back under exactly the key that route writes them with, so this compares the
    live store against a full rebuild of the same file (the independent pipeline)
    row for row, then shows that a source which stops declaring the field leaves
    the collected row in place instead of archiving over it.
    """

    mirror = tmp_path / "mirror"
    _write_gemini(mirror, "g.json", _gemini_doc(usage=True))
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    live_rows = _rows(
        db,
        "SELECT event_id, field_name, disposition, reason "
        "FROM ce_field_dispositions ORDER BY event_id, field_name",
    )
    assert live_rows and {row[1] for row in live_rows} == {"messages[*].usage"}

    ref_db = _full_rebuild_projection(
        mirror, tmp_path, "adaptation-disposition", family=GEMINI
    )
    assert _rows(
        ref_db,
        "SELECT event_id, field_name, disposition, reason "
        "FROM ce_field_dispositions ORDER BY event_id, field_name",
    ) == live_rows

    # The document stops carrying the token fields: the declared disposition is
    # gone from the source, so nothing is replaced and nothing is archived.
    _write_gemini(mirror, "g.json", _gemini_doc(usage=False))
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_changed"] == 1
    assert report["rows_inserted"] == 0
    assert report["rows_updated"] == 0 and report["rows_versioned"] == 0
    assert _table(db, "ce_disposition_versions") == []
    assert _rows(
        db,
        "SELECT event_id, field_name, disposition, reason "
        "FROM ce_field_dispositions ORDER BY event_id, field_name",
    ) == live_rows


def test_stale_relation_and_disposition_rows_are_retained(
    tmp_path: Path,
) -> None:
    """A source that stops emitting them is not a reason to drop collected rows."""

    mirror = tmp_path / "mirror"
    _write_claude(
        mirror, CLAUDE_FILE, _claude_session("sess_s", sidechain=True)
    )
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    relations_before = _relation_state(db)
    dispositions_before = _rows(
        db,
        "SELECT event_id, field_name, disposition, reason "
        "FROM ce_field_dispositions ORDER BY event_id, field_name",
    )
    assert len(relations_before) == 2 and dispositions_before
    disp_versions_before = _table(db, "ce_disposition_versions")
    rel_versions_before = _table(db, "ce_relation_versions")

    # The source is rewritten without the linked assistant record: the parent
    # link and the reasoning block are gone from the file...
    truncated = _claude_user_only("sess_s")
    assert '"parentUuid": null' in truncated and '"thinking"' not in truncated
    _write_claude(mirror, CLAUDE_FILE, truncated)
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_changed"] == 1
    assert report["rows_inserted"] == 0

    # ...and a fresh full rebuild of the same corpus confirms that: the rebuild
    # (a second, independent pipeline) produces no derived rows at all.
    ref_db = _full_rebuild_projection(
        mirror, tmp_path, "stale-derived", family=CLAUDE
    )
    assert _table(ref_db, "ce_event_relations") == []
    assert _table(ref_db, "ce_field_dispositions") == []
    assert _rows(
        ref_db, "SELECT 1 FROM ce_events WHERE content LIKE ?", ("%answer of%",)
    ) == []

    # Nothing was dropped and nothing was archived over: a stale row is not a
    # replaced row, so the history tables stay exactly as they were.
    assert _relation_state(db) == relations_before
    assert _rows(
        db,
        "SELECT event_id, field_name, disposition, reason "
        "FROM ce_field_dispositions ORDER BY event_id, field_name",
    ) == dispositions_before
    assert _table(db, "ce_relation_versions") == rel_versions_before
    assert _table(db, "ce_disposition_versions") == disp_versions_before
    stale_event_id, stale_field = dispositions_before[0][0], dispositions_before[0][1]
    assert _disposition_state(db, stale_event_id, stale_field) == [
        (dispositions_before[0][2], dispositions_before[0][3])
    ]

    # The collected rows are still reachable through the derived-table index the
    # engine reads them with, and the store is still referentially clean.
    con = _connect(db)
    try:
        assert _slot_relation_ids(
            con, GENERATION, _claude_slot(CLAUDE_FILE)
        ) == {row[0] for row in relations_before}
    finally:
        con.close()
    assert _rows(db, "PRAGMA foreign_key_check") == []


# ------------------- 10. 变更槽位：先插入新事件，再刷新引用它们的派生行
#
# 真机事故（2026-09-25）：authority 的第一次增量 apply 直接抛
# ``IntegrityError: FOREIGN KEY constraint failed``，整轮回滚（回滚本身是干净的：
# integrity ok、``PRAGMA foreign_key_check`` 0 行、槽数与事件数分毫未动）。原因
# 是 ``_apply_slot`` 的语句顺序 —— 它先把脏派生行 UPDATE 成新值，之后才 INSERT
# 本文件新产出的事件行，而 ``ce_event_relations`` 的两个端点都被 FK 约束到
# ``ce_events``。
#
# 「关系身份不变、端点指向新事件」是可达的：claude 的 call/result 关系 id 取自
# 原生 ``call_id``（与行号无关），而事件 id 含 locator 里的行号。所以文件顶部插
# 一行，就足以让这条关系保持同一个身份、两个端点全部搬家 —— 此时按原顺序 UPDATE
# 会指向尚不存在的事件行。
#
# 断言落在「刷新后两个端点确实存在于事件表」上，而不是靠「没抛异常」。


def _claude_tool_pair() -> str:
    """一条 user + 一条 assistant：后者带同一 ``call_id`` 的 tool_use/tool_result。"""

    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "s-pair",
            "timestamp": "2026-07-01T10:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "q"}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "s-pair",
            "timestamp": "2026-07-01T10:00:01Z",
            "stop_reason": "tool_use",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call-1", "name": "t",
                     "input": {"command": "ls"}},
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
                ],
            },
        },
    ]
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _claude_tool_pair_shifted() -> str:
    """同样的记录，但顶部多一行：其后每条 locator（``#L<行号>``）整体后移。"""

    leading = {
        "type": "runtime-config",
        "uuid": "rc",
        "parentUuid": None,
        "sessionId": "s-pair",
        "model": "m",
    }
    return json.dumps(leading) + "\n" + _claude_tool_pair()


def test_changed_slot_refreshes_a_relation_whose_endpoints_moved(
    tmp_path: Path,
) -> None:
    """关系 id 不变、端点全搬家的增量必须落地，而不是撞 FK 回滚。"""

    mirror = tmp_path / "mirror"
    _write_claude(mirror, CLAUDE_FILE, _claude_tool_pair())
    db = tmp_path / "live.sqlite"
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"

    pairs = [row for row in _relation_state(db) if row[3] == "call_result"]
    assert len(pairs) == 1
    relation_id, old_source, old_target = pairs[0][0], pairs[0][1], pairs[0][2]

    _write_claude(mirror, CLAUDE_FILE, _claude_tool_pair_shifted())
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["status"] == "ok"

    # 同一身份、不同值：端点真的搬家了（否则这条测试什么也没验证）。
    state = {row[0]: row for row in _relation_state(db)}
    assert state[relation_id][1] != old_source
    assert state[relation_id][2] != old_target
    # 旧端点按「同身份不同值」的契约进了历史表，没有被覆盖掉。
    assert _relation_versions(db, relation_id)[0][2:4] == (old_source, old_target)
    # FK 的实质：刷新后的两个端点确实在事件表里。
    stored = {row[0] for row in _rows(db, "SELECT event_id FROM ce_events")}
    assert {state[relation_id][1], state[relation_id][2]} <= stored
    assert _rows(db, "PRAGMA foreign_key_check") == []


# ------------------- 11. P1-5：跨 slot 同 relation_id 的吞没与端点择优
#
# 同一原生会话被两个 slot 采集（不同镜像路径 → 不同 slot id）时，claude 的
# call/result 关系 id 源于原生 call_id（跨 slot 相同），而端点事件 id 携带
# 每份副本自己的 locator（跨 slot 不同）。第二个 slot 把这条关系归入 new 桶，
# 但 ``_insert_relations`` 的 ``INSERT OR IGNORE`` 静默无操作：库里已有的那条
# 关系锚在先到副本的端点上，第二副本的端点无痕丢失，``rows_inserted`` 虚增，
# 关系边跟随哪个副本完全由 apply 先后决定。
#
# 最小诚实修复的可观察行为：吞没数进 apply report；且仅当「来者端点全部现行、
# 在库锚点全部 stale」时才把关系边迁到现行副本（替换值先进历史表）。测试用
# 真 adapter 产出全部行，仅用一条 seed 复现「库里已持有 B 将派生的关系 id、
# 锚在 A 的端点上」这一被审计确认的库存状态 —— 引擎看到的碰撞是真实的。


def _claude_tool_pair_user_only() -> str:
    """工具对文件截断后的形态：只留首条 user 记录（行号与事件 id 均不变）。"""

    return json.dumps(
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "s-pair",
            "timestamp": "2026-07-01T10:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "q"}]},
        }
    ) + "\n"


def _seed_call_result_relation(db: Path, target_slot: str) -> str:
    """把在库的 call/result 关系改键成 ``target_slot`` 将派生的那个 id。

    关系 id 源于原生 call_id（与 slot 无关），所以在 P1-5 的前提「同一原生
    会话被两个 slot 采集」下，两个 slot 派生的是**同一个** relation id，库
    里只有一行 —— 锚在先到副本（slot A）的端点上。本函数把在库那行
    ``call_result`` 关系的 relation_id 换成第二副本将派生的那个
    （``rel-call:call-1:0``，与 ``_claude_tool_pair`` 的 call_id 和配对序号
    一致，由 adapter 同一函数派生），端点不动，得到的就是第二个 slot apply
    时引擎看到的真实库存状态。（表上有
    ``UNIQUE (generation_id, source_event_id, target_event_id, relation_kind)``，
    同端点同 kind 不能并存两行，故用改键而不是追加。）
    """

    relation_id = make_event_id(
        CLAUDE, target_slot, CLAUDE_CONTRACT_VERSION, "rel-call:call-1:0"
    )
    con = _connect(db)
    try:
        con.execute(
            "UPDATE ce_event_relations SET relation_id=? "
            "WHERE generation_id=? AND relation_kind='call_result'",
            (relation_id, GENERATION),
        )
        con.commit()
    finally:
        con.close()
    assert _relation_endpoints(db, relation_id), "seed must pin the relation id"
    return relation_id


def _relation_endpoints(db: Path, relation_id: str) -> tuple[str, str]:
    return _rows(
        db,
        "SELECT source_event_id, target_event_id FROM ce_event_relations "
        "WHERE relation_id=?",
        (relation_id,),
    )[0][:2]


def test_swallowed_cross_slot_relation_is_counted_and_keeps_the_current_anchor(
    tmp_path: Path,
) -> None:
    """(a) 先 apply A 再 apply B：吞没计数 +1，A 现行时关系边仍锚在 A。"""

    mirror = tmp_path / "mirror"
    _write_claude(mirror, CLAUDE_FILE, _claude_tool_pair())
    db = tmp_path / "live.sqlite"
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"
    assert first["relations_ignored_duplicates"] == 0

    slot_b = _claude_slot("t.jsonl")
    relation_id = _seed_call_result_relation(db, slot_b)
    anchor_a = _relation_endpoints(db, relation_id)
    slot_a_events = {
        row[0]
        for row in _rows(
            db, "SELECT event_id FROM ce_events WHERE artifact_id=?",
            (_claude_slot(CLAUDE_FILE),),
        )
    }
    assert set(anchor_a) <= slot_a_events

    # 第二份镜像：同一原生会话（字节相同），不同 mirror path → 不同 slot。
    relations_before = len(_table(db, "ce_event_relations"))
    _write_claude(mirror, "t.jsonl", _claude_tool_pair())
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["status"] == "ok"
    assert report["n_added"] == 1
    assert report["relations_ignored_duplicates"] == 1
    assert report["relations_endpoint_refreshed"] == 0
    assert report["per_family"][CLAUDE]["relations_ignored_duplicates"] == 1

    # A 现行 → 边不动：仍然锚在先到副本的端点上。
    assert _relation_endpoints(db, relation_id) == anchor_a

    # 计数是诚实的：被吞掉的那条没有计入 rows_inserted。B 的实际落库行 =
    # 自己的 session + events + dispositions + 真正新插入的关系（库内关系行
    # 差值），与 report 的 rows_inserted 必须严格相等。
    b_events = [
        row[0]
        for row in _rows(
            db, "SELECT event_id FROM ce_events WHERE artifact_id=?", (slot_b,)
        )
    ]
    marks = ",".join("?" * len(b_events))
    expected_inserted = (
        _slot_session_count(db, slot_b)
        + len(b_events)
        + len(_rows(
            db,
            f"SELECT 1 FROM ce_field_dispositions WHERE event_id IN ({marks})",
            tuple(b_events),
        ))
        + (len(_table(db, "ce_event_relations")) - relations_before)
    )
    assert report["rows_inserted"] == expected_inserted

    assert _rows(db, "PRAGMA foreign_key_check") == []


def test_swallowed_cross_slot_relation_follows_the_current_copy(
    tmp_path: Path,
) -> None:
    """(b) A 的端点事件 stale 后再 apply B：关系边迁到 B 且全程可见。"""

    mirror = tmp_path / "mirror"
    _write_claude(mirror, CLAUDE_FILE, _claude_tool_pair())
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _claude_slot("t.jsonl")
    relation_id = _seed_call_result_relation(db, slot_b)
    anchor_a = _relation_endpoints(db, relation_id)
    assert _table(db, "ce_relation_versions") == []

    # P0-1 的 stale 路径：A 的文件丢掉 tool 对，其两个端点事件离开当前计算
    # （关系行本身没有 stale_at，现行与否只能看端点事件）。
    _write_claude(mirror, CLAUDE_FILE, _claude_tool_pair_user_only())
    truncated = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert truncated["rows_stale_marked"] >= 2
    stale_states = _rows(
        db,
        "SELECT stale_at FROM ce_events WHERE event_id IN (?,?)",
        anchor_a,
    )
    assert len(stale_states) == 2 and all(row[0] is not None for row in stale_states)

    _write_claude(mirror, "t.jsonl", _claude_tool_pair())
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["status"] == "ok"
    assert report["relations_ignored_duplicates"] == 1
    assert report["relations_endpoint_refreshed"] == 1
    assert report["per_family"][CLAUDE]["relations_endpoint_refreshed"] == 1

    # 边跟随现行副本：端点已刷新为 slot B 的事件。
    anchor_after = _relation_endpoints(db, relation_id)
    assert anchor_after != anchor_a
    b_events = {
        row[0]
        for row in _rows(
            db, "SELECT event_id FROM ce_events WHERE artifact_id=?", (slot_b,)
        )
    }
    assert set(anchor_after) <= b_events

    # 被替换的锚点按 append-only 契约进了历史表，没有无痕丢失。
    versions = _relation_versions(db, relation_id)
    assert [row[0] for row in versions] == [0]
    assert (versions[0][2], versions[0][3]) == anchor_a

    # 择优后该关系归属 slot B（双端点都在 B 的事件集内），归属判定与引擎
    # 读取 slot 关系集用的是同一条查询。
    con = _connect(db)
    try:
        assert relation_id in _slot_relation_ids(con, GENERATION, slot_b)
    finally:
        con.close()
    assert _rows(db, "PRAGMA foreign_key_check") == []


# ------------------------------------------------- batch B engine repairs
#
# P1-13 (fingerprint race), P2 (mirror-wipe guard), P2 (unregistered family
# slots). Each test pins one repair of the engine, isolated from the oracle
# tests above.


def test_capture_then_append_is_not_hidden_by_the_new_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    """P1-13: a file appended during the capture window is re-collected next pass.

    The fingerprint written by an apply must describe the *pre-capture* stat —
    the instant whose bytes the store now holds. Re-statting after the capture
    would freeze the post-capture stat over rows that do not contain the
    appended tail, and the tail would be hidden from every later pass.
    """

    mirror = tmp_path / "mirror"
    _build_corpus(mirror, names=("a.jsonl",))
    db = tmp_path / "live.sqlite"

    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"

    # An external edit lands before the next pass: the file becomes a candidate.
    path = mirror / FAMILY / "a.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8") + _codex_session("sess_mid", 1),
        encoding="utf-8",
    )

    # The race: the source keeps being appended while the engine captures it.
    real_capture = live_sync_module._capture_and_adapt

    def capture_then_append(file_path, **kwargs):
        artifact, result = real_capture(file_path, **kwargs)
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(_codex_session("sess_tail", 1))
        return artifact, result

    monkeypatch.setattr(
        live_sync_module, "_capture_and_adapt", capture_then_append
    )
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    monkeypatch.undo()
    assert second["status"] == "ok"

    # The next pass must see the tail (the stored fingerprint describes the
    # pre-capture instant, so the appended file no longer matches it).
    third = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert third["status"] == "ok"
    assert third["n_changed"] >= 1
    tail_rows = _rows(
        db,
        "SELECT COUNT(*) FROM ce_events WHERE content LIKE ?",
        ("%sess_tail%",),
    )
    assert tail_rows[0][0] >= 1


def test_mass_removal_is_refused_by_the_guard(tmp_path: Path) -> None:
    """P2: > half of the active slots vanishing in one pass refuses to apply.

    A mirror that points at nothing (wiped root, failed stage) would otherwise
    deactivate every slot — and mark the whole live store stale — in a single
    transaction. The guard aborts before any write; the error text is the
    operator's signal.
    """

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"

    for name in ("b.jsonl", "c.jsonl", "d.jsonl"):
        (mirror / FAMILY / name).unlink()

    with pytest.raises(LiveSyncError) as excinfo:
        live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert "refused" in str(excinfo.value)
    assert "3 of 4" in str(excinfo.value)

    # Nothing was written: no slot deactivated, no removal recorded.
    assert _rows(
        db, "SELECT COUNT(*) FROM ce_live_slots WHERE active=1"
    ) == [(4,)]
    assert _removal_records(db) == []


def test_unregistered_family_slots_are_never_marked_removed(
    tmp_path: Path, monkeypatch
) -> None:
    """P2: a family that stops resolving is reported, not deactivated.

    ``scan_mirror`` refuses to guess an unregistered family, so the family's
    files drop out of ``scanned``; without the fix its active slots looked like
    removals and were deactivated (rows marked stale) by the next apply.
    """

    mirror = tmp_path / "mirror"
    _build_corpus(mirror, names=("a.jsonl",))
    db = tmp_path / "live.sqlite"
    first = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert first["status"] == "ok"

    real_resolve = live_sync_module.resolve_family

    def deregistered(name):
        if name == FAMILY:
            raise KeyError(name)
        return real_resolve(name)

    monkeypatch.setattr(live_sync_module, "resolve_family", deregistered)
    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["status"] == "no-op"
    assert second["unregistered_families"] == [FAMILY]
    assert _rows(
        db,
        "SELECT active FROM ce_live_slots WHERE slot_id=?",
        (_slot_id("a.jsonl"),),
    ) == [(1,)]
    assert _removal_records(db) == []

    # The family registers again (or the rename is undone): the slot lines up
    # with its file unchanged and nothing was lost in between.
    monkeypatch.undo()
    third = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert third["status"] == "no-op"
    assert third["n_removed"] == 0
    assert third["unregistered_families"] == []
