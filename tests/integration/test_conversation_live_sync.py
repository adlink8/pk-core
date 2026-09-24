"""Milestone 2: incremental live-sync (stable slot identity, per-file replace).

RED/GREEN tests for :func:`personal_knowledge.application.conversation.live_sync.
live_sync_once`. The engine keeps ONE live generation and, per source slot,
prunes + re-inserts only the rows that file no longer / now emits.

The load-bearing test is the **equality oracle**: after an incremental apply the
compatibility projection (``canonical_sessions`` / ``canonical_messages``) must
be row-for-row identical to a fresh full rebuild of the same corpus. Any missed
prune or missed insert shows up there and nowhere else.

All tests run against temporary SQLite files under ``tmp_path``. No live
database, no ``data/``, no network, no provider calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.snapshots import (
    make_slot_artifact_id,
)
from personal_knowledge.application.conversation.compatibility_projection import (
    build_compatibility_projection,
    write_compatibility_projection,
)
from personal_knowledge.application.conversation.live_sync import live_sync_once
from personal_knowledge.application.run_pipeline import shadow_conversation_generation

FAMILY = "codex"
GENERATION = "live-gen-1"
DEFAULT_FILES = ("a.jsonl", "b.jsonl", "c.jsonl", "d.jsonl")


# --------------------------------------------------------------- synthetic corpus


def _codex_session(session_id: str, turns: int, *, revised: bool = False) -> str:
    """A synthetic Codex JSONL export: meta, then per-turn context/user/answer.

    ``revised=True`` rewrites the FIRST answer body only, keeping the line count
    and every native id/locator unchanged. That is the hardest edit shape for an
    incremental engine: the event id survives while its row content does not, so
    it exercises the "same id, different row" prune path — an engine that only
    prunes *vanished* ids would silently keep the stale text.
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
        if revised and turn == 1:
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


def _full_rebuild_projection(mirror_root: Path, tmp_path: Path, tag: str) -> Path:
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
    entry = report["generations"][FAMILY]
    assert entry["status"] in ("full", "partial"), entry.get("reason")
    projection = build_compatibility_projection(ref_db, entry["generation_id"])
    con = _connect(ref_db)
    try:
        write_compatibility_projection(con, projection)
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
    assert first["rows_inserted"] > 0 and first["rows_pruned"] == 0
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
    """A removal must not leave a stale projected session behind."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    _write(mirror, "b.jsonl", _codex_session("sess_b", 4, revised=True))
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    (mirror / FAMILY / "c.jsonl").unlink()
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert report["n_removed"] == 1

    ref_db = _full_rebuild_projection(mirror, tmp_path, "oracle-removed")
    assert _table(db, "canonical_sessions") == _table(ref_db, "canonical_sessions")
    assert _table(db, "canonical_messages") == _table(ref_db, "canonical_messages")


# ------------------------------------------------------------- 2. idempotency


def test_second_run_without_a_source_change_writes_nothing(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    tables = (
        "ce_events", "ce_sessions", "ce_event_relations", "ce_field_dispositions",
        "ce_source_artifacts", "ce_live_slots", "ce_live_sync_log",
        "canonical_sessions", "canonical_messages",
    )
    before = _counts(db, tables)
    before_bytes = db.read_bytes()

    second = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert second["status"] == "no-op"
    assert second["rows_inserted"] == 0
    assert second["rows_pruned"] == 0
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
    assert report["rows_inserted"] == 0 and report["rows_pruned"] == 0
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

    # The write is proportional to the edited file, nowhere near the corpus.
    assert report["n_changed"] == 1
    assert 0 < report["rows_inserted"] <= 4
    assert report["rows_inserted"] < file_rows
    assert report["rows_inserted"] < first["rows_inserted"] / 4

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


# -------------------------------------------------------------- 4. removal


def test_removed_source_is_pruned_and_leaves_no_dangling_rows(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    doomed_session = _canonical_session_for_content(db, "of sess_b")
    assert _event_ids_for_slot(db, slot_b)
    sessions_before = len(_table(db, "canonical_sessions"))

    (mirror / FAMILY / "b.jsonl").unlink()
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_removed"] == 1
    assert report["rows_pruned"] > 0 and report["rows_inserted"] == 0
    assert report["touched_sessions"]

    # The slot's ce rows are gone...
    assert _event_ids_for_slot(db, slot_b) == []
    assert _rows(
        db, "SELECT session_id FROM ce_sessions WHERE artifact_id=?", (slot_b,)
    ) == []

    # ...its session is gone from the projection...
    assert _rows(
        db,
        "SELECT canonical_session_id FROM canonical_sessions WHERE canonical_session_id=?",
        (doomed_session,),
    ) == []
    assert _rows(
        db,
        "SELECT canonical_message_id FROM canonical_messages WHERE canonical_session_id=?",
        (doomed_session,),
    ) == []
    assert len(_table(db, "canonical_sessions")) == sessions_before - 1

    # ...the slot itself is retained but marked inactive, and so is its
    # provenance row (ce_source_artifacts is never deleted).
    slot = _rows(
        db,
        "SELECT active, content_hash FROM ce_live_slots WHERE slot_id=?",
        (slot_b,),
    )
    assert slot and slot[0][0] == 0
    assert _rows(
        db, "SELECT artifact_id FROM ce_source_artifacts WHERE artifact_id=?",
        (slot_b,),
    )

    # ...and the remaining rows are referentially clean.
    assert _rows(db, "PRAGMA foreign_key_check") == []
    assert _rows(db, "SELECT * FROM pragma_integrity_check") == [("ok",)]


def test_removed_source_returning_is_re_added(tmp_path: Path) -> None:
    """A re-appearing file reuses its slot id (marked inactive -> active)."""

    mirror = tmp_path / "mirror"
    _build_corpus(mirror)
    db = tmp_path / "live.sqlite"
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    slot_b = _slot_id("b.jsonl")
    body = (mirror / FAMILY / "b.jsonl").read_text(encoding="utf-8")
    (mirror / FAMILY / "b.jsonl").unlink()
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert _event_ids_for_slot(db, slot_b) == []

    _write(mirror, "b.jsonl", body)
    report = live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)

    assert report["n_added"] == 1
    assert _event_ids_for_slot(db, slot_b)  # same slot, rows restored
    assert _rows(
        db, "SELECT active FROM ce_live_slots WHERE slot_id=?", (slot_b,)
    )[0][0] == 1


# --------------------------------------------------------------- 5. dry run


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
        "ce_live_sync_log", "canonical_sessions", "canonical_messages",
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
    assert plan["rows_inserted"] == 0 and plan["rows_pruned"] == 0
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
            "rows_pruned": 0, "rows_inserted": 0,
        }
    }
