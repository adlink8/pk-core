"""Phase 62-03: per-family SQLite / directory / partial-source adapter contracts.

Drives the real capture seam (:func:`capture_sqlite` / :func:`capture_directory`)
plus each family's ``detect``/``adapt`` boundary. SQLite fixtures are built
dynamically in ``tmp_path`` and deliberately contain BOTH conversation tables
and canary credential/account/token/auth tables — the privacy boundary is
asserted here and in ``tests/security/test_conversation_source_privacy.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import (
    antigravity,
    chatgpt,
    cursor,
    grok,
    mimo_opencode,
    zcode,
)
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.snapshots import (
    CaptureError,
    capture_directory,
    capture_file,
    capture_sqlite,
)
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
    RelationKind,
)

CANARY_VALUE = "canary-secret-value-314159"


def _make_zcode_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE conversation_traces (
                trace_id TEXT PRIMARY KEY, title TEXT, created_at TEXT
            );
            CREATE TABLE conversation_parts (
                part_id TEXT PRIMARY KEY, trace_id TEXT, turn_id TEXT,
                part_type TEXT, role TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE auth_tokens (
                token_id TEXT PRIMARY KEY, token_value TEXT
            );
            CREATE TABLE accounts (
                account_id TEXT PRIMARY KEY, email TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO conversation_traces VALUES (?, ?, ?)",
            ("tr_1", "zcode session", "2026-07-01T10:00:00Z"),
        )
        rows = [
            ("p1", "tr_1", "tn_1", "text", "user", "zcode prompt", "2026-07-01T10:00:01Z"),
            ("p2", "tr_1", "tn_1", "reasoning", "assistant", "thinking", "2026-07-01T10:00:02Z"),
            ("p3", "tr_1", "tn_1", "text", "assistant", "zcode answer", "2026-07-01T10:00:03Z"),
            ("p4", "tr_1", "tn_1", "tool", "assistant", "bash ls", "2026-07-01T10:00:04Z"),
            ("p5", "tr_1", "tn_1", "compaction", "assistant", "compacted", "2026-07-01T10:00:05Z"),
        ]
        con.executemany(
            "INSERT INTO conversation_parts VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        con.execute("INSERT INTO auth_tokens VALUES (?, ?)", ("tok_1", CANARY_VALUE))
        con.execute("INSERT INTO accounts VALUES (?, ?)", ("acc_1", "user@example.com"))
        con.commit()
    finally:
        con.close()


def _make_mimo_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, created_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, created_at TEXT
            );
            CREATE TABLE message_parts (
                id TEXT PRIMARY KEY, message_id TEXT, part_type TEXT,
                content TEXT, created_at TEXT
            );
            CREATE TABLE api_credentials (
                id TEXT PRIMARY KEY, api_key TEXT
            );
            """
        )
        con.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("s_1", "mimo session", "2026-07-01T10:00:00Z"))
        con.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", ("m1", "s_1", "user", "mimo prompt", "2026-07-01T10:00:01Z"))
        con.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", ("m2", "s_1", "assistant", "mimo answer", "2026-07-01T10:00:02Z"))
        con.execute("INSERT INTO message_parts VALUES (?, ?, ?, ?, ?)", ("mp1", "m2", "reasoning", "mimo thinking", "2026-07-01T10:00:03Z"))
        con.execute("INSERT INTO api_credentials VALUES (?, ?)", ("c1", CANARY_VALUE))
        con.commit()
    finally:
        con.close()


