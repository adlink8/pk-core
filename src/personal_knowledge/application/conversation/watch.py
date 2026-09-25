"""Milestone 3: the background listener that drives ``stage -> live_sync_once``.

Why this module exists
----------------------
``live_sync_once`` (milestone 2) is the **database** half of a live sync: it
reconciles the live generation against whatever is currently in the mirror. It
does not fetch anything. A real-time loop must also get new client content into
the mirror first, so one cycle is::

    stage_client_sources(stage_root=mirror_root)   # client roots -> mirror
    live_sync_once(db=..., mirror_root=..., ...)   # mirror -> live generation

**Staging is not re-implemented here.** The function that populates the mirror
from the machine-local client roots already exists and is already wired to the
CLI: ``discovery.stage_client_sources`` is called from
``application/conversation/v2_sync.py`` (the ``--v2-native`` branch of
``cmd_conversations_v2``), which itself is reached from
``application/sync.py::_cmd_conversations`` and ``cli.py::sync``. This module
calls the *same* function with the *same* arguments (``stage_root`` +
``byte_limit``), so the mirror keeps exactly one owner of its ``.hashes.json``
watermark and there is no second staging implementation to drift.

Design decisions (all conservative; each one is a choice, not a default)
-----------------------------------------------------------------------
* **Debounce is scan-count based, not time based.** A trigger requires
  ``debounce_scans`` *consecutive polls with an identical fingerprint set*. A
  client that is still appending to a rollout file therefore produces a moving
  fingerprint set and is never handed to the parser half-written. Counting scans
  (rather than sleeping) also means a machine that is idle-but-alive and a
  machine that is being written to are distinguished by the same signal.
* **The fingerprint set covers the source roots, not the mirror.** The mirror is
  written *by* staging, so fingerprinting it would let staging's own writes
  look like new client activity (an infinite trigger loop).
* **The lock guards every real run, one-shot included.** ``--watch`` always
  takes it, and so does the one-shot ``once=True`` cycle (P1-15a: when no
  ``lock_path`` is passed, the one-shot uses the same default lock path, so a
  scheduled task can never stage-and-apply concurrently with a resident
  watcher). A run that finds the lock held reports ``status: "locked"``, runs
  nothing, and the CLI exits non-zero — an explicit machine-readable refusal,
  never silent overlap.
* **A cycle failure is reported, never raised.** ``status: "partial"`` means the
  loop is alive but the last cycle did not complete; a dead loop is detectable
  from the heartbeat instead. ``once=True`` returns ``partial`` rather than
  raising so a scheduled task gets a machine-readable report and a non-zero CLI
  exit code.
* **Heartbeat every poll, not only on a change.** An operator has to be able to
  tell "alive and idle" from "dead" — a log that only records applies cannot.

No import-time side effects: importing this module creates no table, file, lock
or connection. Nothing here is a live-data default: ``data/`` paths only ever
arrive from the caller (the CLI), and every test runs under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from personal_knowledge.adapters.conversation_sources.discovery import (
    FAMILY_CLIENT_ROOTS,
    _walk,
    stage_client_sources,
)
from personal_knowledge.application.conversation.live_sync import (
    _connect,
    live_sync_once,
)
from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB

# --------------------------------------------------------------- constants

#: Generation id used when the caller does not pin one. The engine keeps exactly
#: one live generation, so a stable human-readable id (not a content-derived
#: cohort id) is what makes incremental apply meaningful.
DEFAULT_LIVE_GENERATION_ID = "live"

DEFAULT_INTERVAL_S = 30.0
DEFAULT_DEBOUNCE_SCANS = 2

#: Staging limits: the CLI defaults of the existing ``--v2-native`` path, so the
#: watch loop stages exactly the same corpus as a manual staging run.
STAGE_BYTE_LIMIT = 600_000_000
STAGE_COUNT_LIMIT = 2_000

#: Size cap of the listener log; one rotated generation is kept.
DEFAULT_LOG_MAX_BYTES = 1 << 20

LOCK_FILE_NAME = "live-sync.lock"
LOG_FILE_NAME = "live-sync.log"
#: Suffix of the single kept log generation (``live-sync.log.1``).
ROTATED_SUFFIX = ".1"

#: P2 (Windows PID reuse): a lock record that has not beaten its heartbeat for
#: this long is reclaimable even when its PID still looks alive. At production
#: poll intervals the holder beats every cycle, so a day of silence means a
#: frozen or abandoned process (or a legacy record without a start token) —
#: waiting longer only converts a crashed holder into a permanent
#: ``STATUS_LOCKED``. Deliberate trade-off: a holder that stalls without dying
#: for over 24 h loses its lock; that is judged strictly less harmful than an
#: unstealable lock, and the start-token rule below already reclaims the common
#: PID-recycling case immediately.
STALE_LOCK_MAX_AGE_S = 24 * 3600.0

#: ``status`` values a caller can branch on.
STATUS_OK = "ok"
STATUS_NO_OP = "no-op"
STATUS_DRY_RUN = "dry-run"
STATUS_PARTIAL = "partial"
STATUS_LOCKED = "locked"
STATUS_STOPPED = "stopped"


# ----------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_json(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _default_paths(mirror_root: Path) -> tuple[Path, Path]:
    """``(lock, log)`` next to the mirror's parent.

    The engine already treats ``mirror_root.parent`` as the run directory (the
    content-addressed blob store is its sibling ``artifacts`` dir), so the
    listener's coordination files belong there too.
    """
    parent = Path(mirror_root).parent
    return parent / LOCK_FILE_NAME, parent / LOG_FILE_NAME


# ------------------------------------------------------------------- lock


def _pid_alive(pid: int) -> bool:
    """Read-only liveness probe for ``pid``.

    ``os.kill(pid, 0)`` is used on POSIX only. On Windows that call does **not**
    mean "probe": CPython implements it as ``TerminateProcess`` for any signal
    other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT``, so probing with it would
    kill the process whose lock we are inspecting.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """``OpenProcess``/``GetExitCodeProcess`` probe (no termination risk)."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # No such process -> reclaimable. Access denied -> the process exists
        # (but belongs to another user): treat as alive so we never steal a
        # live holder's lock.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _process_start_token(pid: int) -> float | None:
    """An opaque process-creation token for ``pid`` (identity by equality).

    P2 (Windows PID reuse): a PID is recycled by the OS, so "the PID is alive"
    no longer means "the recorded holder is alive". The holder's creation time
    is an identity token that survives recycling: Windows uses the
    ``GetProcessTimes`` creation ``FILETIME`` (epoch seconds); POSIX with
    ``/proc`` uses field 22 of ``/proc/<pid>/stat`` (boot-relative ticks — not
    an epoch value, but stable for one live process, which is all the equality
    comparison needs). ``None`` when the platform or the probe cannot tell;
    callers then fall back to the lock-age rule instead of guessing.
    """

    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_ft, kernel_ft, user_ft = (
                wintypes.FILETIME() for _ in range(3)
            )
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_ft),
                ctypes.byref(kernel_ft),
                ctypes.byref(user_ft),
            ):
                return None
            return (
                (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            ) / 10_000_000.0 - 11644473600.0
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return float(stat_text.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _parse_utc(value) -> float | None:  # noqa: ANN001 - record payload
    """Epoch seconds for the ``_now()`` format, or ``None`` if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _record_age_s(record: dict) -> float | None:
    """Seconds since the record last proved itself (heartbeat, else start)."""
    for key in ("heartbeat_at", "started_at"):
        stamp = _parse_utc(record.get(key))
        if stamp is not None:
            return max(0.0, time.time() - stamp)
    return None


