"""Phase 62 discovery seam: client-directory discovery + incremental staging.

Red test first (engineering contract): the client discovery layer must
(a) map each registered family to machine-local candidate roots,
(b) probe files with the owning family detector (reusing registry.detect_family,
    never a second parser), and (c) stage only new/changed files into a
    shadow-compatible source root, deduped by content hash.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.adapters.conversation_sources.discovery import (
    FAMILY_CLIENT_ROOTS,
    discover_client_sources,
    probe_source_kind,
    stage_client_sources,
)


def _write_codex_jsonl(root: Path, name: str, first_role: str = "user") -> Path:
    path = root / "sessions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "session_id": "s1", "timestamp": 1})
        + "\n"
        + json.dumps(
            {"type": "response_item", "session_id": "s1",
             "payload": {"role": first_role, "content": [{"type": "text", "text": "hi"}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_cursor_sqlite(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE threads (id TEXT, title TEXT, created_at TEXT)")
        con.execute("CREATE TABLE messages (id TEXT, session_id TEXT, role TEXT, content TEXT, created_at TEXT)")
        con.execute("INSERT INTO threads VALUES ('t1','demo','2026-01-01T00:00:00Z')")
        con.commit()
    finally:
        con.close()
    return path


def test_family_client_roots_are_explicit() -> None:
    """Every registered family that has native directories carries candidates."""
    from personal_knowledge.adapters.conversation_sources.registry import known_families

    for family in known_families():
        # chatgpt is manual-import (no native root) and must be declared as such.
        assert family in FAMILY_CLIENT_ROOTS, f"{family} missing in FAMILY_CLIENT_ROOTS"


def test_probe_source_kind_detects_sqlite_and_file(tmp_path: Path) -> None:
    db = _write_cursor_sqlite(tmp_path / "store.sqlite")
    txt = tmp_path / "note.txt"
    txt.write_text("hello", encoding="utf-8")
    assert probe_source_kind(db) == "sqlite"
    assert probe_source_kind(txt) == "file"


def test_discover_codex_and_cursor_from_client_roots(tmp_path: Path) -> None:
    codex_file = _write_codex_jsonl(tmp_path / "home" / ".codex", "s1.jsonl")
    cursor_db = _write_cursor_sqlite(tmp_path / "home" / ".cursor" / "project.db")

    roots = {
        "codex": (tmp_path / "home" / ".codex",),
        "cursor": (tmp_path / "home" / ".cursor",),
    }
    found = discover_client_sources(roots=roots)
    assert "codex" in found
    assert any(p == codex_file for p in found["codex"])
    assert "cursor" in found
    assert any(p == cursor_db for p in found["cursor"])


def test_discovery_ignores_foreign_files(tmp_path: Path) -> None:
    (tmp_path / "home" / ".codex").mkdir(parents=True)
    foreign = tmp_path / "home" / ".codex" / "config.toml"
    foreign.write_text("model = 'x'", encoding="utf-8")
    found = discover_client_sources(
        roots={"codex": (tmp_path / "home" / ".codex",)}
    )
    assert found.get("codex", []) == []


def test_stage_incremental_deduplicates_by_hash(tmp_path: Path) -> None:
    src = _write_codex_jsonl(tmp_path / "home" / ".codex", "s1.jsonl")
    stage = tmp_path / "stage"
    roots = {"codex": (tmp_path / "home" / ".codex",)}

    first = stage_client_sources(roots=roots, stage_root=stage,
                                 byte_limit=10_000, count_limit=100)
    assert first["staged"] == 1
    assert first["skipped"] == 0
    assert (stage / "codex" / "sessions" / "s1.jsonl").exists()

    second = stage_client_sources(roots=roots, stage_root=stage,
                                  byte_limit=10_000, count_limit=100)
    assert second["staged"] == 0
    assert second["skipped"] == 1

    # mutate -> staged again
    src.write_text(src.read_text(encoding="utf-8") + json.dumps({"x": 1}) + "\n", encoding="utf-8")
    third = stage_client_sources(roots=roots, stage_root=stage,
                                 byte_limit=10_000, count_limit=100)
    assert third["staged"] == 1


def test_stage_sqlite_uses_wal_safe_snapshot(tmp_path: Path) -> None:
    """SQLite family sources stage via online-backup snapshot, not loose copy."""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        SQLITE_ALLOWLISTS,
        snapshot_sqlite_to_file,
    )

    db = tmp_path / "home" / ".zcode" / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, time_compacting TEXT, trace_id TEXT, directory TEXT, path TEXT)")
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER)")
        con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER)")
        con.execute("CREATE TABLE credentials (token TEXT)")  # forbidden adjacency
        con.execute("INSERT INTO session VALUES ('s1', NULL, 'demo', 1, 1, NULL, 't1', NULL, NULL)")
        con.execute("INSERT INTO credentials VALUES ('sk-secret')")
        con.commit()
    finally:
        con.close()

    tables, columns = SQLITE_ALLOWLISTS["zcode"]
    target = tmp_path / "stage" / "zcode" / "db.sqlite"
    digest = snapshot_sqlite_to_file(db, target, allowed_tables=tables, allowed_columns=columns)

    # snapshot is a valid sqlite store, credential table excluded
    check = sqlite3.connect(target)
    try:
        tables_now = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "credentials" not in tables_now, "forbidden table leaked into snapshot"
        assert {"session", "message", "part"} <= tables_now
        assert check.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
    finally:
        check.close()
    assert isinstance(digest, str) and len(digest) == 64
    # capture intermediates must not land in the stage tree: the tree is
    # re-scanned on the next run, so a leaked temp would be re-adapted as a
    # second copy of the same trajectory and collide on event ids.
    assert [p.name for p in target.parent.iterdir() if p.name.startswith(".")] == []
