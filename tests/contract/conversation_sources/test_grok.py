"""grok 家族（生产模块 ``grok``，ADAPTER_VERSION 1.2.0）适配契约。

公开 seam 是 registry（``adapt_for`` / ``detect_family``）——生产代码只经它
调用家族模块，所以断言也走同一入口，不直接摸 ``grok.adapt``。夹具来自
``support/grok.py``，正文全是合成句子。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityDimension,
    FidelityLevel,
    RelationKind,
)
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import grok as grok_fixtures


class TestGrok:
    INCLUDE = grok_fixtures.GROK_DIRECTORY_INCLUDE

    @pytest.fixture(scope="class")
    def adapted(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("grok")
        src = tmp / "src"
        src.mkdir()
        grok_fixtures.make_grok_directory(src)
        _manifest, captured = artifacts.captured_directory(
            src, tmp, include_relative=self.INCLUDE, byte_limit=1_000_000, count_limit=8,
        )
        return registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured), artifact_root=tmp / "artifacts"
        )

    def test_detect(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("grok-detect")
        src = tmp / "src"
        src.mkdir()
        grok_fixtures.make_grok_directory(src)
        _manifest, captured = artifacts.captured_directory(
            src, tmp, include_relative=self.INCLUDE, byte_limit=1_000_000, count_limit=8,
        )
        summary = next(a for a in captured if a.relative_path == "summary.md")
        assert registry.detect_family(
            "grok", summary, artifact_root=tmp / "artifacts"
        ) is True

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
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=("summary.md",),
            byte_limit=1_000_000, count_limit=4,
        )
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )
        assert result.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY) is FidelityLevel.PARTIAL
        assert result.fidelity.has_loss()


class TestGrokTypedTranscript:
    """回归：原生 ``type`` 打标 transcript 不得被丢弃。"""

    INCLUDE = grok_fixtures.TYPED_TRANSCRIPT_INCLUDE

    @pytest.fixture(scope="class")
    def captured(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("grok-typed")
        src = tmp / "src"
        src.mkdir()
        grok_fixtures.make_typed_grok_directory(src)
        _manifest, captured = artifacts.captured_directory(
            src, tmp, include_relative=self.INCLUDE, byte_limit=1_000_000, count_limit=8,
        )
        return tmp, captured

    def test_detect_accepts_type_tagged_transcript(self, captured):
        tmp, captured_artifacts = captured
        chat = next(a for a in captured_artifacts if a.relative_path == "chat_history.jsonl")
        assert registry.detect_family(
            "grok", chat, artifact_root=tmp / "artifacts"
        ) is True

    def test_transcript_kinds_are_typed(self, captured):
        tmp, captured_artifacts = captured
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured_artifacts),
            artifact_root=tmp / "artifacts",
        )
        kinds = {e.kind for e in result.events}
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.TOOL_RESULT in kinds
        assert EventKind.REASONING in kinds

    def test_parts_content_is_text_not_repr(self, captured):
        tmp, captured_artifacts = captured
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured_artifacts),
            artifact_root=tmp / "artifacts",
        )
        prompt = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
        assert prompt.content == "typed prompt"

    def test_tool_call_and_result_are_linked(self, captured):
        tmp, captured_artifacts = captured
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured_artifacts),
            artifact_root=tmp / "artifacts",
        )
        linked = [r for r in result.relations if r.relation_kind is RelationKind.CALL_RESULT]
        assert len(linked) == 1

    def test_encrypted_reasoning_keeps_summary_and_flags_ciphertext(self, captured):
        tmp, captured_artifacts = captured
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured_artifacts),
            artifact_root=tmp / "artifacts",
        )
        reasoning = next(e for e in result.events if e.kind is EventKind.REASONING)
        assert reasoning.content == "why"
        fields = {d.field_name for d in reasoning.field_dispositions}
        assert "encrypted_content" in fields

    def test_full_transcript_is_complete_fidelity(self, captured):
        tmp, captured_artifacts = captured
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured_artifacts),
            artifact_root=tmp / "artifacts",
        )
        assert result.sessions[0].fidelity.level(
            FidelityDimension.CONTENT_AVAILABILITY
        ) is FidelityLevel.COMPLETE


class TestGrokPerFileAdaptation:
    """v2 pipeline 逐文件 staging / adapt，而不是整目录一次。

    ``_adapt_source_file`` 只把单 artifact 集合交给适配器，所以一个孤立的
    ``chat_history.jsonl`` 也必须产出自洽结果：generation writer 用外键强制
    ``ce_events -> ce_sessions``，悬空的 session id 会整批中止写入。
    """

    def _adapt_lone_chat(self, tmp: Path):
        src = tmp / "src"
        src.mkdir()
        grok_fixtures.make_typed_grok_directory(src)
        _manifest, captured = artifacts.captured_directory(
            src, tmp, include_relative=("chat_history.jsonl",),
            byte_limit=1_000_000, count_limit=8,
        )
        return registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured), artifact_root=tmp / "artifacts"
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
        """Grok 在若干 reasoning 行上复用一个 ``rs_...`` id。"""
        src = tmp_path / "src"
        src.mkdir()
        row = {"type": "reasoning", "id": "rs-dup",
               "summary": [{"type": "summary_text", "text": "why"}]}
        (src / "chat_history.jsonl").write_text(
            json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8",
        )
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=("chat_history.jsonl",),
            byte_limit=1_000_000, count_limit=8,
        )
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

        assert len(result.events) == 2
        assert len({e.event_id for e in result.events}) == 2


# --------------------------------------------------------------- 会话目录覆盖

def _adapt_session_directory(tmp_path: Path):
    src = tmp_path / "session"
    grok_fixtures.make_session_directory(src)
    _manifest, captured = artifacts.captured_directory(
        src, tmp_path, include_relative=grok_fixtures.SESSION_INCLUDE,
        byte_limit=1_000_000, count_limit=8,
    )
    return registry.adapt_for(
        "grok", SourceArtifactSet(artifacts=captured), artifact_root=tmp_path / "artifacts"
    )


def test_one_directory_is_one_session_with_message_text(tmp_path: Path) -> None:
    result = _adapt_session_directory(tmp_path)

    assert len(result.sessions) == 1
    session = result.sessions[0]
    assert session.native_session_id == grok_fixtures.SESSION_ID
    assert {event.session_id for event in result.events} == {session.session_id}

    user = next(event for event in result.events if event.kind is EventKind.USER_MESSAGE)
    assistant = next(
        event for event in result.events if event.kind is EventKind.ASSISTANT_MESSAGE
    )
    tool = next(event for event in result.events if event.kind is EventKind.TOOL_RESULT)
    assert user.content == grok_fixtures.SESSION_USER_TEXT
    assert assistant.content == grok_fixtures.SESSION_ASSISTANT_TEXT
    assert tool.content == grok_fixtures.SESSION_TOOL_TEXT


def test_encrypted_reasoning_keeps_summary_and_names_missing_key(tmp_path: Path) -> None:
    result = _adapt_session_directory(tmp_path)
    reasoning = next(event for event in result.events if event.kind is EventKind.REASONING)

    assert reasoning.kind is not EventKind.UNKNOWN_NATIVE
    assert reasoning.content == grok_fixtures.SESSION_REASONING_SUMMARY
    reason_text = artifacts.reasons(reasoning)
    assert reason_text
    assert "encrypted_content" in reason_text and "无密钥" in reason_text


def test_events_jsonl_types_are_reason_events_not_dropped(tmp_path: Path) -> None:
    result = _adapt_session_directory(tmp_path)
    native_rows = [
        event
        for event in result.events
        if (event.provenance.native_locator or "").startswith("events.jsonl#")
    ]
    assert len(native_rows) == 2

    for native_type in ("phase_changed", "tool_started"):
        matched = [
            event
            for event in native_rows
            if native_type in artifacts.reasons(event)
        ]
        assert len(matched) == 1
        event = matched[0]
        assert event.kind is not EventKind.USER_MESSAGE
        assert event.kind is not EventKind.ASSISTANT_MESSAGE
        assert event.field_dispositions
        assert all((record.reason or "").strip() for record in event.field_dispositions)
        assert event.kind is not EventKind.UNKNOWN_NATIVE or native_type in artifacts.reasons(event)
