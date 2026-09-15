"""Phase 62-02: per-family stream adapter contracts (RED → GREEN).

One explicit capability contract per family (D-02). Each test drives the
real capture seam (:func:`capture_file`) plus the family ``detect``/``adapt``
boundary — never parser internals — and asserts typed events, relations,
fidelity, provenance and fail-closed behavior. Fixtures are small redacted
synthetic shapes observed in local artifacts (62-RESEARCH format matrix).
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from personal_knowledge.adapters.conversation_sources import codex, copilot, gemini, pi
from personal_knowledge.adapters.conversation_sources import workbuddy_kimi
from personal_knowledge.adapters.conversation_sources.claude_qoder import (
    adapt as adapt_dag,
)
from personal_knowledge.adapters.conversation_sources.claude_qoder import (
    capability as dag_capability,
)
from personal_knowledge.adapters.conversation_sources.claude_qoder import (
    detect as detect_dag,
)
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.jsonl_stream import JSONLLineError
from personal_knowledge.adapters.conversation_sources.registry import (
    adapt_for,
    capability_for,
    detect_family,
    known_families,
    resolve_family,
    select_adapter,
)
from personal_knowledge.adapters.conversation_sources.snapshots import capture_file
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
    RelationKind,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "conversation_sources"


def _capture(tmp_path: Path, fixture_name: str):
    """Capture a fixture through the immutable seam and return (artifact, blob_root)."""
    src = FIXTURES / fixture_name
    assert src.exists(), f"missing fixture {src}"
    artifact, blob = capture_file(
        src, tmp_path, relative_path=fixture_name,
        byte_limit=1_000_000, count_limit=1,
    )
    return artifact, blob.parent  # blob store root: dest_dir/artifacts


def _artifact_set(tmp_path: Path, fixture_name: str) -> tuple[SourceArtifactSet, Path]:
    artifact, root = _capture(tmp_path, fixture_name)
    return SourceArtifactSet(artifacts=(artifact,)), root


# --------------------------------------------------------------------------- Codex

class TestCodex:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("codex")
        artifact_set, root = _artifact_set(tmp, "codex_agent_sessions.jsonl")
        return codex.adapt(artifact_set, artifact_root=root)

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "codex_agent_sessions.jsonl")
        assert codex.detect(artifact, artifact_root=root) is True

    def test_detect_rejects_other_stream(self, tmp_path):
        artifact, root = _capture(tmp_path, "pi_conversation.jsonl")
        assert codex.detect(artifact, artifact_root=root) is False

    def test_versions(self, adapted):
        assert adapted.family == "codex"
        assert adapted.adapter_version
        # Expectation changed from "1" to "2": the conversation contract version
        # was bumped because ``artifact_id`` stopped being the content hash and
        # became the stable per-slot identity. Ids produced under "1" and "2"
        # must never coexist in one database, so the declared version moved with
        # the identity change.
        assert adapted.contract_version == "2"

    def test_session_and_events(self, adapted):
        assert len(adapted.sessions) == 1
        kinds = {e.kind for e in adapted.events}
        assert EventKind.SESSION_LIFECYCLE in kinds
        assert EventKind.TURN_BOUNDARY in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.TOOL_RESULT in kinds
        assert EventKind.COMPACTION_SUMMARY in kinds

    def test_provenance_resolvable(self, adapted):
        assert all(e.provenance.resolvable() for e in adapted.events)

    def test_call_result_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.CALL_RESULT]
        assert len(rels) == 1

    def test_turn_membership_relations(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.TURN_MEMBERSHIP]
        assert len(rels) >= 1

    def test_compaction_is_not_user_message(self, adapted):
        compact = [e for e in adapted.events if e.kind is EventKind.COMPACTION_SUMMARY]
        assert len(compact) == 1
        assert "Compacted earlier turns." in (compact[0].summary or "")

    def test_replay_digest_stable(self, tmp_path):
        artifact_set, root = _artifact_set(tmp_path, "codex_agent_sessions.jsonl")
        first = codex.adapt(artifact_set, artifact_root=root)
        second = codex.adapt(artifact_set, artifact_root=root)
        assert first.dataset_digest == second.dataset_digest

    def test_malformed_line_fails_closed(self, tmp_path):
        artifact, root = _capture(tmp_path, "codex_agent_sessions.jsonl")
        artifact_set = SourceArtifactSet(artifacts=(artifact,))
        # Replace the blob with a deliberately malformed stream.
        blob = root / artifact.artifact_id
        blob.write_text('{"type":"session_meta","session_id":"s"}\nnot json\n', encoding="utf-8")
        with pytest.raises(JSONLLineError):
            codex.adapt(artifact_set, artifact_root=root)


# ----------------------------------------------------------------------- Claude/Qoder

class TestClaude:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("claude")
        artifact_set, root = _artifact_set(tmp, "claude_export.jsonl")
        return adapt_dag("claude", artifact_set, artifact_root=root)

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "claude_export.jsonl")
        assert detect_dag("claude", artifact, artifact_root=root) is True

    def test_capability(self):
        cap = dag_capability("claude")
        assert cap.family == "claude"
        assert cap.digest()

    def test_kinds(self, adapted):
        kinds = {e.kind for e in adapted.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds

    def test_parent_child_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.PARENT_CHILD]
        assert len(rels) >= 1

    def test_sidechain_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.SIDECHAIN]
        assert len(rels) == 1

    def test_all_relations_valid(self, adapted):
        known = {e.event_id for e in adapted.events}
        for rel in adapted.relations:
            assert rel.source_event_id in known
            assert rel.target_event_id in known


class TestQoder:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("qoder")
        artifact_set, root = _artifact_set(tmp, "qoder_export.jsonl")
        return adapt_dag("qoder", artifact_set, artifact_root=root)

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "qoder_export.jsonl")
        assert detect_dag("qoder", artifact, artifact_root=root) is True

    def test_capability_distinct(self):
        assert dag_capability("qoder").family == "qoder"
        assert dag_capability("qoder").digest() != dag_capability("claude").digest()

    def test_compaction_summary_event(self, adapted):
        compact = [e for e in adapted.events if e.kind is EventKind.COMPACTION_SUMMARY]
        assert len(compact) == 1

    def test_compacted_range_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.COMPACTED_RANGE]
        assert len(rels) == 1


# ------------------------------------------------------------------------------ Pi

class TestPi:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("pi")
        artifact_set, root = _artifact_set(tmp, "pi_conversation.jsonl")
        return pi.adapt(artifact_set, artifact_root=root)

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "pi_conversation.jsonl")
        assert pi.detect(artifact, artifact_root=root) is True

    def test_detect_rejects_codex(self, tmp_path):
        artifact, root = _capture(tmp_path, "codex_agent_sessions.jsonl")
        assert pi.detect(artifact, artifact_root=root) is False

    def test_kinds(self, adapted):
        kinds = {e.kind for e in adapted.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.COMPACTION_SUMMARY in kinds

    def test_compaction_range_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.COMPACTED_RANGE]
        assert len(rels) == 1

    def test_fidelity_honest(self, adapted):
        # compaction is visible; content is fully available here → complete
        assert adapted.fidelity.level(FidelityDimension.COMPACTION_VISIBILITY) is FidelityLevel.COMPLETE

    def test_unknown_kind_preserved(self, tmp_path):
        fixture = tmp_path / "pi_unknown.jsonl"
        fixture.write_text(
            '{"type":"conversation","id":"s","created_at":"2026-07-01T10:00:00Z"}\n'
            '{"type":"some_future_kind","conversation_id":"s","message_id":"x","content":"x"}\n',
            encoding="utf-8",
        )
        artifact, root = _capture(tmp_path, "pi_conversation.jsonl")
        blob = root / artifact.artifact_id
        blob.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        result = pi.adapt(SourceArtifactSet(artifacts=(artifact,)), artifact_root=root)
        kinds = {e.kind for e in result.events}
        assert EventKind.UNKNOWN_NATIVE in kinds
        assert result.warnings
        assert result.fidelity.has_loss()

    def test_observed_session_message_wire_format(self, tmp_path):
        src = tmp_path / "pi_live.jsonl"
        src.write_text(
            '\n'.join((
                json.dumps({"type": "session", "id": "s-live", "timestamp": "t0"}),
                json.dumps({"type": "message", "id": "m1", "parentId": None,
                            "timestamp": "t1", "message": {"role": "user", "content": "u"}}),
                json.dumps({"type": "message", "id": "m2", "parentId": "m1",
                            "timestamp": "t2", "message": {"role": "assistant", "content": "a"}}),
            )) + '\n', encoding="utf-8",
        )
        artifact, blob = capture_file(
            src, tmp_path / "capture", relative_path=src.name,
            byte_limit=100_000, count_limit=1,
        )
        assert pi.detect(artifact, artifact_root=blob.parent)
        result = pi.adapt(SourceArtifactSet((artifact,)), artifact_root=blob.parent)
        assert {EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE} <= {
            event.kind for event in result.events
        }


# ------------------------------------------------------------------------- Workbuddy

class TestWorkbuddy:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("workbuddy")
        artifact_set, root = _artifact_set(tmp, "workbuddy_session.jsonl")
        return adapt_for("workbuddy", artifact_set, artifact_root=root)

    def test_family(self, adapted):
        assert adapted.family == "workbuddy"

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "workbuddy_session.jsonl")
        assert detect_family("workbuddy", artifact, artifact_root=root) is True

    def test_kinds(self, adapted):
        kinds = {e.kind for e in adapted.events}
        assert EventKind.REASONING in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.TOOL_RESULT in kinds

    def test_call_result_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.CALL_RESULT]
        assert len(rels) == 1


# ---------------------------------------------------------------------------- Kimi

class TestKimi:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("kimi")
        artifact_set, root = _artifact_set(tmp, "kimi_turn.jsonl")
        return adapt_for("kimi", artifact_set, artifact_root=root)

    def test_family(self, adapted):
        assert adapted.family == "kimi"

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "kimi_turn.jsonl")
        assert detect_family("kimi", artifact, artifact_root=root) is True

    def test_lifecycle_kinds(self, adapted):
        kinds = {e.kind for e in adapted.events}
        assert EventKind.TURN_BOUNDARY in kinds
        assert EventKind.LOOP_BOUNDARY in kinds
        assert EventKind.FILE_CONTEXT in kinds
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds

    def test_observed_dotted_wire_protocol(self, tmp_path):
        src = tmp_path / "wire.jsonl"
        rows = (
            {"type": "metadata", "created_at": "t0", "protocol_version": 1},
            {"type": "turn.prompt", "time": "t1", "content": "prompt"},
            {"type": "context.append_loop_event", "time": "t2",
             "event": {"type": "tool.call", "id": "call-1"}},
            {"type": "context.append_loop_event", "time": "t3",
             "event": {"type": "tool.result", "id": "call-1"}},
            {"type": "usage.record", "time": "t4"},
        )
        src.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        artifact, blob = capture_file(
            src, tmp_path / "capture", relative_path=src.name,
            byte_limit=100_000, count_limit=1,
        )
        assert detect_family("kimi", artifact, artifact_root=blob.parent)
        result = adapt_for("kimi", SourceArtifactSet((artifact,)), artifact_root=blob.parent)
        kinds = {event.kind for event in result.events}
        assert {EventKind.USER_MESSAGE, EventKind.TOOL_CALL,
                EventKind.TOOL_RESULT, EventKind.USAGE} <= kinds
        assert any(r.relation_kind is RelationKind.CALL_RESULT for r in result.relations)


# -------------------------------------------------------------------------- Copilot

class TestCopilot:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("copilot")
        artifact_set, root = _artifact_set(tmp, "copilot_trace.jsonl")
        return adapt_for("copilot", artifact_set, artifact_root=root)

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "copilot_trace.jsonl")
        assert detect_family("copilot", artifact, artifact_root=root) is True

    def test_alias_resolves(self):
        assert resolve_family("vscode-copilot") == "copilot"
        assert capability_for("vscode-copilot").family == "copilot"

    def test_paired_tool_relation(self, adapted):
        rels = [r for r in adapted.relations if r.relation_kind is RelationKind.CALL_RESULT]
        assert len(rels) == 1

    def test_missing_completion_is_partial(self, tmp_path):
        artifact, root = _capture(tmp_path, "copilot_trace.jsonl")
        blob = root / artifact.artifact_id
        text = blob.read_text(encoding="utf-8").replace(
            '{"type":"tool_execution_complete","session_id":"cp_s1","turn_id":"cp_turn_1","tool_id":"ct1","timestamp":"2026-07-01T10:00:03Z"}\n', ""
        )
        blob.write_text(text, encoding="utf-8")
        result = adapt_for("copilot", SourceArtifactSet(artifacts=(artifact,)), artifact_root=root)
        assert any("no completion" in w for w in result.warnings)
        assert result.fidelity.has_loss()
        assert result.fidelity.level(FidelityDimension.RELATION_COMPLETENESS) is FidelityLevel.PARTIAL

    def test_observed_dotted_event_stream(self, tmp_path):
        src = tmp_path / "events.jsonl"
        rows = (
            {"type": "session.start", "id": "s", "timestamp": "t0", "data": {"sessionId": "s"}},
            {"type": "user.message", "id": "u", "timestamp": "t1", "data": {"content": "u"}},
            {"type": "assistant.message", "id": "a", "timestamp": "t2", "data": {"content": "a"}},
            {"type": "session.shutdown", "id": "end", "timestamp": "t3", "data": {}},
        )
        src.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        artifact, blob = capture_file(
            src, tmp_path / "capture", relative_path=src.name,
            byte_limit=100_000, count_limit=1,
        )
        assert copilot.detect(artifact, artifact_root=blob.parent)
        result = copilot.adapt(SourceArtifactSet((artifact,)), artifact_root=blob.parent)
        assert {EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE} <= {
            event.kind for event in result.events
        }

    def test_observed_vscode_requests_json(self, tmp_path):
        src = tmp_path / "chat.json"
        src.write_text(json.dumps({
            "sessionId": "vs-1", "creationDate": "t0",
            "requests": [{"requestId": "r1", "responseId": "a1",
                          "timestamp": "t1", "message": {"text": "u"},
                          "response": [{"value": "a"}]}],
        }), encoding="utf-8")
        artifact, blob = capture_file(
            src, tmp_path / "capture", relative_path=src.name,
            byte_limit=100_000, count_limit=1,
        )
        assert copilot.detect(artifact, artifact_root=blob.parent)
        result = copilot.adapt(SourceArtifactSet((artifact,)), artifact_root=blob.parent)
        assert len(result.sessions) == 1
        assert {EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE} <= {
            event.kind for event in result.events
        }


# --------------------------------------------------------------------------- Gemini

class TestGemini:
    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("gemini")
        artifact_set, root = _artifact_set(tmp, "gemini_conversation.json")
        return adapt_for("gemini", artifact_set, artifact_root=root)

    def test_detect(self, tmp_path):
        artifact, root = _capture(tmp_path, "gemini_conversation.json")
        assert detect_family("gemini", artifact, artifact_root=root) is True

    def test_ordered_messages(self, adapted):
        kinds = [e.kind for e in adapted.events if e.kind is not EventKind.SESSION_LIFECYCLE]
        assert kinds == [EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE]

    def test_session(self, adapted):
        assert len(adapted.sessions) == 1
        assert adapted.sessions[0].native_session_id == "gm_s1"


# -------------------------------------------------------------------------- Registry

class TestRegistry:
    def test_all_stream_families_registered(self):
        families = set(known_families())
        for name in ("codex", "claude", "qoder", "pi", "workbuddy", "kimi",
                     "kimi-work", "copilot", "vscode-copilot", "gemini"):
            assert name in families

    def test_every_family_has_versioned_capability(self):
        # every registered family (stream families + 62-03 handoff) owns a
        # versioned capability whose family resolves to the registered owner
        for name in known_families():
            cap = capability_for(name)
            assert resolve_family(name) == cap.family
            assert cap.digest()

    def test_select_adapter_routes_fixtures(self, tmp_path_factory):
        for fixture, expected in (
            ("codex_agent_sessions.jsonl", "codex"),
            ("claude_export.jsonl", "claude"),
            ("qoder_export.jsonl", "qoder"),
            ("pi_conversation.jsonl", "pi"),
            ("workbuddy_session.jsonl", "workbuddy"),
            ("kimi_turn.jsonl", "kimi"),
            ("copilot_trace.jsonl", "copilot"),
            ("gemini_conversation.json", "gemini"),
        ):
            tmp = tmp_path_factory.mktemp("sel-" + expected)
            artifact, root = _capture(tmp, fixture)
            assert select_adapter(artifact, artifact_root=root) == expected, fixture

    def test_no_generic_fallback(self, tmp_path):
        artifact, root = _capture(tmp_path, "codex_agent_sessions.jsonl")
        # unknown family names fail closed
        import pytest as _pt
        with _pt.raises(KeyError):
            resolve_family("no-such-family")
