"""Phase 62-01 Task 2: immutable allowlisted source snapshot seam.

RED tests for :mod:`personal_knowledge.adapters.conversation_sources.snapshots`:
  - content-addressed deduplication of identical bytes
  - manifest write/replay verification
  - changed-source detection with immutable old artifacts
  - symlink/reparse escape and exact allowlisted relative path rejection
  - byte/count limits fail closed before any formal artifact is published
  - SQLite online-backup consistency under concurrent WAL writes
  - declared table/column capability validation; credential/account/token/auth
    tables are never copied into the published artifact or reported
  - failure before publication leaves no artifact or manifest behind

All sources are synthetic fixtures under pytest tmp_path. No live data, no
user paths, no network, no provider calls (D-31, D-05, D-08).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources.snapshots import (
    CaptureError,
    CaptureManifest,
    CapturePolicy,
    capture_directory,
    capture_file,
    capture_sqlite,
    make_slot_artifact_id,
    read_manifest,
    replay_manifest,
    write_manifest,
)
from personal_knowledge.core.conversation_events import make_event_id


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _blob_root(dest: Path) -> Path:
    return dest / "artifacts"


def _make_sqlite_store(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, content TEXT)"
    )
    con.commit()
    return con


# --------------------------------------------------------------------------
# content-addressed file capture + dedup
# --------------------------------------------------------------------------


def test_file_capture_is_content_addressed_and_deduped(tmp_path: Path) -> None:
    src = tmp_path / "rollout.jsonl"
    _write(src, '{"type":"message"}\n')
    dest = tmp_path / "snap"
    # Milestone 1: pass family + a stable mirror path so artifact_id becomes a
    # per-source SLOT (sha256("art|" + family + "|" + mirror_path)) rather than a
    # content hash. The on-disk blob store stays content-addressed (named by
    # content_hash[:32]); byte-identical content still dedups there.
    art1, _ = capture_file(
        src, dest, relative_path="codex/rollout.jsonl",
        byte_limit=10_000, count_limit=100,
        family="codex", mirror_path="codex/rollout.jsonl",
    )
    art2, _ = capture_file(
        src, dest, relative_path="codex/rollout.jsonl",
        byte_limit=10_000, count_limit=100,
        family="codex", mirror_path="codex/rollout.jsonl",
    )
    # slot id is stable across content-identical captures
    assert art1.artifact_id == art2.artifact_id
    assert art1.content_hash == art2.content_hash
    assert art1.capture_method == "sha256"
    # dedup: exactly one content-addressed blob stored for the same content
    blob_ids = {p.name for p in _blob_root(dest).iterdir()}
    assert art1.content_hash[:32] in blob_ids
    assert len(blob_ids) == 1


def test_changed_source_detected_and_old_artifact_immutable(tmp_path: Path) -> None:
    src = tmp_path / "f.jsonl"
    _write(src, "v1\n")
    dest = tmp_path / "snap"
    # Milestone 1: with a stable slot identity, editing the source content rotates
    # content_hash (and the content-addressed blob) but NOT artifact_id. The old
    # assertion here was `art1.artifact_id != art2.artifact_id` (content-addressed
    # id); that expectation is now incorrect -- the slot id is constant across
    # edits, which is exactly what makes incremental sync possible.
    art1, _ = capture_file(
        src, dest, relative_path="f.jsonl", byte_limit=100_000, count_limit=100,
        family="codex", mirror_path="codex/f.jsonl",
    )
    _write(src, "v2\n")
    art2, _ = capture_file(
        src, dest, relative_path="f.jsonl", byte_limit=100_000, count_limit=100,
        family="codex", mirror_path="codex/f.jsonl",
    )
    assert art1.content_hash != art2.content_hash
    # slot id is STABLE despite the content change (the milestone-1 fix)
    assert art1.artifact_id == art2.artifact_id
    # both blobs remain on disk: capture is append-only / immutable at the blob layer
    blob_ids = {p.name for p in _blob_root(dest).iterdir()}
    assert art1.content_hash[:32] in blob_ids
    assert art2.content_hash[:32] in blob_ids
    # the slot id is NOT itself a content-addressed blob name
    assert art1.artifact_id not in blob_ids


# --------------------------------------------------------------------------
# manifest write / replay
# --------------------------------------------------------------------------


def test_manifest_write_and_replay_verify_artifacts(tmp_path: Path) -> None:
    src = tmp_path / "a.jsonl"
    _write(src, "data\n")
    dest = tmp_path / "snap"
    art, _ = capture_file(
        src, dest, relative_path="a.jsonl", byte_limit=100_000, count_limit=100,
    )
    manifest = CaptureManifest(
        manifest_id="m1",
        source_root=str(src.parent),
        capture_method="file",
        artifacts=(art,),
        policy=CapturePolicy(byte_limit=100_000, count_limit=100),
        schema_digest=None,
        privacy_dispositions=(),
        created_at="2026-08-12T00:00:00Z",
    )
    manifest_path = write_manifest(manifest, dest)
    loaded = read_manifest(manifest_path)
    assert loaded.manifest_id == "m1"
    assert loaded.capture_method == "file"
    assert loaded.artifacts[0].content_hash == art.content_hash
    replay = replay_manifest(loaded, _blob_root(dest))
    assert replay.ok
    assert replay.missing == []
    assert replay.mismatched == []


# --------------------------------------------------------------------------
# symlink / reparse / allowlisted path validation
# --------------------------------------------------------------------------


def test_symlink_reparse_escape_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside_secret.txt"
    _write(outside, "secret\n")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "leak.txt"
    created = False
    try:
        link.symlink_to(outside)
        created = True
    except (OSError, NotImplementedError):
        # Windows without Developer Mode: fall back to a junction, which is a
        # reparse point too and requires no privilege to create.
        try:
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
            created = True
        except (OSError, AttributeError, ImportError):
            pytest.skip("neither symlinks nor junctions available on this host")
    assert created, "escape fixture could not be created"
    with pytest.raises(CaptureError, match="symlink|reparse|junction"):
        capture_file(
            link, tmp_path / "snap", relative_path="leak.txt",
            byte_limit=100_000, count_limit=100,
        )
    # fail closed: no artifact published
    assert not _blob_root(tmp_path / "snap").exists()


def test_exact_allowlisted_relative_paths_reject_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / "ok.jsonl", "ok\n")
    _write(tmp_path / "outside.jsonl", "out\n")
    # parent traversal is rejected
    with pytest.raises(CaptureError, match="escape|relative"):
        capture_directory(
            root, tmp_path / "snap",
            include_relative=("../outside.jsonl",),
            byte_limit=100_000, count_limit=100,
        )
    # absolute path input is rejected
    with pytest.raises(CaptureError, match="escape|relative|absolute"):
        capture_directory(
            root, tmp_path / "snap",
            include_relative=(str(root / "ok.jsonl"),),
            byte_limit=100_000, count_limit=100,
        )
    assert not _blob_root(tmp_path / "snap").exists()


def test_directory_capture_allowlisted_paths_only(tmp_path: Path) -> None:
    root = tmp_path / "grok"
    root.mkdir()
    _write(root / "summary.json", "{}")
    _write(root / "transcript.jsonl", "line1\n")
    _write(root / "events.jsonl", "e1\n")
    _write(root / "token_secrets.txt", "should-not-be-captured")
    manifest, artifacts = capture_directory(
        root, tmp_path / "snap",
        include_relative=("summary.json", "transcript.jsonl", "events.jsonl"),
        byte_limit=100_000, count_limit=100,
    )
    paths = {a.relative_path for a in artifacts}
    assert paths == {"summary.json", "transcript.jsonl", "events.jsonl"}
    assert "token_secrets.txt" not in paths
    assert manifest.capture_method == "directory"
    assert len(manifest.artifacts) == 3


# --------------------------------------------------------------------------
# byte / count limits fail closed
# --------------------------------------------------------------------------


def test_byte_limit_fails_closed_before_publish(tmp_path: Path) -> None:
    src = tmp_path / "big.jsonl"
    _write(src, "x" * 5000)
    dest = tmp_path / "snap"
    with pytest.raises(CaptureError, match="byte"):
        capture_file(
            src, dest, relative_path="big.jsonl",
            byte_limit=100, count_limit=100,
        )
    assert not _blob_root(dest).exists()
    assert not (dest / "manifest.json").exists()


def test_count_limit_fails_closed_before_publish(tmp_path: Path) -> None:
    root = tmp_path / "dir"
    root.mkdir()
    _write(root / "a.jsonl", "a")
    _write(root / "b.jsonl", "b")
    with pytest.raises(CaptureError, match="count"):
        capture_directory(
            root, tmp_path / "snap",
            include_relative=("a.jsonl", "b.jsonl"),
            byte_limit=100_000, count_limit=1,
        )
    assert not _blob_root(tmp_path / "snap").exists()
    assert not (tmp_path / "snap" / "manifest.json").exists()


# --------------------------------------------------------------------------
# SQLite capture: WAL consistency + allowlist + forbidden tables
# --------------------------------------------------------------------------


def test_sqlite_capture_is_wal_consistent_under_concurrent_writes(
    tmp_path: Path,
) -> None:
    src = tmp_path / "live.sqlite"
    con = sqlite3.connect(str(src))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, content TEXT)"
    )
    for i in range(50):
        con.execute("INSERT INTO sessions VALUES (?, 'codex')", (f"s{i}",))
    con.commit()
    baseline = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    stop = threading.Event()

    def writer() -> None:
        i = 1000
        while not stop.is_set():
            try:
                con.execute(
                    "INSERT INTO messages VALUES (?, 's0', 'body')", (i,)
                )
                con.commit()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            i += 1

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        art, snap_path = capture_sqlite(
            src, tmp_path / "snap",
            allowed_tables=("sessions", "messages"),
            allowed_columns={
                "sessions": ("id", "agent"),
                "messages": ("id", "session_id", "content"),
            },
            byte_limit=1_000_000, count_limit=10_000,
        )
    finally:
        stop.set()
        thread.join()

    check = sqlite3.connect(str(snap_path))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count = check.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == baseline  # consistent snapshot, no torn rows
        assert art.content_hash
    finally:
        check.close()
    con.close()


def test_sqlite_capture_excludes_credential_tables(tmp_path: Path) -> None:
    src = tmp_path / "store.sqlite"
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    con.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, password TEXT)")
    con.execute("CREATE TABLE auth_tokens (id TEXT PRIMARY KEY, token TEXT)")
    con.execute("CREATE TABLE api_credentials (id TEXT PRIMARY KEY, secret TEXT)")
    con.commit()
    con.close()

    art, snap_path = capture_sqlite(
        src, tmp_path / "snap",
        allowed_tables=("sessions", "messages"),
        allowed_columns={"sessions": ("id",), "messages": ("id", "content")},
        byte_limit=1_000_000, count_limit=100,
    )
    check = sqlite3.connect(str(snap_path))
    try:
        tables = {
            r[0]
            for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        check.close()
    assert tables == {"sessions", "messages"}
    assert not any(
        any(part in t for part in ("account", "token", "credential", "auth"))
        for t in tables
    )
    # schema digest recorded, no body
    assert art.schema_digest
    # privacy dispositions are metadata-only but mention the excluded kinds
    joined = " ".join(art.privacy_dispositions).lower()
    assert any(part in joined for part in ("credential", "token", "account"))


def test_sqlite_missing_declared_column_fails_closed(tmp_path: Path) -> None:
    src = tmp_path / "store.sqlite"
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    dest = tmp_path / "snap"
    with pytest.raises(CaptureError, match="column|schema"):
        capture_sqlite(
            src, dest,
            allowed_tables=("sessions",),
            allowed_columns={"sessions": ("id", "agent")},
            byte_limit=1_000_000, count_limit=100,
        )
    assert not _blob_root(dest).exists()
    assert not (dest / "manifest.json").exists()


def test_sqlite_forbidden_table_in_allowlist_fails_closed(tmp_path: Path) -> None:
    src = tmp_path / "store.sqlite"
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, password TEXT)")
    con.commit()
    con.close()
    dest = tmp_path / "snap"
    with pytest.raises(CaptureError, match="forbidden|allowlist"):
        capture_sqlite(
            src, dest,
            allowed_tables=("accounts",),
            allowed_columns={"accounts": ("id",)},
            byte_limit=1_000_000, count_limit=100,
        )
    assert not _blob_root(dest).exists()


# --------------------------------------------------------------------------
# Milestone 1: stable per-source-slot identity (artifact_id is NOT content)
# --------------------------------------------------------------------------
#
# The core invariant: ``artifact_id`` is a stable per-(family, mirror-path)
# slot, while ``content_hash`` keeps holding ``sha256(bytes)``. Editing the
# bytes of a source must rotate the content hash but NOT the slot id, and two
# files that share a basename in different directories must get distinct slots.


def test_slot_artifact_id_is_deterministic_and_content_independent() -> None:
    family = "codex"
    mirror = "sessions/abc/rollout.jsonl"
    id1 = make_slot_artifact_id(family, mirror)
    id2 = make_slot_artifact_id(family, mirror)
    assert id1 == id2  # deterministic
    assert len(id1) == 64  # full sha256 hex digest
    # the slot id is independent of the captured bytes
    assert make_slot_artifact_id(family, mirror) == id1


def test_slot_identity_same_path_diff_bytes_same_id_diff_content(
    tmp_path: Path,
) -> None:
    family = "grok"
    mirror = "projects/x/conversations/1.jsonl"
    src = tmp_path / "src.jsonl"
    dest = tmp_path / "snap"
    src.write_text("v1\n", encoding="utf-8")
    art1, _ = capture_file(
        src, dest, relative_path="x/1.jsonl",
        byte_limit=100_000, count_limit=100,
        family=family, mirror_path=mirror,
    )
    src.write_text("v2 with totally different bytes\n", encoding="utf-8")
    art2, _ = capture_file(
        src, dest, relative_path="x/1.jsonl",
        byte_limit=100_000, count_limit=100,
        family=family, mirror_path=mirror,
    )
    # SAME slot identity despite DIFFERENT content
    assert art1.artifact_id == art2.artifact_id
    assert art1.content_hash != art2.content_hash
    # on-disk blob store stays content-addressed (named by content_hash[:32])
    blob_root = _blob_root(dest)
    assert (blob_root / art1.content_hash[:32]).exists()
    assert (blob_root / art2.content_hash[:32]).exists()
    # NOT named by the (stable) artifact_id
    assert not (blob_root / art1.artifact_id).exists()
    # both content versions survive: capture is append-only at the blob layer
    assert len(list(blob_root.iterdir())) == 2


def test_slot_identity_distinct_paths_same_basename_differ() -> None:
    family = "codex"
    id_a = make_slot_artifact_id(family, "sessions/A/rollout.jsonl")
    id_b = make_slot_artifact_id(family, "sessions/B/rollout.jsonl")
    assert id_a != id_b
    # same basename under a different family is still distinct
    id_c = make_slot_artifact_id("grok", "sessions/A/rollout.jsonl")
    assert id_c != id_a
    assert id_c != id_b


def test_slot_identity_capture_file_stable_id_ignores_content(
    tmp_path: Path,
) -> None:
    family = "codex"
    mirror = "codex/sessions/abc/r.jsonl"
    src = tmp_path / "r.jsonl"
    dest = tmp_path / "snap"
    src.write_text("alpha\n", encoding="utf-8")
    art1, _ = capture_file(
        src, dest, relative_path="codex/r.jsonl",
        byte_limit=100_000, count_limit=100,
        family=family, mirror_path=mirror,
    )
    src.write_text("beta\n", encoding="utf-8")
    art2, _ = capture_file(
        src, dest, relative_path="codex/r.jsonl",
        byte_limit=100_000, count_limit=100,
        family=family, mirror_path=mirror,
    )
    assert art1.artifact_id == art2.artifact_id
    assert art1.content_hash != art2.content_hash
    # the id is fully determined by (family, mirror_path)
    assert art1.artifact_id == make_slot_artifact_id(family, mirror)


def test_slot_identity_edit_does_not_rotate_event_or_session_id(
    tmp_path: Path,
) -> None:
    """The payoff of slot identity: a one-line edit must not re-key a whole file.

    All 12 conversation adapters derive ``session_id`` and ``event_id`` through
    ``make_event_id(family, artifact.artifact_id, contract_version, ...)``. While
    ``artifact_id`` was ``sha256(file bytes)[:32]``, editing one line rotated
    every downstream event/session id, which is what made incremental sync
    impossible (one 211,670-event SQLite source = 24.3 % of the corpus).
    """
    family = "codex"
    mirror = "sessions/abc/rollout.jsonl"
    src = tmp_path / "rollout.jsonl"
    dest = tmp_path / "snap"

    def _ids(text: str) -> tuple[str, str, str]:
        src.write_text(text, encoding="utf-8")
        artifact, _ = capture_file(
            src, dest, relative_path=src.name,
            byte_limit=100_000, count_limit=100,
            family=family, mirror_path=mirror,
        )
        return (
            make_event_id(family, artifact.artifact_id, "2", "native-session"),
            make_event_id(family, artifact.artifact_id, "2", "native-event-1"),
            artifact.content_hash,
        )

    before_session, before_event, before_hash = _ids("line one\n")
    after_session, after_event, after_hash = _ids("line one edited\n")

    assert before_hash != after_hash  # bytes did change
    assert before_session == after_session  # but identity did not rotate
    assert before_event == after_event


def test_live_sync_schema_is_additive_and_indexed(tmp_path: Path) -> None:
    from personal_knowledge.application.conversation.event_schema import create_v2_schema

    db = tmp_path / "v2.db"
    create_v2_schema(db)
    con = sqlite3.connect(str(db))
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'ix_%'"
            )
        }
        # Only asserting table *names* used to be enough to pass while the live
        # tables were still missing the slot-uniqueness guarantee and the
        # sync-log counters the live engine records. Assert the columns the
        # milestone-1 contract specifies, so a shape regression cannot hide
        # behind a name check. (Expectation widened, not weakened: the previous
        # name assertions below are all still enforced.)
        columns = {
            table: {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for table in ("ce_live_slots", "ce_live_sync_log", "ce_live_state")
        }
    finally:
        con.close()
    for table in ("ce_live_slots", "ce_live_sync_log", "ce_live_state"):
        assert table in tables
    for index in (
        "ix_ce_events_gen_art",
        "ix_ce_sessions_gen_art",
        "ix_ce_rel_gen_target",
    ):
        assert index in indexes
    assert columns["ce_live_slots"] == {
        "slot_id", "family", "mirror_path", "content_hash", "byte_size",
        "first_seen_at", "last_synced_at", "active",
    }
    assert columns["ce_live_sync_log"] == {
        "sync_id", "started_at", "finished_at", "n_added", "n_changed",
        "n_removed", "rows_pruned", "rows_inserted", "per_family", "status",
        "detail",
    }
    assert columns["ce_live_state"] == {"key", "value"}
    # idempotent re-apply is safe and preserves the tables/indexes
    create_v2_schema(db)


def test_live_slots_unique_per_family_and_mirror_path(tmp_path: Path) -> None:
    """A slot id must be unambiguous: one (family, mirror_path) -> one slot.

    This is the storage-level half of the slot-identity invariant
    ``artifact_id = sha256("art|" + family + "|" + mirror_path)``; without the
    UNIQUE constraint two rows could claim the same mirror path with different
    slot ids and the live engine would not know which one to replace.
    """
    import hashlib

    from personal_knowledge.application.conversation.event_schema import create_v2_schema

    db = tmp_path / "v2.db"
    create_v2_schema(db)
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            "INSERT INTO ce_live_slots"
            "(slot_id, family, mirror_path, content_hash, byte_size, first_seen_at)"
            " VALUES (?,?,?,?,?,?)",
            ("slot-a", "codex", "sessions/abc/rollout.jsonl", "h1", 10, "2026-01-01T00:00:00Z"),
        )
        # a different family + the same mirror path is a different slot
        con.execute(
            "INSERT INTO ce_live_slots"
            "(slot_id, family, mirror_path, content_hash, byte_size, first_seen_at)"
            " VALUES (?,?,?,?,?,?)",
            ("slot-b", "grok", "sessions/abc/rollout.jsonl", "h1", 10, "2026-01-01T00:00:00Z"),
        )
        # same (family, mirror_path) as the first row: must be rejected
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO ce_live_slots"
                "(slot_id, family, mirror_path, content_hash, byte_size, first_seen_at)"
                " VALUES (?,?,?,?,?,?)",
                ("slot-c", "codex", "sessions/abc/rollout.jsonl", "h2", 11, "2026-01-01T00:00:00Z"),
            )
        # content_hash / byte_size / last_synced_at may be NULL until first sync
        con.execute(
            "INSERT INTO ce_live_slots"
            "(slot_id, family, mirror_path, first_seen_at) VALUES (?,?,?,?)",
            ("slot-d", "codex", "sessions/zzz/new.jsonl", "2026-01-01T00:00:00Z"),
        )
        # `active` defaults to 1 so a freshly seen slot is live by default
        active, = con.execute(
            "SELECT active FROM ce_live_slots WHERE slot_id='slot-d'"
        ).fetchone()
        assert active == 1
        # the stored slot id is exactly the documented slot derivation
        assert hashlib.sha256(
            b"art|codex|sessions/abc/rollout.jsonl"
        ).hexdigest() == make_slot_artifact_id(
            "codex", "sessions/abc/rollout.jsonl"
        )
    finally:
        con.close()
