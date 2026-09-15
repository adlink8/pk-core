"""Pathless ChatGPT compatibility observations through the live shadow seam."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.conversation import chatgpt_snapshot
from personal_knowledge.application.conversation import (
    agentsview_unavailable_snapshot,
)
from personal_knowledge.adapters.conversation_sources.snapshots import CaptureError
from personal_knowledge.application.conversation.live_native_shadow import (
    build_live_native_shadow,
)
from personal_knowledge.core.project_paths import ROOT


def _agentsview_fixture(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                deleted_at TEXT,
                file_path TEXT,
                account_email TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp TEXT,
                is_system INTEGER NOT NULL DEFAULT 0,
                is_sidechain INTEGER NOT NULL DEFAULT 0,
                thinking_text TEXT,
                token_usage TEXT,
                undeclared_marker TEXT
            );
            CREATE TABLE auth_tokens (
                id TEXT PRIMARY KEY,
                token_value TEXT NOT NULL
            );
            """
        )
        con.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "chat-1", "chatgpt", "2026-08-01T01:00:00Z",
                    "2026-08-01T01:05:00Z", None, None,
                    "undeclared-account@example.invalid",
                ),
                (
                    "chat-2", "chatgpt", "2026-08-02T02:00:00Z",
                    "2026-08-02T02:05:00Z", None, None,
                    "undeclared-account-2@example.invalid",
                ),
                (
                    "grok-1", "grok", "2026-08-02T04:00:00Z",
                    "2026-08-02T04:05:00Z", None,
                    "X:/retired-grok/grok-1/summary.md",
                    "undeclared-grok-account@example.invalid",
                ),
                (
                    "grok-2", "grok", "2026-08-02T05:00:00Z",
                    "2026-08-02T05:05:00Z", None,
                    "X:/retired-grok/grok-2/summary.md",
                    "undeclared-grok-account-2@example.invalid",
                ),
                (
                    "other-1", "claude", "2026-08-03T03:00:00Z",
                    "2026-08-03T03:05:00Z", None, None,
                    "undeclared-other-account@example.invalid",
                ),
            ],
        )
        con.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "msg-1", "chat-1", 1, "user", "pathless user body",
                    "2026-08-01T01:00:01Z", 0, 0,
                    "private thinking must stay out", "9999",
                    "undeclared-message-marker",
                ),
                (
                    "msg-2", "chat-1", 2, "assistant", "pathless assistant body",
                    "2026-08-01T01:00:02Z", 0, 0,
                    "private thinking must stay out", "9999",
                    "undeclared-message-marker",
                ),
                (
                    "msg-3", "chat-2", 1, "user", "second session body",
                    "2026-08-02T02:00:01Z", 0, 0,
                    "private thinking must stay out", "9999",
                    "undeclared-message-marker",
                ),
                (
                    "grok-msg-1", "grok-1", 1, "user",
                    "pathless grok user body",
                    "2026-08-02T04:00:01Z", 0, 0,
                    "private grok thinking must stay out", "7777",
                    "undeclared-grok-message-marker",
                ),
                (
                    "grok-msg-2", "grok-1", 2, "assistant",
                    "pathless grok assistant body",
                    "2026-08-02T04:00:02Z", 0, 0,
                    "private grok thinking must stay out", "7777",
                    "undeclared-grok-message-marker",
                ),
                (
                    "grok-msg-3", "grok-2", 1, "assistant",
                    "second grok session body",
                    "2026-08-02T05:00:01Z", 0, 0,
                    "private grok thinking must stay out", "7777",
                    "undeclared-grok-message-marker",
                ),
                (
                    "other-msg-1", "other-1", 1, "user",
                    "non-chatgpt-secret-marker-must-not-be-captured",
                    "2026-08-03T03:00:01Z", 0, 0,
                    "private other thinking must stay out", "8888",
                    "undeclared-other-message-marker",
                ),
            ],
        )
        con.execute("INSERT INTO auth_tokens VALUES ('auth-1','forbidden-token-marker')")
        con.commit()
    finally:
        con.close()


