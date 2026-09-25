"""grok 家族（生产模块 ``grok``，ADAPTER_VERSION 1.4.1）适配契约。

公开 seam 是 registry（``adapt_for`` / ``detect_family``）——生产代码只经它
调用家族模块，所以断言也走同一入口，不直接摸 ``grok.adapt``。夹具来自
``support/grok.py``，正文全是合成句子。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources import chatgpt as chatgpt_adapter
from personal_knowledge.adapters.conversation_sources import registry
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.core.conversation_events import (
    EventKind,
    FieldDisposition,
    FidelityDimension,
    FidelityLevel,
    RelationKind,
)
from tests.contract.conversation_sources.support import artifacts
from tests.contract.conversation_sources.support import chatgpt as chatgpt_fixtures
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


# ------------------------------------------- 探测器全函数（grok 的三个文件名）
# 探测器是全函数：畸形文件判「不是我」，不得把解析异常抛给发现层（会被记成
# probe_error）。grok 的 read_text 只被 except OSError 守着，非 UTF-8 字节会
# 抛 UnicodeDecodeError（ValueError 子类），必须落回 False。
# 三个文件名都要过文件名闸门，才真正摸到内容。期望值是独立字面量 False。
NON_UTF8_TEXT = bytes([0xFF, 0xFE, 0x00]) + b"# Summary"
UTF16_TEXT = "# Summary".encode("utf-16")


@pytest.mark.parametrize(
    "name", ["chat_history.jsonl", "summary.json", "summary.md"]
)
@pytest.mark.parametrize("raw", [NON_UTF8_TEXT, UTF16_TEXT])
def test_grok_detector_rejects_malformed_bytes_without_raising(tmp_path, name, raw):
    artifact, root = artifacts.probe_file(tmp_path, name, raw)
    assert registry.detect_family("grok", artifact, artifact_root=root) is False


def test_grok_rejects_agentsview_reconcile_scratch_db(tmp_path):
    """AgentView 的 reconcile 临时库与 chatgpt 共享同一子串锚点。

    两家都用 ``sessions.db`` / ``agentsview`` 判定 AgentView 兼容行，那对
    AgentView 自己的临时库是误判（它只有 ``candidates`` 表）。锚点是共享的，
    排除规则也必须共享，否则同一批文件在两家之间认领口径不一致。
    """
    name = ".agentsview-reconcile-629221015.db"
    artifact, root = artifacts.probe_file(
        tmp_path, name, b"", source_kind="sqlite"
    )
    assert registry.detect_family("grok", artifact, artifact_root=root) is False


def test_grok_pathless_agentsview_captured_snapshot(tmp_path):
    """抓取后的 AgentsView 快照按 ``content_hash[:32]`` 寻址 blob（P0-6 回归）。

    ``grok.adapt`` 的 pathless 分支（单 artifact、``source_kind == "sqlite"``）
    委托共享的 ``adapt_pathless_observation``，它是该共享通道的唯一调用方。旧
    实现用 ``artifact_id`` 拼 blob 路径——抓取后的 blob 存放在内容寻址存储里
    （键为 ``content_hash[:32]``，见 ``contracts.artifact_bytes_path`` 的
    docstring），该路径必然不存在，对所有抓取 artifact 直接抛
    "not resolvable SQLite"。夹具经真实 capture seam 抓取，正中这条路径。

    顺带锁定共享映射的保真度诚实性：正文为空的观测必须写 UNAVAILABLE，不得
    冒充 MAPPED（对齐 chatgpt pathless 适配器的口径）。
    """
    db = tmp_path / "sessions.db"
    chatgpt_fixtures.make_agentsview_db(
        db,
        sessions=(
            ("grok-chat-1", "grok", "2026-09-01T00:00:00Z", None, None, None),
        ),
        messages=(
            (
                "m-1", "grok-chat-1", 1, "user",
                "grok pathless 正文", "2026-09-01T00:00:01Z", 0, 0,
            ),
            (
                "m-2", "grok-chat-1", 2, "assistant",
                None, "2026-09-01T00:00:02Z", 0, 0,
            ),
        ),
    )
    # 允许清单引用生产常量（AgentsView 形态由 chatgpt 家族声明），抓取归属
    # 归 grok——pathless 通道的表结构是共享契约，不应在测试里复制一份。
    # ``mirror_path`` 是关键：生产 v2_sync 抓取时同时给 family + mirror_path，
    # artifact_id 因此是**槽位 id**（``make_slot_artifact_id``），不再是内容寻址
    # 的 blob id——旧实现拿 artifact_id 拼 blob 路径正是在这条生产路径上必然
    # 失效。缺了它，夹具会落进 legacy 回退（artifact_id == blob_id），测不出 bug。
    artifact, root = artifacts.captured_sqlite(
        db,
        tmp_path,
        allowed_tables=chatgpt_adapter.LIVE_ALLOWED_TABLES,
        allowed_columns=chatgpt_adapter.LIVE_ALLOWED_COLUMNS,
        family="grok",
        mirror_path="agentsview/sessions.db",
    )
    result = registry.adapt_for(
        "grok", artifacts.single(artifact), artifact_root=root
    )

    assert result.family == "grok"
    assert len(result.sessions) == 1
    assert result.sessions[0].native_session_id == "grok-chat-1"

    filled = artifacts.event_with(result, EventKind.USER_MESSAGE)
    assert filled.content == "grok pathless 正文"
    assert {d.field_name: d.disposition for d in filled.field_dispositions}[
        "content"
    ] is FieldDisposition.MAPPED

    missing = artifacts.event_with(result, EventKind.ASSISTANT_MESSAGE)
    assert missing.content is None
    assert {d.field_name: d.disposition for d in missing.field_dispositions}[
        "content"
    ] is FieldDisposition.UNAVAILABLE


# ------------------------------------------------- 会话键兜底链（P0-3 回归）
# 每份 Grok 导出的 summary.md 都以同一个 ``# Summary`` 标题头开头（detect
# 本身就锚定它）。旧的兜底取正文首行当 native_session_id，导致全部会话共享
# 同一个键、canonical 层折叠成一个伪会话。兜底链必须是：记录内真实 id →
# 同集合 summary.json 的 info.id → artifact 完整 relative_path 的确定性键，
# 绝不从正文内容派生。


class TestGrokSessionKeyFallback:
    @staticmethod
    def _adapt_summary_md(tmp_path: Path, relative_path: str, body_line: str):
        src = tmp_path / "src"
        target = src / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # 标题头完全相同，正文不同——正是真实 Grok 导出的形态。
        target.write_text(f"# Summary\n{body_line}\n", encoding="utf-8")
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=(relative_path,),
            byte_limit=1_000_000, count_limit=4,
        )
        return registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

    def test_same_heading_different_bodies_yield_distinct_session_ids(self, tmp_path):
        first = self._adapt_summary_md(tmp_path, "sessions/a/summary.md", "正文一")
        second = self._adapt_summary_md(tmp_path, "sessions/b/summary.md", "正文二")

        ids_first = {s.native_session_id for s in first.sessions}
        ids_second = {s.native_session_id for s in second.sessions}
        assert ids_first and ids_second
        assert ids_first != ids_second
        # 旧 bug 的键正是标题头本身；新键必须与它脱离干系。
        assert ids_first.isdisjoint(ids_second | {"# Summary"})

    def test_session_key_uses_full_path_not_parent_name(self, tmp_path):
        # 两个会话的父目录都叫 ``sessions/``：单用 parent.name 的旧兜底同样
        # 会把它们折叠成一个伪会话。
        first = self._adapt_summary_md(tmp_path, "exp-a/sessions/summary.md", "正文一")
        second = self._adapt_summary_md(tmp_path, "exp-b/sessions/summary.md", "正文二")

        ids_first = {s.native_session_id for s in first.sessions}
        ids_second = {s.native_session_id for s in second.sessions}
        assert ids_first and ids_second and ids_first != ids_second

    def test_summary_md_inherits_summary_json_native_id(self, tmp_path):
        """同集合里带 summary.json 时，summary.md 复用它的 ``info.id``。"""
        src = tmp_path / "src"
        src.mkdir()
        (src / "summary.md").write_text("# Summary\nshared heading\n", encoding="utf-8")
        (src / "summary.json").write_text(
            json.dumps({"info": {"id": "native-abc-123"}}), encoding="utf-8"
        )
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=("summary.md", "summary.json"),
            byte_limit=1_000_000, count_limit=4,
        )
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

        assert result.sessions
        assert all(s.native_session_id == "native-abc-123" for s in result.sessions)
        # 整个 artifact set 是一个会话：summary.md 事件与 summary.json 事件
        # 必须共享同一个 session_id，不允许各立门户。
        assert {e.session_id for e in result.events} == {s.session_id for s in result.sessions}

    def test_summary_json_falls_back_to_full_path_key(self, tmp_path):
        """summary.json 缺 ``info.id`` 且父目录同名时，仍不得互相折叠。"""
        ids_per_path = []
        for relative in ("batch-x/sessions/summary.json", "batch-y/sessions/summary.json"):
            src = tmp_path / "src"
            target = src / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"info": {}}), encoding="utf-8")
            _manifest, captured = artifacts.captured_directory(
                src, tmp_path, include_relative=(relative,),
                byte_limit=1_000_000, count_limit=4,
            )
            result = registry.adapt_for(
                "grok", SourceArtifactSet(artifacts=captured),
                artifact_root=tmp_path / "artifacts",
            )
            ids_per_path.append({s.native_session_id for s in result.sessions})

        assert all(ids for ids in ids_per_path)
        # 单用 parent.name 的旧兜底会把两把键都算成 ``sessions``。
        assert ids_per_path[0] != ids_per_path[1]
        assert all(id_ != "sessions" for ids in ids_per_path for id_ in ids)


# ------------------------------------------------ 重复原生 id 消歧（P1-17a）
# 事件 id 是 (family, artifact, native_id) 内容寻址且不含 kind 域：tool_call
# 原生 id 复用会折叠成重复事件 id，AdaptationResult 直接拒收整份会话；旧的
# ``pending_calls[call_id] = ...`` 还会静默覆盖首对映射。任何输入都不得抛
# duplicate event id——重复 id 按出现顺序追加确定性序号后缀，首个映射保留。


class TestGrokDuplicateNativeIds:
    @staticmethod
    def _adapt_chat(tmp_path: Path, chat_text: str):
        src = tmp_path / "src"
        src.mkdir()
        (src / "chat_history.jsonl").write_text(chat_text, encoding="utf-8")
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=("chat_history.jsonl",),
            byte_limit=1_000_000, count_limit=8,
        )
        return registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

    def test_duplicate_call_ids_across_rows_both_emit_events(self, tmp_path: Path):
        """跨行复用同一 tool_call id：两个 call 都有事件，会话不再整体失败。"""
        row1 = {"type": "assistant", "id": "a1", "content": "one",
                "tool_calls": [{"id": "call-dup", "name": "read_file", "arguments": "{}"}]}
        tr1 = {"type": "tool_result", "id": "tr-1",
               "tool_call_id": "call-dup", "content": "first"}
        row2 = {"type": "assistant", "id": "a2", "content": "two",
                "tool_calls": [{"id": "call-dup", "name": "read_file", "arguments": "{}"}]}
        tr2 = {"type": "tool_result", "id": "tr-2",
               "tool_call_id": "call-dup", "content": "second"}
        text = "\n".join(json.dumps(r) for r in (row1, tr1, row2, tr2)) + "\n"

        result = self._adapt_chat(tmp_path, text)

        calls = [e for e in result.events if e.kind is EventKind.TOOL_CALL]
        assert len(calls) == 2
        assert len({e.event_id for e in calls}) == 2
        results = [e for e in result.events if e.kind is EventKind.TOOL_RESULT]
        assert len(results) == 2
        linked = [
            r for r in result.relations if r.relation_kind is RelationKind.CALL_RESULT
        ]
        assert len(linked) == 2
        # 两次结果都回链到首个 call 事件：配对不丢失、也不指向不存在的 call。
        assert {r.source_event_id for r in linked} == {calls[0].event_id}
        assert any("duplicate tool_call id" in w for w in result.warnings)

    def test_duplicate_call_ids_within_one_row_both_emit_events(self, tmp_path: Path):
        row = {"type": "assistant", "id": "a1", "content": "two calls same id",
               "tool_calls": [
                   {"id": "call-dup", "name": "read_file", "arguments": "{}"},
                   {"id": "call-dup", "name": "write_file", "arguments": "{}"},
               ]}

        result = self._adapt_chat(tmp_path, json.dumps(row) + "\n")

        calls = [e for e in result.events if e.kind is EventKind.TOOL_CALL]
        assert len(calls) == 2
        assert len({e.event_id for e in calls}) == 2
        # 消歧保序：首次出现保留裸 id，重复出现带确定性序号后缀。
        assert calls[0].provenance.native_event_id == "call-dup"
        assert (calls[1].provenance.native_event_id or "").startswith("call-dup#")

    def test_row_id_and_call_id_sharing_one_string_do_not_collide(self, tmp_path: Path):
        """事件 id 域不含 kind：行 id 与 call id 撞串也必须消歧，不得抛。"""
        row = {"type": "assistant", "id": "same-id", "content": "row",
               "tool_calls": [{"id": "same-id", "name": "read_file", "arguments": "{}"}]}
        follow = {"type": "assistant", "id": "same-id", "content": "again"}

        result = self._adapt_chat(
            tmp_path, json.dumps(row) + "\n" + json.dumps(follow) + "\n"
        )

        assert len({e.event_id for e in result.events}) == len(result.events)

    def test_duplicate_subagent_ids_do_not_crash(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "subagents.json").write_text(
            json.dumps([
                {"id": "sub-dup", "name": "a"},
                {"id": "sub-dup", "name": "b"},
            ]),
            encoding="utf-8",
        )
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=("subagents.json",),
            byte_limit=1_000_000, count_limit=4,
        )
        result = registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

        subs = [e for e in result.events if e.kind is EventKind.SUBAGENT_BOUNDARY]
        assert len(subs) == 2
        assert len({e.event_id for e in subs}) == 2
        assert any(
            "subagents.json" in w and "duplicate" in w for w in result.warnings
        )


# ------------------------------------------------ 坏行计数上报（P1-17b 回归）
# JSONL 里的坏行（非法 JSON / 合法 JSON 但非对象）解析继续，但必须以 warnings
# 计数上报，不允许静默吞掉。


class TestGrokJsonlBadLineAccounting:
    @staticmethod
    def _capture_one(tmp_path: Path, name: str, text: str):
        src = tmp_path / "src"
        src.mkdir()
        (src / name).write_text(text, encoding="utf-8")
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=(name,),
            byte_limit=1_000_000, count_limit=8,
        )
        return registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

    def test_chat_history_bad_lines_are_counted_in_warnings(self, tmp_path: Path):
        lines = [
            json.dumps({"type": "user", "id": "u1", "content": "ok"}),
            "{not valid json",
            json.dumps([1, 2, 3]),
            json.dumps({"type": "assistant", "id": "a1", "content": "fine"}),
            "",
        ]
        result = self._capture_one(
            tmp_path, "chat_history.jsonl", "\n".join(lines) + "\n"
        )

        msgs = [
            e for e in result.events
            if e.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
        ]
        assert len(msgs) == 2, "好行必须照常解析，坏行只跳过不中断"
        assert any(
            "chat_history.jsonl" in w and "skipped 2 malformed" in w
            for w in result.warnings
        )

    def test_events_jsonl_bad_lines_are_counted_in_warnings(self, tmp_path: Path):
        lines = [
            json.dumps({"ts": "2026-07-01T10:00:00Z", "type": "phase_changed"}),
            "]{ broken",
        ]
        result = self._capture_one(tmp_path, "events.jsonl", "\n".join(lines) + "\n")

        native_rows = [
            e for e in result.events
            if (e.provenance.native_locator or "").startswith("events.jsonl#")
        ]
        assert len(native_rows) == 1
        assert any(
            "events.jsonl" in w and "skipped 1 malformed" in w
            for w in result.warnings
        )


# ------------------------------------------ 时间戳全类型归一（P2 回归）
# 原生 ts 可能是 ISO 字符串、epoch 秒（10 位整型）或 epoch 毫秒（13 位整型）。
# 旧实现只认 str：整型 ts 一律丢弃成 None，epoch 秒更是 normalize_timestamp
# 的盲区。归一后必须全部落到 canonical UTC ``...Z``。


def _iso_ts(seconds: int, millis: int = 0) -> str:
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return base if not millis else base[:-1] + f".{millis:03d}Z"


TS_SECONDS = 1751300000
TS_MILLIS = 1751300000123


class TestGrokTimestampNormalization:
    @staticmethod
    def _capture(tmp_path: Path, include_relative, files: dict[str, str]):
        src = tmp_path / "src"
        src.mkdir()
        for name, text in files.items():
            (src / name).write_text(text, encoding="utf-8")
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=include_relative,
            byte_limit=1_000_000, count_limit=8,
        )
        return registry.adapt_for(
            "grok", SourceArtifactSet(artifacts=captured),
            artifact_root=tmp_path / "artifacts",
        )

    def test_events_jsonl_integer_timestamps_are_normalized(self, tmp_path: Path):
        rows = [
            {"ts": TS_SECONDS, "type": "phase_changed"},
            {"ts": TS_MILLIS, "type": "tool_started"},
        ]
        result = self._capture(
            tmp_path, ("events.jsonl",),
            {"events.jsonl": "\n".join(json.dumps(r) for r in rows) + "\n"},
        )

        occurred = sorted(
            e.occurred_at for e in result.events
            if (e.provenance.native_locator or "").startswith("events.jsonl#")
        )
        assert occurred == sorted([
            _iso_ts(TS_SECONDS), _iso_ts(TS_SECONDS, 123),
        ])

    def test_summary_json_integer_timestamps_are_normalized(self, tmp_path: Path):
        doc = {
            "info": {"id": "ts-native-1"},
            "created_at": TS_SECONDS,
            "updated_at": TS_MILLIS,
        }
        result = self._capture(
            tmp_path, ("summary.json",),
            {"summary.json": json.dumps(doc)},
        )

        session = result.sessions[0]
        assert session.started_at == _iso_ts(TS_SECONDS)
        assert session.ended_at == _iso_ts(TS_SECONDS, 123)
        lifecycle = next(
            e for e in result.events if e.kind is EventKind.SESSION_LIFECYCLE
        )
        assert lifecycle.occurred_at == _iso_ts(TS_SECONDS)

    def test_chat_row_integer_timestamp_is_normalized(self, tmp_path: Path):
        row = {"type": "user", "id": "u1", "content": "x", "timestamp": TS_SECONDS}
        result = self._capture(
            tmp_path, ("chat_history.jsonl",),
            {"chat_history.jsonl": json.dumps(row) + "\n"},
        )

        user = next(e for e in result.events if e.kind is EventKind.USER_MESSAGE)
        assert user.occurred_at == _iso_ts(TS_SECONDS)


# ------------------------------------- detect 流式探测窗口（P2 回归）
# 旧实现 ``read_text(...)[:16384]`` 把整份 transcript 全量载入内存再截断；
# 大文件（多 MB）在 discovery 扫描时每个候选都白付一次全量读。探测必须
# 流式分块只读前 16K。


class TestGrokDetectStreamingWindow:
    @staticmethod
    def _capture_chat(tmp_path: Path, text: str):
        src = tmp_path / "src"
        src.mkdir()
        (src / "chat_history.jsonl").write_text(text, encoding="utf-8")
        _manifest, captured = artifacts.captured_directory(
            src, tmp_path, include_relative=("chat_history.jsonl",),
            byte_limit=10_000_000, count_limit=8,
        )
        return captured[0]

    def test_large_file_detect_streams_without_full_read(self, tmp_path, monkeypatch):
        big = (
            json.dumps({"type": "system", "content": "preamble " * 20000}) + "\n"
            + json.dumps({"type": "user", "content": "tail"})
        )
        chat = self._capture_chat(tmp_path, big)

        def _no_full_read(self, *args, **kwargs):
            raise AssertionError("detect 不得整文件 read_text（流式探测窗口）")

        monkeypatch.setattr(Path, "read_text", _no_full_read)
        assert registry.detect_family(
            "grok", chat, artifact_root=tmp_path / "artifacts"
        ) is True

    def test_markers_beyond_16k_window_do_not_match(self, tmp_path):
        chat = self._capture_chat(tmp_path, "x" * 20000 + '"type":"user"')
        assert registry.detect_family(
            "grok", chat, artifact_root=tmp_path / "artifacts"
        ) is False