class WatchLock:
    """Single-instance lock: a PID record that also carries the heartbeat.

    Acquisition uses ``O_CREAT|O_EXCL``, so it is atomic against a second
    instance. A record whose PID is dead (crash, power loss, killed console) is
    stale and is reclaimed. The file is deliberately *not* a pure mutex: the
    holder rewrites ``heartbeat_at`` in it so an operator can tell a live idle
    listener from an abandoned lock. The record also carries the holder's
    process-creation token (``holder_proc_start``), which lets a reclaimer tell
    a recycled PID from the original holder on Windows (P2).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._held = False
        self._started_at: str | None = None

    # -- read-only inspection (used by ``live_status`` too)

    def read(self) -> dict | None:
        return _read_json(self.path)

    def holder_alive(self) -> bool:
        record = self.read()
        if record is None:
            return False
        try:
            return _pid_alive(int(record.get("pid", -1)))
        except (TypeError, ValueError):
            return False

    def describe(self) -> dict:
        record = self.read()
        if record is None:
            return {"path": str(self.path), "present": False}
        return {
            "path": str(self.path),
            "present": True,
            "pid": record.get("pid"),
            "started_at": record.get("started_at"),
            "heartbeat_at": record.get("heartbeat_at"),
            "holder_alive": self.holder_alive(),
        }

    # -- lifecycle

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _attempt or not self._reclaim_if_stale():
                    return False
                continue
            self._started_at = _now()
            payload = {
                "pid": os.getpid(),
                "started_at": self._started_at,
                "heartbeat_at": self._started_at,
                "holder_proc_start": _process_start_token(os.getpid()),
            }
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True))
            self._held = True
            return True
        return False

    def _reclaim_if_stale(self) -> bool:
        """Drop the lock file only if it is provably stale, and unchanged by then.

        Three reclaim rules, most specific first (P2 PID-reuse hardening):

        1. the recorded PID is dead — the classic stale case;
        2. the PID is alive but its process-creation token differs from the
           recorded ``holder_proc_start`` — the PID was recycled onto an
           unrelated process, so the real holder is gone; reclaim immediately;
        3. the PID is alive (or a start token is unavailable on either side)
           but the record has not beaten its heartbeat for
           ``STALE_LOCK_MAX_AGE_S`` — see the constant's trade-off note.

        Rule 3 is the conservative fallback: an unparseable record without any
        usable timestamp keeps the lock rather than guessing.
        """
        try:
            raw = self.path.read_bytes()
        except OSError:
            return False
        record = _read_json(self.path)
        if record is None:
            # Unreadable/truncated record from a killed writer: reclaimable, but
            # re-check the bytes so we cannot delete a fresh record written in
            # between.
            try:
                if self.path.read_bytes() != raw:
                    return False
            except OSError:
                return False
            _unlink(self.path)
            return True
        try:
            pid = int(record.get("pid", -1))
        except (TypeError, ValueError):
            pid = -1
        if not _pid_alive(pid):
            reclaim = True
        else:
            holder_start = record.get("holder_proc_start")
            current_start = _process_start_token(pid)
            try:
                recycled = (
                    holder_start is not None
                    and current_start is not None
                    and float(holder_start) != float(current_start)
                )
            except (TypeError, ValueError):
                recycled = False
            if recycled:
                reclaim = True
            else:
                age = _record_age_s(record)
                reclaim = age is not None and age > STALE_LOCK_MAX_AGE_S
        if not reclaim:
            return False
        try:
            if self.path.read_bytes() != raw:
                return False  # the holder rotated it under us
        except OSError:
            return False
        _unlink(self.path)
        return True

    def heartbeat(self) -> None:
        if not self._held:
            return
        payload = {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "heartbeat_at": _now(),
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            _unlink(tmp)  # a failed heartbeat must never kill the loop

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        record = self.read()
        if record is not None:
            try:
                if int(record.get("pid", -1)) != os.getpid():
                    return  # someone else owns it now: never delete their lock
            except (TypeError, ValueError):
                pass
        _unlink(self.path)


# ------------------------------------------------------------------- log


def _append_log(log_path: Path | None, record: dict, *,
                max_bytes: int = DEFAULT_LOG_MAX_BYTES) -> None:
    """Size-capped append of one JSON line. Never raises."""
    if log_path is None:
        return
    line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        if log_path.exists() and log_path.stat().st_size + len(line) > max_bytes:
            os.replace(log_path, log_path.with_name(
                log_path.name + ROTATED_SUFFIX
            ))
    except OSError:
        pass
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass  # logging is best-effort; it must never break the loop


# ------------------------------------------------------------ source scan


def scan_source_fingerprints(
    roots: Mapping[str, tuple[Path, ...]] | None = None,
) -> dict[str, list[int]]:
    """``{absolute source path: [mtime_ns, size]}`` over the client roots.

    The walk reuses ``discovery._walk`` (same depth bound, same skip list, same
    refusal to follow symlinks/junctions) so the poll sees exactly the file set
    the staging path sees. Stat-only: nothing is opened, hashed or parsed — that
    is what makes a 30-second poll cheap enough to run continuously.
    """
    effective = FAMILY_CLIENT_ROOTS if roots is None else roots
    out: dict[str, list[int]] = {}
    for _family, root_paths in sorted(effective.items()):
        for root in root_paths:
            root = Path(root)
            try:
                if not root.is_dir():
                    continue
                files = _walk(root)
            except OSError:
                continue
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                out[path.as_posix()] = [stat.st_mtime_ns, stat.st_size]
    return out


def _fingerprint_digest(fingerprints: Mapping[str, list[int]]) -> str:
    payload = json.dumps(
        {str(key): list(value) for key, value in sorted(fingerprints.items())},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------- cycle


def _cycle(
    *,
    db: Path,
    mirror_root: Path,
    generation_id: str,
    stage_fn: Callable[[], dict],
    apply_fn: Callable[..., dict],
    dry_run: bool,
) -> dict:
    """One ``stage -> live_sync_once`` cycle. Raises on failure (caller logs it)."""
    started = time.monotonic()
    staged = stage_fn()
    live = apply_fn(
        db=db,
        mirror_root=mirror_root,
        generation_id=generation_id,
        dry_run=dry_run,
    )
    report = dict(live)
    report["stage"] = {
        "staged": int(staged.get("staged", 0)),
        "skipped": int(staged.get("skipped", 0)),
    }
    report["db"] = str(db)
    report["mirror_root"] = str(mirror_root)
    report["generation_id"] = generation_id
    report["duration_s"] = round(time.monotonic() - started, 3)
    report["heartbeat_at"] = _now()
    return report


def _partial_report(*, db: Path, mirror_root: Path, generation_id: str,
                    error: str, started: float) -> dict:
    """A failed cycle: the loop is alive, the cycle did not complete."""
    return {
        "status": STATUS_PARTIAL,
        "generation_id": generation_id,
        "db": str(db),
        "mirror_root": str(mirror_root),
        "n_added": 0,
        "n_changed": 0,
        "n_removed": 0,
        "n_unchanged": 0,
        "rows_pruned": 0,
        "rows_inserted": 0,
        "per_family": {},
        "touched_sessions": [],
        "projection": {"sessions": 0, "messages": 0, "tools": 0},
        "stage": {"staged": 0, "skipped": 0},
        "error": error,
        "duration_s": round(time.monotonic() - started, 3),
        "heartbeat_at": _now(),
    }


# ------------------------------------------------------------------ signals


def _install_stop_handlers(stop: threading.Event) -> dict:
    """Route SIGINT/SIGTERM to ``stop``; return the replaced handlers.

    The handler only sets the flag, so an in-flight transaction is never
    interrupted: the running cycle finishes, and the loop notices the flag
    before its next poll. Installing handlers can fail (not the main thread,
    restricted platform), in which case the loop still runs and is stoppable
    only by terminating the process.
    """
    previous: dict = {}

    def _handler(_signum, _frame) -> None:  # noqa: ANN001 - signal signature
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            continue
    return previous


def _restore_handlers(previous: Mapping) -> None:
    for sig, handler in (previous or {}).items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            pass


# ------------------------------------------------------------------- watch


def run_watch(
    *,
    db: Path,
    mirror_root: Path,
    generation_id: str,
    source_roots: dict[str, tuple[Path, ...]] | None = None,
    interval_s: float = DEFAULT_INTERVAL_S,
    debounce_scans: int = DEFAULT_DEBOUNCE_SCANS,
    once: bool = False,
    lock_path: Path | None = None,
    log_path: Path | None = None,
    scan_fn: Callable[[], Mapping[str, list[int]]] | None = None,
    stage_fn: Callable[[], dict] | None = None,
    apply_fn: Callable[..., dict] | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    """Run the live listener (or, with ``once=True``, a single cycle).

    ``source_roots`` is passed straight through to ``stage_client_sources`` as
    ``roots`` (``family -> (candidate root,)``); ``None`` keeps that function's
    own default (:data:`discovery.FAMILY_CLIENT_ROOTS`), which is the same
    behaviour the ``--v2-native`` path has.

    ``scan_fn`` / ``stage_fn`` / ``apply_fn`` / ``stop_event`` exist so the loop
    can be driven deterministically (a test or a sandbox rehearsal) instead of
    being slept through; every one of them defaults to the production behaviour.

    Returns the last cycle's report, extended with ``cycles`` / ``heartbeat_at``.
    In loop mode the return value describes the shutdown
    (``status: "stopped"`` + the number of applied cycles and the last report).
    ``status: "locked"`` means another instance holds the lock and nothing ran.
    """
    db = Path(db)
    mirror_root = Path(mirror_root)
    generation_id = str(generation_id)
    interval = max(0.0, float(interval_s))
    debounce = max(1, int(debounce_scans))
    scan_fn = scan_fn or (lambda: scan_source_fingerprints(source_roots))
    stage_fn = stage_fn or (
        lambda: stage_client_sources(
            stage_root=mirror_root,
            roots=source_roots,
            byte_limit=STAGE_BYTE_LIMIT,
            count_limit=STAGE_COUNT_LIMIT,
        )
    )
    apply_fn = apply_fn or live_sync_once
    stop = stop_event if stop_event is not None else threading.Event()
    if once and lock_path is None:
        # P1-15a: a one-shot cycle races the resident watcher exactly like a
        # second watcher would, so it takes the same default lock. When the
        # lock is held it returns an explicit ``status: "locked"`` report and
        # runs nothing — never a silent concurrent staging/apply.
        lock_path = _default_paths(mirror_root)[0]
    lock = WatchLock(Path(lock_path)) if lock_path is not None else None

    if lock is not None and not lock.acquire():
        record = {"kind": "locked", "at": _now(), "lock": lock.describe()}
        _append_log(log_path, record)
        return {
            "status": STATUS_LOCKED,
            "generation_id": generation_id,
            "db": str(db),
            "mirror_root": str(mirror_root),
            "cycles": 0,
            "heartbeat_at": _now(),
            "lock": lock.describe(),
        }

    previous_handlers: dict = {}
    if not once:
        previous_handlers = _install_stop_handlers(stop)

    try:
        if once:
            started = time.monotonic()
            try:
                report = _cycle(
                    db=db, mirror_root=mirror_root, generation_id=generation_id,
                    stage_fn=stage_fn, apply_fn=apply_fn, dry_run=False,
                )
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                report = _partial_report(
                    db=db, mirror_root=mirror_root, generation_id=generation_id,
                    error=f"{type(exc).__name__}: {exc}", started=started,
                )
            report["cycles"] = 1
            report["lock"] = lock.describe() if lock is not None else None
            _append_log(log_path, {"kind": "cycle", "once": True, **report})
            return report

        return _loop(
            db=db, mirror_root=mirror_root, generation_id=generation_id,
            interval=interval, debounce=debounce, lock=lock, log_path=log_path,
            scan=scan_fn, stage_fn=stage_fn, apply_fn=apply_fn, stop=stop,
        )
    finally:
        _restore_handlers(previous_handlers)
        if lock is not None:
            lock.release()


def _loop(
    *,
    db: Path,
    mirror_root: Path,
    generation_id: str,
    interval: float,
    debounce: int,
    lock: WatchLock | None,
    log_path: Path | None,
    scan: Callable[[], Mapping[str, list[int]]],
    stage_fn: Callable[[], dict],
    apply_fn: Callable[..., dict],
    stop: threading.Event,
) -> dict:
    """The poll/debounce/apply loop. Never raises on a cycle failure."""
    last_digest: str | None = None
    applied_digest: str | None = None
    stable_runs = 0
    cycles = 0
    last_report: dict | None = None

    while not stop.is_set():
        try:
            fingerprints = scan()
            digest = _fingerprint_digest(fingerprints)
            scanned = len(fingerprints)
        except Exception as exc:  # noqa: BLE001 - a failed poll is survivable
            stable_runs = 0
            last_digest = None
            _append_log(log_path, {
                "kind": "poll-error", "at": _now(), "status": STATUS_PARTIAL,
                "error": f"{type(exc).__name__}: {exc}",
            })
            if lock is not None:
                lock.heartbeat()
            if stop.wait(interval):
                break
            continue

        if digest == last_digest:
            stable_runs += 1
        else:
            stable_runs = 1
            last_digest = digest

        due = stable_runs >= debounce and digest != applied_digest
        if due and not stop.is_set():
            started = time.monotonic()
            try:
                report = _cycle(
                    db=db, mirror_root=mirror_root, generation_id=generation_id,
                    stage_fn=stage_fn, apply_fn=apply_fn, dry_run=False,
                )
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                report = _partial_report(
                    db=db, mirror_root=mirror_root, generation_id=generation_id,
                    error=f"{type(exc).__name__}: {exc}", started=started,
                )
            cycles += 1
            report["cycles"] = cycles
            last_report = report
            # A partial cycle is retried on the next stable poll; a completed
            # one is not (the fingerprint set has been consumed).
            if report["status"] != STATUS_PARTIAL:
                applied_digest = digest
            _append_log(log_path, {"kind": "cycle", "digest": digest, **report})
        else:
            _append_log(log_path, {
                "kind": "heartbeat", "at": _now(), "status": "idle",
                "digest": digest, "scanned": scanned,
                "stable_runs": stable_runs, "stable": stable_runs >= debounce,
                "debounce_scans": debounce, "cycles": cycles,
            })

        if lock is not None:
            lock.heartbeat()
        if stop.wait(interval):
            break

    summary = {
        "status": STATUS_STOPPED,
        "generation_id": generation_id,
        "db": str(db),
        "mirror_root": str(mirror_root),
        "cycles": cycles,
        "heartbeat_at": _now(),
        "last": last_report,
        "lock": lock.describe() if lock is not None else None,
    }
    _append_log(log_path, {
        "kind": "stopped", "at": _now(), "status": STATUS_STOPPED,
        "cycles": cycles,
    })
    return summary


# ---------------------------------------------------------------- status


def live_status(*, db: Path, mirror_root: Path, generation_id: str) -> dict:
    """Read-only live-sync summary. Writes nothing: no schema, no lock, no log.

    The pending-change count comes from the engine's own ``dry_run`` plan (the
    one code path that classifies added/changed/removed without capturing), so
    the number an operator reads here is the number the next apply would use.
    """
    db = Path(db)
    mirror_root = Path(mirror_root)
    plan = live_sync_once(
        db=db, mirror_root=mirror_root, generation_id=generation_id, dry_run=True
    )
    lock_path, log_path = _default_paths(mirror_root)
    status: dict = {
        "generation_id": generation_id,
        "db": str(db),
        "db_exists": db.exists(),
        "mirror_root": str(mirror_root),
        "slots": {"total": 0, "active": 0, "inactive": 0},
        "last_sync": None,
        "pending": {
            "n_added": plan["n_added"],
            "n_changed": plan["n_changed"],
            "n_removed": plan["n_removed"],
            "n_unchanged": plan["n_unchanged"],
            "total": plan["n_added"] + plan["n_changed"] + plan["n_removed"],
            "per_family": plan["per_family"],
            "slots": plan.get("slots", []),
        },
        "lock": WatchLock(lock_path).describe(),
        "log_path": str(log_path),
    }
    if not db.exists():
        return status

    con = _connect(db, readonly=True)
    try:
        try:
            rows = con.execute(
                "SELECT active, COUNT(*) FROM ce_live_slots GROUP BY active"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            count = int(row[1])
            status["slots"]["total"] += count
            if int(row[0]):
                status["slots"]["active"] = count
            else:
                status["slots"]["inactive"] = count
        try:
            row = con.execute(
                "SELECT sync_id, started_at, finished_at, n_added, n_changed, "
                "n_removed, rows_pruned, rows_inserted, per_family, status, detail "
                "FROM ce_live_sync_log ORDER BY started_at DESC, sync_id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            status["last_sync"] = {
                "sync_id": row[0],
                "started_at": row[1],
                "finished_at": row[2],
                "n_added": row[3],
                "n_changed": row[4],
                "n_removed": row[5],
                "rows_pruned": row[6],
                "rows_inserted": row[7],
                "per_family": row[8],
                "status": row[9],
                "detail": row[10],
            }
    finally:
        con.close()
    return status


# ------------------------------------------------------------------- CLI


def add_conversation_watch_args(parser) -> None:  # noqa: ANN001 - argparse
    """Add the live-sync / watch flags (additive, opt-in; default flow unchanged)."""
    parser.add_argument(
        "--live-sync",
        action="store_true",
        help="Live sync: stage client sources then run ONE incremental apply, "
             "then exit (default: off; never touches the live canonical store "
             "unless --live-db points at it)",
    )
    parser.add_argument(
        "--live-dry-run",
        action="store_true",
        help="Live sync: report what an incremental apply WOULD change and write "
             "nothing (no staging, no schema, no transaction)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Live sync: run the background listener (poll -> debounce -> stage -> "
             "apply) until SIGINT/SIGTERM",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help=f"Live sync: seconds between polls for --watch (default {int(DEFAULT_INTERVAL_S)})",
    )
    parser.add_argument(
        "--live-status",
        action="store_true",
        help="Live sync: read-only summary (slots, last sync row, pending changes); "
             "writes nothing",
    )
    parser.add_argument(
        "--live-db",
        type=Path,
        default=None,
        help="Live sync: live database (default: the authoritative conversation "
             "store; the shadow staging db is never a default target)",
    )
    parser.add_argument(
        "--live-mirror",
        type=Path,
        default=None,
        help="Live sync: mirror root that staging writes and the engine reads "
             "(default: the --v2-stage default)",
    )
    parser.add_argument(
        "--live-generation",
        default=None,
        help=f"Live sync: generation id to keep live (default: {DEFAULT_LIVE_GENERATION_ID})",
    )


def live_targets(args) -> tuple[Path, Path]:  # noqa: ANN001 - argparse Namespace
    """Resolve ``(mirror_root, db)`` for the live modes.

    The mirror stays in the staging tree — that tree is the *collected corpus*
    (client-file snapshots plus the content-addressed blob store beside it), not a
    shadow copy of the store. The database does **not** stay there: the
    incremental engine is the library's writer, so it targets the authoritative
    conversation store. It used to default to ``--v2-db`` (the Phase 62 shadow
    database, whose whole point was that v2 output never touched the authority);
    that reason died with the manual-activation gate, and pointing at the shadow
    db left the library frozen while fresh data piled up next door.
    """

    mirror_root = Path(args.live_mirror) if args.live_mirror else Path(args.v2_stage)
    db = Path(args.live_db) if args.live_db else AGENT_CONVERSATIONS_DB
    return mirror_root, db


def cmd_conversations_live(args) -> int:  # noqa: ANN001 - argparse
    """CLI routing for the live-sync / watch modes (exit codes as elsewhere:
    0 success, 1 refused/failed, 2 internal)."""
    mirror_root, db = live_targets(args)
    generation_id = args.live_generation or DEFAULT_LIVE_GENERATION_ID

    if args.live_status:
        status = live_status(
            db=db, mirror_root=mirror_root, generation_id=generation_id
        )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    if args.live_dry_run:
        plan = live_sync_once(
            db=db, mirror_root=mirror_root, generation_id=generation_id,
            dry_run=True,
        )
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("[live] dry run: nothing was staged or written.")
        return 0

    if args.watch:
        lock_path, log_path = _default_paths(mirror_root)
        print(
            f"[live] watching {mirror_root} every {args.watch_interval:g}s "
            f"(db={db}, generation={generation_id})\n"
            f"[live] lock: {lock_path}\n[live] log:  {log_path}\n"
            "[live] stop with Ctrl-C (SIGINT) or SIGTERM."
        )
        report = run_watch(
            db=db,
            mirror_root=mirror_root,
            generation_id=generation_id,
            interval_s=args.watch_interval,
            lock_path=lock_path,
            log_path=log_path,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["status"] == STATUS_LOCKED:
            print(f"[error] another live listener already holds {lock_path}")
            return 1
        return 0

    report = run_watch(
        db=db, mirror_root=mirror_root, generation_id=generation_id, once=True
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == STATUS_LOCKED:
        print(
            "[error] another live listener holds the live-sync lock; the "
            "one-shot cycle refused to run"
        )
        return 1
    if report["status"] == STATUS_PARTIAL:
        print(f"[error] live sync cycle failed: {report.get('error')}")
        return 1
    return 0


__all__ = [
    "DEFAULT_DEBOUNCE_SCANS",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_LIVE_GENERATION_ID",
    "STATUS_LOCKED",
    "STATUS_PARTIAL",
    "STATUS_STOPPED",
    "WatchLock",
    "add_conversation_watch_args",
    "cmd_conversations_live",
    "live_status",
    "live_targets",
    "run_watch",
    "scan_source_fingerprints",
]