def test_live_shadow_adapts_each_pathless_chatgpt_session_and_message(tmp_path: Path) -> None:
    agentsview_db = tmp_path / "sessions.db"
    event_db = tmp_path / "canonical.sqlite"
    artifact_store = tmp_path / "artifact-store"
    _agentsview_fixture(agentsview_db)

    report = build_live_native_shadow(
        agentsview_db=agentsview_db,
        db=event_db,
        artifact_store=artifact_store,
        report_path=tmp_path / "report.json",
    )

    chatgpt = report["generations"]["chatgpt"]
    assert (chatgpt["session_count"], chatgpt["event_count"]) == (2, 5)

    con = sqlite3.connect(event_db)
    try:
        generation_id = report["generation_id"]
        rows = con.execute(
            "SELECT e.kind, e.content, e.summary, e.native_locator FROM ce_events e "
            "JOIN ce_sessions s ON s.generation_id=e.generation_id "
            "AND s.session_id=e.session_id "
            "WHERE e.generation_id=? AND s.family='chatgpt' "
            "AND e.kind IN ('user_message','assistant_message') "
            "ORDER BY e.native_locator",
            (generation_id,),
        ).fetchall()
    finally:
        con.close()
    assert [(kind, content) for kind, content, _summary, _locator in rows] == [
        ("user_message", "pathless user body"),
        ("assistant_message", "pathless assistant body"),
        ("user_message", "second session body"),
    ]
    assert all(summary is None for _kind, _content, summary, _locator in rows)

    # Expectation changed: the blob store is content-addressed, so a captured
    # file is named ``content_hash[:32]``. ``artifact_id`` is now the stable
    # per-slot identity (sha256("art|<family>|<mirror path>")), so using it as
    # the file name here resolved to a non-existent blob and the test failed
    # with "unable to open database file".
    content_hash = chatgpt["artifact_refs"][0]["content_hash"]
    captured = artifact_store / "artifacts" / content_hash[:32]
    snapshot = sqlite3.connect(f"file:{captured.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in snapshot.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        columns = {
            table: {row[1] for row in snapshot.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
        captured_sessions = snapshot.execute(
            "SELECT id, agent FROM sessions ORDER BY id"
        ).fetchall()
        captured_messages = snapshot.execute(
            "SELECT id, session_id, content FROM messages ORDER BY id"
        ).fetchall()
    finally:
        snapshot.close()
    assert tables == {"sessions", "messages"}
    assert columns == {
        "sessions": {
            "id", "agent", "started_at", "ended_at", "deleted_at", "file_path",
        },
        "messages": {
            "id", "session_id", "ordinal", "role", "content", "timestamp",
            "is_system", "is_sidechain",
        },
    }
    assert captured_sessions == [("chat-1", "chatgpt"), ("chat-2", "chatgpt")]
    assert len(captured_messages) == 3
    assert all(session_id in {"chat-1", "chat-2"} for _id, session_id, _body in captured_messages)
    assert all(
        "non-chatgpt-secret-marker" not in (body or "")
        for _id, _session_id, body in captured_messages
    )
    artifact_bytes = captured.read_bytes()
    assert b"non-chatgpt-secret-marker-must-not-be-captured" not in artifact_bytes
    assert b"private thinking must stay out" not in artifact_bytes
    assert b"forbidden-token-marker" not in artifact_bytes


def test_live_shadow_adapts_pathless_grok_through_filtered_observation(
    tmp_path: Path,
) -> None:
    agentsview_db = tmp_path / "sessions.db"
    event_db = tmp_path / "canonical.sqlite"
    artifact_store = tmp_path / "artifact-store"
    _agentsview_fixture(agentsview_db)

    report = build_live_native_shadow(
        agentsview_db=agentsview_db,
        db=event_db,
        artifact_store=artifact_store,
        report_path=tmp_path / "report.json",
    )

    grok = report["generations"]["grok"]
    assert grok["status"] == "partial"
    assert (grok["session_count"], grok["event_count"]) == (2, 5)
    assert grok["native_snapshot_count"] == 0
    assert grok["compatibility_observation_count"] == 1
    # The existing ChatGPT compatibility observation remains unchanged.
    assert (
        report["generations"]["chatgpt"]["session_count"],
        report["generations"]["chatgpt"]["event_count"],
    ) == (2, 5)

    con = sqlite3.connect(event_db)
    try:
        rows = con.execute(
            "SELECT e.kind, e.content FROM ce_events e "
            "JOIN ce_sessions s ON s.generation_id=e.generation_id "
            "AND s.session_id=e.session_id "
            "WHERE e.generation_id=? AND s.family='grok' "
            "AND e.kind IN ('user_message','assistant_message') "
            "ORDER BY e.native_locator",
            (report["generation_id"],),
        ).fetchall()
    finally:
        con.close()
    assert rows == [
        ("user_message", "pathless grok user body"),
        ("assistant_message", "pathless grok assistant body"),
        ("assistant_message", "second grok session body"),
    ]

    # blobs are addressed by content_hash[:32], not by the slot artifact_id
    content_hash = grok["artifact_refs"][0]["content_hash"]
    captured = artifact_store / "artifacts" / content_hash[:32]
    snapshot = sqlite3.connect(f"file:{captured.as_posix()}?mode=ro", uri=True)
    try:
        sessions = snapshot.execute(
            "SELECT id, agent FROM sessions ORDER BY id"
        ).fetchall()
        messages = snapshot.execute(
            "SELECT id, session_id, content FROM messages ORDER BY id"
        ).fetchall()
        columns = {
            table: {row[1] for row in snapshot.execute(f"PRAGMA table_info({table})")}
            for table in ("sessions", "messages")
        }
    finally:
        snapshot.close()
    assert sessions == [("grok-1", "grok"), ("grok-2", "grok")]
    assert len(messages) == 3
    assert all(session_id in {"grok-1", "grok-2"} for _id, session_id, _ in messages)
    assert columns == {
        "sessions": {
            "id", "agent", "started_at", "ended_at", "deleted_at", "file_path",
        },
        "messages": {
            "id", "session_id", "ordinal", "role", "content", "timestamp",
            "is_system", "is_sidechain",
        },
    }
    artifact_bytes = captured.read_bytes()
    assert b"pathless user body" not in artifact_bytes
    assert b"private grok thinking must stay out" not in artifact_bytes
    assert b"forbidden-token-marker" not in artifact_bytes


def test_live_shadow_merges_native_and_pathless_grok_without_counting_observation_as_native(
    tmp_path: Path,
) -> None:
    agentsview_db = tmp_path / "sessions.db"
    native_summary = tmp_path / "native-grok" / "summary.md"
    native_summary.parent.mkdir()
    native_summary.write_text(
        "# Summary\ngrok_session_native\nnative summary\n", encoding="utf-8"
    )
    _agentsview_fixture(agentsview_db)
    con = sqlite3.connect(agentsview_db)
    try:
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
            (
                "grok-native", "grok", "2026-08-03T06:00:00Z",
                "2026-08-03T06:05:00Z", None, str(native_summary),
                "undeclared-native-account@example.invalid",
            ),
        )
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "grok-native-msg", "grok-native", 1, "user",
                "available-native-grok-secret-must-not-enter-observation",
                "2026-08-03T06:00:01Z", 0, 0,
                "available-native-private-thinking", "1234",
                "available-native-undeclared-marker",
            ),
        )
        con.commit()
    finally:
        con.close()

    report = build_live_native_shadow(
        agentsview_db=agentsview_db,
        db=tmp_path / "canonical.sqlite",
        artifact_store=tmp_path / "artifact-store",
        report_path=tmp_path / "report.json",
    )

    grok = report["generations"]["grok"]
    assert (grok["session_count"], grok["event_count"]) == (3, 6)
    assert grok["snapshot_count"] == 2
    assert grok["native_snapshot_count"] == 1
    assert grok["compatibility_observation_count"] == 1
    assert report["gates"]["all_available_files_captured"] is True
    assert report["gates"]["all_unavailable_sessions_observed"] is True
    assert report["gates"]["overall"] is True
    observation = next(
        ref for ref in grok["artifact_refs"] if ref["source_kind"] == "sqlite"
    )
    # blob id = content_hash[:32]; artifact_id is the slot identity (see above)
    observation_bytes = (
        tmp_path / "artifact-store" / "artifacts"
        / observation["content_hash"][:32]
    ).read_bytes()
    assert b"available-native-grok-secret-must-not-enter-observation" not in observation_bytes
    assert b"available-native-private-thinking" not in observation_bytes


