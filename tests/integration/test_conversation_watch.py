"""Milestone 3: the background listener (``watch``) and its CLI wiring.

Covers :mod:`personal_knowledge.application.conversation.watch`:

  - ``run_watch(once=True)`` performs one ``stage -> live_sync_once`` cycle and
    a second unchanged cycle is a no-op (the engine's idempotency, observed
    through the listener);
  - debounce: a fingerprint set that keeps moving never triggers; a trigger
    needs ``debounce_scans`` consecutive identical scans (driven with an
    injected ``scan_fn`` — no real sleeping);
  - the single-instance lock refuses a second holder, reclaims a stale record
    from a dead PID, and ``run_watch`` reports ``status: locked`` when the
    holder is alive;
  - failure containment: an exception inside a cycle is reported as
    ``status: partial`` and the loop survives to apply the next stable scan;
  - CLI: ``--live-status`` and ``--live-dry-run`` write nothing (the DB file is
    byte-identical / never created).

The synthetic codex corpus mirrors ``test_conversation_live_sync.py`` (the
engine's own tests) so both halves of the loop are exercised against the same
shape of data. All tests run under ``tmp_path`` — no live database, no
``data/``, no network.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from personal_knowledge.application.conversation.live_sync import live_sync_once
from personal_knowledge.application.conversation.watch import (
    STATUS_LOCKED,
    STATUS_NO_OP,
    STATUS_OK,
    STATUS_PARTIAL,
    WatchLock,
    live_status,
    run_watch,
)
from personal_knowledge.application.sync import main

FAMILY = "codex"
GENERATION = "live"
TABLES = (
    "ce_events", "ce_sessions", "ce_event_relations", "ce_field_dispositions",
    "ce_source_artifacts", "ce_live_slots", "ce_live_sync_log",
    "canonical_sessions", "canonical_messages",
)


# --------------------------------------------------------------- synthetic corpus


def _codex_session(session_id: str, turns: int) -> str:
    """Synthetic Codex JSONL export (same shape as the live-sync tests)."""

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
        records.append(
            {
                "type": "response_item",
                "session_id": session_id,
                "turn_id": f"turn_{turn}",
                "item_id": f"resp_{session_id}_{turn}",
                "role": "assistant",
                "content": f"answer {turn} of {session_id}",
                "timestamp": f"2026-07-01T10:{turn:02d}:05Z",
            }
        )
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _make_source(tmp_path: Path, name: str = "codex.jsonl") -> Path:
    src = tmp_path / "sources"
    src.mkdir(exist_ok=True)
    (src / name).write_text(_codex_session("sess_a", 3), encoding="utf-8")
    return src


def _write_mirror_file(mirror: Path, name: str, text: str) -> Path:
    family_dir = mirror / FAMILY
    family_dir.mkdir(parents=True, exist_ok=True)
    path = family_dir / name
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------- db probes


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return [tuple(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def _counts(db: Path, tables: tuple[str, ...]) -> dict[str, int]:
    return {t: len(_rows(db, f"SELECT 1 FROM {t}")) for t in tables}


# ------------------------------------------------------- 1. once cycle + idempotency


def test_once_cycle_stages_then_applies(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    db = tmp_path / "live.sqlite"
    mirror = tmp_path / "mirror"

    report = run_watch(
        db=db, mirror_root=mirror, generation_id=GENERATION,
        once=True, source_roots={FAMILY: (src,)},
    )
    assert report["status"] == STATUS_OK, report.get("error")
    assert report["stage"]["staged"] == 1
    assert report["rows_inserted"] > 0
    assert len(_rows(db, "SELECT 1 FROM ce_events")) > 0
    assert (mirror / FAMILY / "codex.jsonl").exists()  # the mirror was populated


def test_second_once_cycle_without_change_is_a_noop(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    db = tmp_path / "live.sqlite"
    mirror = tmp_path / "mirror"
    kwargs = dict(
        db=db, mirror_root=mirror, generation_id=GENERATION,
        once=True, source_roots={FAMILY: (src,)},
    )
    assert run_watch(**kwargs)["status"] == STATUS_OK

    before = _counts(db, TABLES)
    second = run_watch(**kwargs)
    assert second["status"] == STATUS_NO_OP
    assert second["rows_inserted"] == 0 and second["rows_pruned"] == 0
    assert _counts(db, TABLES) == before


# ------------------------------------------------------------------ 2. debounce


def _noop_apply() -> dict:
    return {
        "status": "ok", "n_added": 0, "n_changed": 0, "n_removed": 0,
        "n_unchanged": 0, "rows_pruned": 0, "rows_inserted": 0,
        "per_family": {}, "touched_sessions": [],
        "projection": {"sessions": 0, "messages": 0, "tools": 0},
    }


def _drive_loop(
    tmp_path: Path,
    scans: list[dict],
    *,
    apply_failures: int = 0,
    debounce: int = 2,
) -> tuple[dict, list[dict]]:
    """Run the real loop over a scripted scan sequence (no sleeping)."""

    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True)
    db = tmp_path / "live.sqlite"
    applies: list[dict] = []

    def apply_fn(**_kwargs) -> dict:
        applies.append(_kwargs)
        if len(applies) <= apply_failures:
            raise RuntimeError("boom")
        return _noop_apply()

    seq = list(scans)
    stop = threading.Event()

    def scan_fn() -> dict:
        if not seq:
            stop.set()
            return {}
        return seq.pop(0)

    report = run_watch(
        db=db, mirror_root=mirror, generation_id=GENERATION,
        interval_s=0, debounce_scans=debounce, once=False,
        scan_fn=scan_fn,
        stage_fn=lambda: {"staged": 0, "skipped": 0},
        apply_fn=apply_fn, stop_event=stop,
    )
    return report, applies


def test_debounce_triggers_only_after_stable_scans(tmp_path: Path) -> None:
    fp_a = {"a.jsonl": [1, 10]}
    fp_b = {"a.jsonl": [2, 11]}
    # A,A,A -> one apply (needs 2 stable scans); B,B -> a second apply.
    report, applies = _drive_loop(
        tmp_path, [fp_a, fp_a, fp_a, fp_b, fp_b], debounce=2,
    )
    assert len(applies) == 2
    assert report["status"] == "stopped"
    assert report["cycles"] == 2


def test_moving_fingerprint_never_triggers(tmp_path: Path) -> None:
    scans = [{"a.jsonl": [i, 10]} for i in range(6)]
    _report, applies = _drive_loop(tmp_path, scans, debounce=2)
    assert applies == []  # every scan moved; nothing was ever stable


def test_cycle_failure_is_contained_and_retried(tmp_path: Path) -> None:
    fp_a = {"a.jsonl": [1, 10]}
    report, applies = _drive_loop(
        tmp_path, [fp_a, fp_a, fp_a], apply_failures=1, debounce=2,
    )
    # First apply raised (partial); the digest was NOT consumed, so the next
    # stable scan retried it and the loop survived.
    assert len(applies) == 2
    assert report["cycles"] == 2
    assert report["last"]["status"] == "ok"


def test_once_cycle_failure_reports_partial(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    report = run_watch(
        db=tmp_path / "live.sqlite", mirror_root=mirror,
        generation_id=GENERATION, once=True,
        stage_fn=lambda: (_ for _ in ()).throw(RuntimeError("stage exploded")),
    )
    assert report["status"] == STATUS_PARTIAL
    assert "stage exploded" in report["error"]


# ---------------------------------------------------------------------- 3. lock


def test_lock_refuses_a_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "live-sync.lock"
    first = WatchLock(lock_path)
    assert first.acquire() is True
    second = WatchLock(lock_path)
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    pid = proc.pid
    proc.kill()
    proc.wait()

    lock_path = tmp_path / "live-sync.lock"
    lock_path.write_text(
        json.dumps({"pid": pid, "started_at": "x", "heartbeat_at": "x"}),
        encoding="utf-8",
    )
    assert WatchLock(lock_path).acquire() is True


def test_watch_refuses_while_an_alive_holder_owns_the_lock(
    tmp_path: Path,
) -> None:
    src = _make_source(tmp_path)
    lock_path = tmp_path / "live-sync.lock"
    # The current test process is definitely alive, so its PID is a valid
    # "another instance" record.
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": "x", "heartbeat_at": "x"}),
        encoding="utf-8",
    )
    report = run_watch(
        db=tmp_path / "live.sqlite", mirror_root=tmp_path / "mirror",
        generation_id=GENERATION, once=True,
        source_roots={FAMILY: (src,)}, lock_path=lock_path,
    )
    assert report["status"] == STATUS_LOCKED
    assert report["cycles"] == 0
    assert not (tmp_path / "live.sqlite").exists()  # nothing ran


# ------------------------------------------------------------------- 4. CLI


def test_cli_live_status_writes_nothing(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    db = tmp_path / "live.sqlite"
    mirror = tmp_path / "mirror"
    assert run_watch(
        db=db, mirror_root=mirror, generation_id=GENERATION,
        once=True, source_roots={FAMILY: (src,)},
    )["status"] == STATUS_OK
    before = db.read_bytes()

    assert main([
        "conversations", "--live-status",
        "--live-db", str(db), "--live-mirror", str(mirror),
    ]) == 0
    assert db.read_bytes() == before  # byte-identical: read-only summary


def test_cli_live_dry_run_creates_nothing(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _write_mirror_file(mirror, "codex.jsonl", _codex_session("sess_a", 3))
    db = tmp_path / "live.sqlite"

    assert main([
        "conversations", "--live-dry-run",
        "--live-db", str(db), "--live-mirror", str(mirror),
    ]) == 0
    assert not db.exists()  # a dry run must not even create the database


def test_cli_live_status_reports_pending_changes(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _write_mirror_file(mirror, "codex.jsonl", _codex_session("sess_a", 3))
    db = tmp_path / "live.sqlite"

    # Missing DB: the engine has no slot watermark to compare against, so its
    # plan is intentionally empty and the status flags db_exists=False.
    status = live_status(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert status["db_exists"] is False
    assert status["pending"]["total"] == 0
    assert status["slots"]["total"] == 0

    # After one apply the slot is live and nothing is pending.
    live_sync_once(db=db, mirror_root=mirror, generation_id=GENERATION)
    status = live_status(db=db, mirror_root=mirror, generation_id=GENERATION)
    assert status["db_exists"] is True
    assert status["slots"]["active"] == 1
    assert status["pending"]["total"] == 0
