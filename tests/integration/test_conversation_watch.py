"""Batch B: the watch lock contract (P1-15a one-shot locking, P2 PID reuse).

Pins two repairs of :mod:`personal_knowledge.application.conversation.watch`:

* **P1-15a** — the one-shot ``once=True`` cycle takes the same default lock as
  the resident watcher when no ``lock_path`` is passed, so a scheduled task can
  never stage-and-apply concurrently with a watcher. A refused one-shot reports
  ``status: "locked"`` and runs nothing.
* **P2** — a lock whose PID was recycled onto an unrelated process (holder
  crashed, OS reused the PID) is reclaimable via the recorded process-creation
  token, and — as the conservative fallback — via the lock-age rule
  (``STALE_LOCK_MAX_AGE_S``). Without either, a recycled PID means a permanent
  ``STATUS_LOCKED``.

All tests run under ``tmp_path``; no real mirror, database or client roots.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_knowledge.application.conversation import watch as watch_module
from personal_knowledge.application.conversation.watch import (
    STALE_LOCK_MAX_AGE_S,
    STATUS_LOCKED,
    WatchLock,
    run_watch,
)


def _run_args(tmp_path: Path) -> dict:
    return {
        "db": tmp_path / "live.sqlite",
        "mirror_root": tmp_path / "mirror",
        "generation_id": "live",
    }


def _no_stage() -> dict:
    raise AssertionError("stage_fn must not run")


def _no_apply(**_kwargs) -> dict:
    raise AssertionError("apply_fn must not run")


# ------------------------------------------------------------- P1-15a lock


def test_once_cycle_takes_the_default_lock_and_releases_it(tmp_path: Path) -> None:
    """A one-shot without an explicit lock_path locks the default path."""

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    lock_path = tmp_path / "live-sync.lock"
    calls: list[dict] = []

    def apply_stub(**kwargs) -> dict:
        calls.append(kwargs)
        return {"status": "no-op"}

    report = run_watch(
        once=True,
        stage_fn=lambda: {"staged": 0, "skipped": 0},
        apply_fn=apply_stub,
        **_run_args(tmp_path),
    )

    assert report["status"] == "no-op"
    assert len(calls) == 1  # the cycle actually ran
    assert not lock_path.exists(), "the one-shot released the default lock"


def test_once_cycle_reports_locked_while_a_watcher_holds_the_lock(
    tmp_path: Path,
) -> None:
    """A held lock stops the one-shot explicitly: locked, zero cycles, no run."""

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    lock_path = tmp_path / "live-sync.lock"
    stop = threading.Event()

    def holder() -> None:
        run_watch(
            once=False,
            interval_s=0.01,
            debounce_scans=1,
            scan_fn=lambda: {},
            stage_fn=lambda: {"staged": 0, "skipped": 0},
            apply_fn=lambda **_kwargs: {"status": "no-op"},
            stop_event=stop,
            lock_path=lock_path,
            **_run_args(tmp_path),
        )

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not lock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lock_path.exists(), "the watcher acquired the default lock"

    try:
        report = run_watch(
            once=True,
            stage_fn=_no_stage,
            apply_fn=_no_apply,
            **_run_args(tmp_path),
        )
        assert report["status"] == STATUS_LOCKED
        assert report["cycles"] == 0
        assert report["lock"]["present"] is True
    finally:
        stop.set()
        thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert not lock_path.exists(), "the watcher released the lock on shutdown"


# ------------------------------------------------- P2 PID reuse / lock age


def _record(tmp_path: Path, **payload) -> WatchLock:
    lock = WatchLock(tmp_path / "live-sync.lock")
    base = {
        "pid": os.getpid(),
        "started_at": watch_module._now(),
        "heartbeat_at": watch_module._now(),
    }
    base.update(payload)
    lock.path.write_text(json.dumps(base), encoding="utf-8")
    return lock


def test_acquire_records_the_holder_start_token(tmp_path: Path) -> None:
    """The lock record carries the holder's process-creation token (P2)."""

    lock = WatchLock(tmp_path / "live-sync.lock")
    try:
        assert lock.acquire() is True
        record = lock.read()
        assert record is not None
        assert record["pid"] == os.getpid()
        assert (
            record["holder_proc_start"]
            == watch_module._process_start_token(os.getpid())
        )
    finally:
        lock.release()
    assert not lock.path.exists()


def test_recycled_pid_is_reclaimed_immediately(tmp_path: Path, monkeypatch) -> None:
    """Rule 2: alive PID, but the creation token differs from the holder's."""

    lock = _record(tmp_path, holder_proc_start=1.0)
    monkeypatch.setattr(
        watch_module,
        "_process_start_token",
        lambda pid: 2.0 if pid == os.getpid() else None,
    )
    assert lock._reclaim_if_stale() is True
    assert not lock.path.exists()


def test_matching_start_token_keeps_a_live_holder(tmp_path: Path, monkeypatch) -> None:
    """Rule 2 negative: the same token means the original holder still runs."""

    lock = _record(tmp_path, holder_proc_start=424242.0)
    monkeypatch.setattr(
        watch_module,
        "_process_start_token",
        lambda pid: 424242.0 if pid == os.getpid() else None,
    )
    assert lock._reclaim_if_stale() is False
    assert lock.path.exists()


def test_lock_older_than_the_threshold_is_reclaimed(tmp_path: Path) -> None:
    """Rule 3: no start token (legacy record), heartbeat silent for > 24 h."""

    assert STALE_LOCK_MAX_AGE_S == 24 * 3600.0
    stale = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_LOCK_MAX_AGE_S + 60)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    lock = WatchLock(tmp_path / "live-sync.lock")
    lock.path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": stale,
                    "heartbeat_at": stale}),
        encoding="utf-8",
    )
    assert lock._reclaim_if_stale() is True
    assert not lock.path.exists()


def test_fresh_record_without_a_start_token_is_kept(tmp_path: Path) -> None:
    """Rule 3 negative: an unparseable-token-free but fresh record is kept."""

    lock = _record(tmp_path)  # live PID, fresh heartbeat, no holder_proc_start
    assert lock._reclaim_if_stale() is False
    assert lock.path.exists()


def test_record_without_usable_timestamps_is_kept(tmp_path: Path) -> None:
    """Conservative fallback: no tokens, no timestamps -> never guessed."""

    lock = WatchLock(tmp_path / "live-sync.lock")
    lock.path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    assert lock._reclaim_if_stale() is False
    assert lock.path.exists()