def test_chatgpt_capture_never_stages_full_source_and_cleans_failed_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agentsview_db = tmp_path / "sessions.db"
    artifact_store = tmp_path / "artifact-store"
    _agentsview_fixture(agentsview_db)
    source_digest = hashlib.sha256(agentsview_db.read_bytes()).hexdigest()
    secret = b"non-chatgpt-secret-marker-must-not-be-captured"
    project_temp = tmp_path / "project" / "var" / "tmp" / "conversation-capture"
    monkeypatch.setattr(
        agentsview_unavailable_snapshot, "_CAPTURE_TEMP_ROOT", project_temp
    )

    # The byte policy fails naturally only after the filtered SQLite has been
    # built, exercising cleanup without a private hook or implementation mock.
    with pytest.raises(CaptureError, match="exceeds byte_limit"):
        chatgpt_snapshot.capture_chatgpt_snapshot(
            agentsview_db,
            artifact_store / "chatgpt",
            byte_limit=1,
        )

    assert project_temp.exists()
    assert not any(project_temp.iterdir())
    assert not any(
        secret in candidate.read_bytes()
        for candidate in artifact_store.rglob("*")
        if candidate.is_file()
    )
    assert hashlib.sha256(agentsview_db.read_bytes()).hexdigest() == source_digest


def test_live_capture_default_temp_root_is_inside_project() -> None:
    capture_root = agentsview_unavailable_snapshot._CAPTURE_TEMP_ROOT.resolve()
    assert capture_root.is_relative_to(ROOT.resolve())
