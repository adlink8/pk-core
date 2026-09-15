"""Audit 2026-09-15 三项修复的回归测试。

覆盖：
  - Fix A  workbuddy_kimi 会话时间倒挂（started_at <= ended_at）
  - Fix B  cursor JSONL 空 transcript 不产生幽灵会话
  - Fix C  引擎公共层 session title 回退（空 title 用首条 user 消息）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import cursor
from personal_knowledge.adapters.conversation_sources import workbuddy_kimi
from personal_knowledge.adapters.conversation_sources.contracts import (
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.snapshots import capture_file
from personal_knowledge.application.conversation.event_repository import (
    GenerationInput,
    _enrich_session_titles,
)
from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    FidelityProfile,
    Provenance,
    TypedEvent,
)


def test_kimi_session_time_bounds_no_reversal():
    # 记录0 用 created_at（较晚），记录1 用毫秒纪元 time（较早）：
    # 旧逻辑 started=records[0] > ended=records[-1] 会倒挂；新逻辑取极值修正。
    later = datetime(2026, 7, 1, 10, 0, 3, tzinfo=timezone.utc)
    earlier = datetime(2026, 7, 1, 10, 0, 1, tzinfo=timezone.utc)
    records = [
        {"created_at": later.isoformat().replace("+00:00", "Z")},
        {"time": int(earlier.timestamp() * 1000)},
    ]
    started, ended = workbuddy_kimi._session_time_bounds(records)
    assert started is not None and ended is not None
    assert started <= ended
    assert "10:00:01" in started  # 最早来自毫秒纪元记录
    assert "10:00:03" in ended    # 最晚来自 created_at 记录


def test_kimi_session_time_bounds_empty():
    assert workbuddy_kimi._session_time_bounds([]) == (None, None)
    assert workbuddy_kimi._session_time_bounds([{}, {"foo": 1}]) == (None, None)


def _capture_jsonl(tmp_path: Path, rows: list[dict]):
    src = tmp_path / "thread.jsonl"
    src.write_text(
        chr(10).join(json.dumps(r) for r in rows) + chr(10), encoding="utf-8"
    )
    artifact, blob = capture_file(
        src, tmp_path / "capture", relative_path="thread.jsonl",
        byte_limit=1_000_000, count_limit=1,
    )
    return artifact, blob.parent


def test_cursor_empty_transcript_no_ghost_session(tmp_path: Path):
    # 无 message 行（仅 unknown / turn 边界等）的 transcript 不应产生会话。
    rows = [
        {"type": "turn_ended", "timestamp": "2026-07-01T10:00:00Z"},
        {"role": "system", "content": "note"},
    ]
    artifact, root = _capture_jsonl(tmp_path, rows)
    result = cursor.adapt(SourceArtifactSet((artifact,)), artifact_root=root)
    assert result.sessions == ()


def _make_gen(sessions, events):
    return GenerationInput(
        family="claude",
        adapter_version="x", contract_version="x", capability_digest="x",
        source_manifest_id="x", dataset_digest="x",
        sessions=tuple(sessions), events=tuple(events),
    )


def _prov():
    return Provenance(
        artifact_id="a", artifact_hash="h", native_locator="l",
        native_session_id="s", native_event_id="s", contract_version="x",
    )


def test_enrich_session_titles_from_first_user_message():
    prov = _prov()
    sess = AdaptedSession(
        session_id="s1", provenance=prov, fidelity=FidelityProfile.complete(),
        title=None,
    )
    evt = TypedEvent(
        event_id="e1", session_id="s1", kind=EventKind.USER_MESSAGE,
        provenance=prov, fidelity=FidelityProfile.complete(),
        content="请帮我\n修复这个\t很长的 bug 描述内容",
    )
    out = _enrich_session_titles(_make_gen([sess], [evt]))
    assert out.sessions[0].title == "请帮我 修复这个 很长的 bug 描述内容"
    assert len(out.sessions[0].title) <= 80


def test_enrich_session_titles_summary_only_stays_empty():
    # claude 类无 user 消息的 summary-only 会话保持空 title。
    prov = _prov()
    sess = AdaptedSession(
        session_id="s1", provenance=prov, fidelity=FidelityProfile.complete(),
        title=None,
    )
    evt = TypedEvent(
        event_id="e1", session_id="s1", kind=EventKind.SESSION_LIFECYCLE,
        provenance=prov, fidelity=FidelityProfile.complete(), summary="summary only",
    )
    out = _enrich_session_titles(_make_gen([sess], [evt]))
    assert out.sessions[0].title is None


def test_enrich_session_titles_truncates_to_80():
    prov = _prov()
    sess = AdaptedSession(
        session_id="s1", provenance=prov, fidelity=FidelityProfile.complete(),
        title=None,
    )
    evt = TypedEvent(
        event_id="e1", session_id="s1", kind=EventKind.USER_MESSAGE,
        provenance=prov, fidelity=FidelityProfile.complete(), content="x" * 200,
    )
    out = _enrich_session_titles(_make_gen([sess], [evt]))
    assert len(out.sessions[0].title) == 80