def _make_antigravity_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE trajectories (
                id TEXT PRIMARY KEY, name TEXT, created_at TEXT
            );
            CREATE TABLE steps (
                id TEXT PRIMARY KEY, trajectory_id TEXT, seq INTEGER,
                kind TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE subtrajectories (
                id TEXT PRIMARY KEY, step_id TEXT, parent_trajectory_id TEXT,
                content TEXT, created_at TEXT
            );
            CREATE TABLE session_tokens (
                id TEXT PRIMARY KEY, session_secret TEXT
            );
            """
        )
        con.execute("INSERT INTO trajectories VALUES (?, ?, ?)", ("t_1", "antigravity run", "2026-07-01T10:00:00Z"))
        con.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", ("st1", "t_1", 1, "user", "antigravity prompt", "2026-07-01T10:00:01Z"))
        con.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", ("st2", "t_1", 2, "assistant", "antigravity answer", "2026-07-01T10:00:02Z"))
        con.execute("INSERT INTO subtrajectories VALUES (?, ?, ?, ?, ?)", ("sub1", "st2", "t_1", "sub task", "2026-07-01T10:00:03Z"))
        con.execute("INSERT INTO session_tokens VALUES (?, ?)", ("tok_1", CANARY_VALUE))
        con.commit()
    finally:
        con.close()


def _capture_sqlite(db: Path, tmp_path: Path, *, allowed_tables, allowed_columns):
    artifact, blob = capture_sqlite(
        db, tmp_path, allowed_tables=allowed_tables,
        allowed_columns=allowed_columns, byte_limit=1_000_000, count_limit=8,
    )
    return artifact, blob.parent


def _sqlite_blob(artifact, root: Path) -> Path:
    return root / artifact.artifact_id


# ----------------------------------------------------------------------------- ZCode

class TestZCode:
    ALLOWED_TABLES = ("conversation_traces", "conversation_parts")
    ALLOWED_COLUMNS = {
        "conversation_traces": ("trace_id", "title", "created_at"),
        "conversation_parts": ("part_id", "trace_id", "turn_id", "part_type",
                               "role", "content", "created_at"),
    }

    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("zcode")
        db = tmp / "zcode.db"
        _make_zcode_db(db)
        artifact, root = _capture_sqlite(
            db, tmp, allowed_tables=self.ALLOWED_TABLES, allowed_columns=self.ALLOWED_COLUMNS,
        )
        return zcode.adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=root), artifact, root

    def test_detect(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("zcode-detect")
        db = tmp / "zcode.db"
        _make_zcode_db(db)
        artifact, root = _capture_sqlite(
            db, tmp, allowed_tables=self.ALLOWED_TABLES, allowed_columns=self.ALLOWED_COLUMNS,
        )
        assert zcode.detect(artifact, artifact_root=root) is True

    def test_family_and_kinds(self, adapted):
        result, _a, _r = adapted
        assert result.family == "zcode"
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.REASONING in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.COMPACTION_SUMMARY in kinds

    def test_trace_and_turn_preserved(self, adapted):
        result, _a, _r = adapted
        assert len(result.sessions) == 1
        assert result.sessions[0].native_session_id == "tr_1"
        turn_rels = [r for r in result.relations if r.relation_kind is RelationKind.TURN_MEMBERSHIP]
        assert len(turn_rels) >= 1

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        result, _a, _r = adapted
        message = next(
            event for event in result.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "zcode prompt"
        assert message.summary is None

    def test_canary_never_in_events(self, adapted):
        result, _a, _r = adapted
        blob_text = " ".join(
            value
            for event in result.events
            for value in (event.content, event.summary)
            if value
        )
        assert CANARY_VALUE not in blob_text

    def test_privacy_dispositions_record_exclusions(self, adapted):
        _result, artifact, _r = adapted
        exclusions = [d for d in artifact.privacy_dispositions if d.startswith("excluded_table:")]
        assert any("auth_tokens" in d for d in exclusions)
        assert any("accounts" in d for d in exclusions)


# ------------------------------------------------------------- MimoCode / OpenCode

class TestMimoOpenCode:
    ALLOWED_TABLES = ("sessions", "messages", "message_parts")
    ALLOWED_COLUMNS = {
        "sessions": ("id", "title", "created_at"),
        "messages": ("id", "session_id", "role", "content", "created_at"),
        "message_parts": ("id", "message_id", "part_type", "content", "created_at"),
    }

    @pytest.fixture(scope="class", params=["mimo", "opencode"])
    def adapted(self, tmp_path_factory, request):
        family = request.param
        tmp = tmp_path_factory.mktemp(family)
        db = tmp / "store.db"
        _make_mimo_db(db)
        artifact, root = _capture_sqlite(
            db, tmp, allowed_tables=self.ALLOWED_TABLES, allowed_columns=self.ALLOWED_COLUMNS,
        )
        adapt = mimo_opencode.adapt_family(family)
        return adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=root), artifact, family

    def test_detect(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("mimo-detect")
        db = tmp / "store.db"
        _make_mimo_db(db)
        artifact, root = _capture_sqlite(
            db, tmp, allowed_tables=self.ALLOWED_TABLES, allowed_columns=self.ALLOWED_COLUMNS,
        )
        assert mimo_opencode.detect(artifact, artifact_root=root) is True

    def test_family(self, adapted):
        _result, _a, family = adapted
        assert family in ("mimo", "opencode")

    def test_message_relations(self, adapted):
        result, _a, _f = adapted
        assert len(result.sessions) == 1
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.REASONING in kinds

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        result, _a, _family = adapted
        message = next(
            event for event in result.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "mimo prompt"
        assert message.summary is None

    def test_canary_never_in_events(self, adapted):
        result, _a, _f = adapted
        blob_text = " ".join(
            value
            for event in result.events
            for value in (event.content, event.summary)
            if value
        )
        assert CANARY_VALUE not in blob_text


# ----------------------------------------------------------------------- Antigravity

class TestAntigravity:
    ALLOWED_TABLES = ("trajectories", "steps", "subtrajectories")
    ALLOWED_COLUMNS = {
        "trajectories": ("id", "name", "created_at"),
        "steps": ("id", "trajectory_id", "seq", "kind", "content", "created_at"),
        "subtrajectories": ("id", "step_id", "parent_trajectory_id", "content", "created_at"),
    }

    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("antigravity")
        db = tmp / "trajectory.db"
        _make_antigravity_db(db)
        artifact, root = _capture_sqlite(
            db, tmp, allowed_tables=self.ALLOWED_TABLES, allowed_columns=self.ALLOWED_COLUMNS,
        )
        return antigravity.adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=root)

    def test_detect(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("antigravity-detect")
        db = tmp / "trajectory.db"
        _make_antigravity_db(db)
        artifact, root = _capture_sqlite(
            db, tmp, allowed_tables=self.ALLOWED_TABLES, allowed_columns=self.ALLOWED_COLUMNS,
        )
        assert antigravity.detect(artifact, artifact_root=root) is True

    def test_hierarchy_relations(self, adapted):
        result = adapted
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        rels = {r.relation_kind for r in result.relations}
        assert RelationKind.PARENT_CHILD in rels

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        message = next(
            event for event in adapted.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "antigravity prompt"
        assert message.summary is None

    def test_canary_never_in_events(self, adapted):
        result = adapted
        blob_text = " ".join(
            value
            for event in result.events
            for value in (event.content, event.summary)
            if value
        )
        assert CANARY_VALUE not in blob_text


# ----------------------------------------------------------------------------- Grok

def _make_grok_dir(base: Path) -> None:
    (base / "summary.md").write_text(
        "# Summary\ngrok_session_s1\nA grok session summary\n", encoding="utf-8"
    )
    (base / "chat_history.jsonl").write_text(
        '{"id":"g1","role":"user","content":"grok prompt","timestamp":"2026-07-01T10:00:00Z"}\n'
        '{"id":"g2","role":"assistant","content":"grok answer","timestamp":"2026-07-01T10:00:01Z"}\n',
        encoding="utf-8",
    )
    (base / "compaction.md").write_text(
        "# Compaction\nCompacted earlier turns.\n", encoding="utf-8"
    )
    (base / "subagents.json").write_text(
        json.dumps([{"id": "sub1", "name": "grok-sub", "created_at": "2026-07-01T10:00:02Z"}])
        + "\n",
        encoding="utf-8",
    )


class TestGrok:
    INCLUDE = ("summary.md", "chat_history.jsonl", "compaction.md", "subagents.json")

    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("grok")
        src = tmp / "src"
        src.mkdir()
        _make_grok_dir(src)
        manifest, artifacts = capture_directory(
            src, tmp, include_relative=self.INCLUDE, byte_limit=1_000_000, count_limit=8,
        )
        return grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts")

    def test_detect(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("grok-detect")
        src = tmp / "src"
        src.mkdir()
        _make_grok_dir(src)
        _manifest, artifacts = capture_directory(
            src, tmp, include_relative=self.INCLUDE, byte_limit=1_000_000, count_limit=8,
        )
        summary = next(a for a in artifacts if a.relative_path == "summary.md")
        assert grok.detect(summary, artifact_root=tmp / "artifacts") is True

    def test_family_and_kinds(self, adapted):
        result = adapted
        assert result.family == "grok"
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.COMPACTION_SUMMARY in kinds

    def test_cross_file_subagent_relation(self, adapted):
        result = adapted
        rels = [r for r in result.relations
                if r.relation_kind is RelationKind.SOURCE_SESSION_CROSSWALK]
        assert len(rels) == 1

    def test_full_directory_is_complete_fidelity(self, adapted):
        result = adapted
        assert result.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY) is FidelityLevel.COMPLETE

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        message = next(
            event for event in adapted.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "grok prompt"
        assert message.summary is None

    def test_summary_only_is_partial(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "summary.md").write_text("# Summary\ngrok_session_s2\n", encoding="utf-8")
        _manifest, artifacts = capture_directory(
            src, tmp_path, include_relative=("summary.md",),
            byte_limit=1_000_000, count_limit=4,
        )
        result = grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp_path / "artifacts")
        assert result.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY) is FidelityLevel.PARTIAL
        assert result.fidelity.has_loss()


def _make_typed_grok_dir(base: Path) -> None:
    """Native transcript shape: records tagged ``type``, content as typed parts.

    A real ``chat_history.jsonl`` opens with a multi-kilobyte system prompt and
    tags every record with ``type`` — never ``role``. The adapter once read this
    file through a ``role`` envelope and a 512-byte probe window, so the whole
    transcript was excluded at discovery time and the session degraded to a
    content-free summary shell.
    """
    (base / "summary.json").write_text(
        json.dumps({
            "info": {"id": "01a05b59-typed-0000-0000-000000000000", "cwd": "D:\\proj"},
            "session_summary": "typed transcript",
            "created_at": "2026-07-01T10:00:00Z",
            "updated_at": "2026-07-01T10:00:09Z",
            "num_messages": 5,
            "num_chat_messages": 4,
            "current_model_id": "grok-4.6-build",
        }),
        encoding="utf-8",
    )
    (base / "chat_history.jsonl").write_text(
        json.dumps({"type": "system", "content": "system preamble " * 200}) + "\n"
        + json.dumps({"type": "user", "content": [{"type": "text", "text": "typed prompt"}]}) + "\n"
        + json.dumps({
            "type": "assistant",
            "content": "typed answer",
            "tool_calls": [{
                "id": "call-1", "name": "read_file",
                "arguments": json.dumps({"target_file": "a.md"}),
            }],
            "model_id": "grok-4.6-build",
        }) + "\n"
        + json.dumps({"type": "tool_result", "tool_call_id": "call-1", "content": "file body"}) + "\n"
        + json.dumps({
            "type": "reasoning", "id": "rs-1",
            "summary": [{"type": "summary_text", "text": "why"}],
            "encrypted_content": "CIPHERTEXT",
        }) + "\n",
        encoding="utf-8",
    )


class TestGrokTypedTranscript:
    """Regression: a native ``type``-tagged transcript must not be dropped."""

    INCLUDE = ("summary.json", "chat_history.jsonl")

    @pytest.fixture(scope="class")
    def captured(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("grok-typed")
        src = tmp / "src"
        src.mkdir()
        _make_typed_grok_dir(src)
        _manifest, artifacts = capture_directory(
            src, tmp, include_relative=self.INCLUDE, byte_limit=1_000_000, count_limit=8,
        )
        return tmp, artifacts

    def test_detect_accepts_type_tagged_transcript(self, captured):
        tmp, artifacts = captured
        chat = next(a for a in artifacts if a.relative_path == "chat_history.jsonl")
        assert grok.detect(chat, artifact_root=tmp / "artifacts") is True

    def test_transcript_kinds_are_typed(self, captured):
        tmp, artifacts = captured
        result = grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts")
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.TOOL_RESULT in kinds
        assert EventKind.REASONING in kinds

    def test_parts_content_is_text_not_repr(self, captured):
        tmp, artifacts = captured
        result = grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts")
        prompt = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
        assert prompt.content == "typed prompt"

    def test_tool_call_and_result_are_linked(self, captured):
        tmp, artifacts = captured
        result = grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts")
        linked = [r for r in result.relations if r.relation_kind is RelationKind.CALL_RESULT]
        assert len(linked) == 1

    def test_encrypted_reasoning_keeps_summary_and_flags_ciphertext(self, captured):
        tmp, artifacts = captured
        result = grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts")
        reasoning = next(e for e in result.events if e.kind is EventKind.REASONING)
        assert reasoning.content == "why"
        fields = {d.field_name for d in reasoning.field_dispositions}
        assert "encrypted_content" in fields

    def test_full_transcript_is_complete_fidelity(self, captured):
        tmp, artifacts = captured
        result = grok.adapt(SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts")
        assert result.sessions[0].fidelity.level(
            FidelityDimension.CONTENT_AVAILABILITY
        ) is FidelityLevel.COMPLETE


class TestGrokPerFileAdaptation:
    """The v2 pipeline stages and adapts one file at a time, not the directory.

    ``_adapt_source_file`` hands the adapter a single-artifact set, so a lone
    ``chat_history.jsonl`` must still yield a self-consistent result: the
    generation writer enforces ``ce_events -> ce_sessions`` by foreign key, and
    a dangling session id aborts the whole cohort write.
    """

    def _adapt_lone_chat(self, tmp: Path):
        src = tmp / "src"
        src.mkdir()
        _make_typed_grok_dir(src)
        _manifest, artifacts = capture_directory(
            src, tmp, include_relative=("chat_history.jsonl",),
            byte_limit=1_000_000, count_limit=8,
        )
        return grok.adapt(
            SourceArtifactSet(artifacts=artifacts), artifact_root=tmp / "artifacts"
        )

    def test_transcript_only_set_still_has_a_session_record(self, tmp_path: Path):
        result = self._adapt_lone_chat(tmp_path)

        assert result.events, "isolated transcript should still emit events"
        assert result.sessions, "a transcript-only set still needs a session row"

    def test_every_event_session_is_backed_by_a_session_record(self, tmp_path: Path):
        result = self._adapt_lone_chat(tmp_path)

        known = {s.session_id for s in result.sessions}
        assert {e.session_id for e in result.events} <= known

    def test_backfilled_session_is_anchored_to_a_present_artifact(self, tmp_path: Path):
        result = self._adapt_lone_chat(tmp_path)

        artifact_ids = {a.artifact_id for a in result.artifacts}
        assert all(s.provenance.artifact_id in artifact_ids for s in result.sessions)

    def test_reused_reasoning_native_id_yields_unique_event_ids(self, tmp_path: Path):
        """Grok reuses one ``rs_...`` id across several reasoning rows."""
        src = tmp_path / "src"
        src.mkdir()
        row = {"type": "reasoning", "id": "rs-dup",
               "summary": [{"type": "summary_text", "text": "why"}]}
        (src / "chat_history.jsonl").write_text(
            json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8",
        )
        _manifest, artifacts = capture_directory(
            src, tmp_path, include_relative=("chat_history.jsonl",),
            byte_limit=1_000_000, count_limit=8,
        )
        result = grok.adapt(
            SourceArtifactSet(artifacts=artifacts), artifact_root=tmp_path / "artifacts"
        )

        assert len(result.events) == 2
        assert len({e.event_id for e in result.events}) == 2


# -------------------------------------------------------------------------- ChatGPT

class TestChatGPT:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("chatgpt")
        src = tmp / "sessions.db"
        con = sqlite3.connect(src)
        try:
            con.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, agent TEXT, started_at TEXT,
                    ended_at TEXT, deleted_at TEXT, file_path TEXT
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, session_id TEXT, ordinal INTEGER,
                    role TEXT, content TEXT, timestamp TEXT,
                    is_system INTEGER, is_sidechain INTEGER
                );
                """
            )
            con.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                ("chat-1", "chatgpt", "2026-07-01T10:00:00Z", None, None, None),
            )
            con.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                (
                    "message-1", "chat-1", 1, "user", "chatgpt prompt",
                    "2026-07-01T10:00:01Z", 0, 0,
                ),
            )
            con.commit()
        finally:
            con.close()
        artifact, blob = capture_sqlite(
            src, tmp,
            allowed_tables=chatgpt.LIVE_ALLOWED_TABLES,
            allowed_columns=chatgpt.LIVE_ALLOWED_COLUMNS,
            byte_limit=1_000_000, count_limit=2,
        )
        return chatgpt.adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=blob.parent)

    def test_family(self, adapted):
        result = adapted
        assert result.family == "chatgpt"

    def test_detect_binds_to_agentsview(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("chatgpt-detect")
        src = tmp / "sessions.db"
        con = sqlite3.connect(src)
        try:
            con.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT, agent TEXT, started_at TEXT, ended_at TEXT,
                    deleted_at TEXT, file_path TEXT
                );
                CREATE TABLE messages (
                    id TEXT, session_id TEXT, ordinal INTEGER, role TEXT,
                    content TEXT, timestamp TEXT, is_system INTEGER,
                    is_sidechain INTEGER
                );
                """
            )
            con.commit()
        finally:
            con.close()
        artifact, blob = capture_sqlite(
            src, tmp,
            allowed_tables=chatgpt.LIVE_ALLOWED_TABLES,
            allowed_columns=chatgpt.LIVE_ALLOWED_COLUMNS,
            byte_limit=1_000_000, count_limit=2,
        )
        assert chatgpt.detect(artifact, artifact_root=blob.parent) is True

    def test_native_reconstruction_unavailable(self, adapted):
        result = adapted
        assert result.fidelity.level(FidelityDimension.SOURCE_AVAILABILITY) is FidelityLevel.PARTIAL
        assert result.fidelity.level(FidelityDimension.STRUCTURE_COMPLETENESS) is FidelityLevel.PARTIAL
        assert any("native reconstruction unavailable" in w for w in result.warnings)

    def test_pathless_session_and_exact_compatibility_message(self, adapted):
        result = adapted
        assert len(result.sessions) == 1
        assert result.sessions[0].native_session_id == "chat-1"
        message = next(event for event in result.events if event.kind is EventKind.USER_MESSAGE)
        assert message.content == "chatgpt prompt"
        assert message.summary is None


# -------------------------------------------------------------------------- Cursor

def _make_cursor_db(path: Path, *, attribution_only: bool = False) -> None:
    con = sqlite3.connect(path)
    try:
        if attribution_only:
            con.execute("CREATE TABLE attribution (id TEXT PRIMARY KEY, name TEXT)")
            con.execute("INSERT INTO attribution VALUES ('a1', 'attribution-only')")
        else:
            con.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, created_at TEXT);
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at TEXT
                );
                """
            )
            con.execute("INSERT INTO threads VALUES ('t1', 'cursor thread', '2026-07-01T10:00:00Z')")
            con.execute("INSERT INTO messages VALUES ('m1', 'user', 'cursor prompt', '2026-07-01T10:00:01Z')")
            con.execute("INSERT INTO messages VALUES ('m2', 'assistant', 'cursor answer', '2026-07-01T10:00:02Z')")
        con.commit()
    finally:
        con.close()


class TestCursor:
    ALLOWED = {
        "threads": ("id", "title", "created_at"),
        "messages": ("id", "role", "content", "created_at"),
    }

    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("cursor")
        db = tmp / "cursor.db"
        _make_cursor_db(db)
        artifact, blob = capture_sqlite(
            db, tmp, allowed_tables=("threads", "messages"),
            allowed_columns=self.ALLOWED, byte_limit=1_000_000, count_limit=4,
        )
        return cursor.adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=blob.parent)

    def test_detect_supported_store(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("cursor-detect")
        db = tmp / "cursor.db"
        _make_cursor_db(db)
        artifact, blob = capture_sqlite(
            db, tmp, allowed_tables=("threads", "messages"),
            allowed_columns=self.ALLOWED, byte_limit=1_000_000, count_limit=4,
        )
        assert cursor.detect(artifact, artifact_root=blob.parent) is True

    def test_kinds(self, adapted):
        result = adapted
        kinds = {e.kind for e in result.events}
        assert EventKind.SESSION_LIFECYCLE in kinds
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds

    def test_exact_message_content_is_not_stored_as_summary(self, adapted):
        message = next(
            event for event in adapted.events
            if event.kind is EventKind.USER_MESSAGE
        )
        assert message.content == "cursor prompt"
        assert message.summary is None

    def test_attribution_only_store_fails_closed(self, tmp_path):
        db = tmp_path / "attribution.db"
        _make_cursor_db(db, attribution_only=True)
        artifact, blob = capture_sqlite(
            db, tmp_path, allowed_tables=("attribution",),
            allowed_columns={"attribution": ("id", "name")},
            byte_limit=1_000_000, count_limit=2,
        )
        assert cursor.detect(artifact, artifact_root=blob.parent) is False
        result = cursor.adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=blob.parent)
        assert result.events == ()
        assert any("not supported" in w for w in result.warnings)
