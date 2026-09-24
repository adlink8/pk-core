"""切片E 契约测试：投影层全局只增不删（收集库语义）。

``canonical_messages`` 的 ``rowid`` 是 ``retrieval/conversation_fts.py``
(:28-33) 的增量游标，契约要求三张投影表只增不改不删：

  - 缺的插入（insert）；
  - 变了的**原地 UPDATE**（rowid 不动）；
  - 新一轮不再产出的行，**原样留着**（不删）。

为此**激活/整库那条路也必须走只增原语**，不能 DELETE + 重写：任何 DELETE
都会回收 rowid、让游标错位，任何「删了再插」都会让同一行看起来是新行。

本文件的期望值全部来自数据库实测：跑前用 ``SELECT id, rowid`` 记录映射，
跑后比对；不用实现内部的集合运算反推期望值。所有用例只碰 ``tmp_path`` 下的
临时 SQLite，不触真实库、不触 ``data/`` / ``var/``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.application.conversation.event_generations import (
    GenerationLifecycle,
)
from personal_knowledge.application.conversation.event_repository import (
    GenerationInput,
)
from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    FidelityProfile,
    Provenance,
    TypedEvent,
    make_event_id,
)

FAMILY = "codex"
MANIFEST = "manifest-1"
ARTIFACT_ID = "art-a"
NATIVE_SESSION = "s-1"

SESSION_TABLE = ("canonical_sessions", "canonical_session_id")
MESSAGE_TABLE = ("canonical_messages", "canonical_message_id")
TOOL_TABLE = ("canonical_tool_events", "canonical_tool_id")


# --------------------------------------------------------------- db probes


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def _rowids(db: Path, table: str, id_column: str) -> dict[str, int]:
    """``{id: rowid}`` — 独立来源：直接读物理 rowid，不经过任何实现逻辑。"""

    con = _connect(db)
    try:
        return {
            str(row[0]): int(row[1])
            for row in con.execute(f"SELECT {id_column}, rowid FROM {table}")
        }
    finally:
        con.close()


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = _connect(db)
    try:
        return [tuple(row) for row in con.execute(sql, params)]
    finally:
        con.close()


# ------------------------------------------------------- synthetic generation


def _artifact() -> SourceArtifact:
    return SourceArtifact(
        artifact_id=ARTIFACT_ID, family=FAMILY, source_kind="file",
        content_hash="h" * 8, capture_method="sha256",
        relative_path="rollout.jsonl", byte_size=10,
    )


def _prov(native_id: str, locator: str) -> Provenance:
    return Provenance(
        artifact_id=ARTIFACT_ID, artifact_hash="h" * 8, native_locator=locator,
        native_session_id=NATIVE_SESSION, native_event_id=native_id,
        contract_version="1",
    )


def _generation(
    dataset_digest: str,
    messages: tuple[tuple[EventKind, str, str, str], ...],
) -> GenerationInput:
    """One codex session whose message events are exactly ``messages``.

    Each entry is ``(kind, locator, native_event_id, body)``. The locator is the
    event's address, so it decides the origin-derived canonical message id.
    """

    events = [
        TypedEvent(
            event_id=make_event_id(
                FAMILY, ARTIFACT_ID, "1", native_id,
                kind=kind, session_id=NATIVE_SESSION, native_locator=locator,
            ),
            session_id=NATIVE_SESSION,
            kind=kind,
            provenance=_prov(native_id, locator),
            fidelity=FidelityProfile.complete(),
            ordinal=ordinal,
            occurred_at=f"2026-08-12T00:0{ordinal}:00Z",
            summary=body,
        )
        for ordinal, (kind, locator, native_id, body) in enumerate(messages, start=1)
    ]
    return GenerationInput(
        family=FAMILY,
        adapter_version="1",
        contract_version="1",
        capability_digest="cap-1",
        source_manifest_id=MANIFEST,
        dataset_digest=dataset_digest,
        artifacts=(_artifact(),),
        sessions=(
            AdaptedSession(
                session_id=NATIVE_SESSION,
                provenance=_prov(NATIVE_SESSION, "jsonl:s-1"),
                fidelity=FidelityProfile.complete(),
                native_session_id=NATIVE_SESSION,
                started_at="2026-08-12T00:00:00Z",
                ended_at="2026-08-12T00:05:00Z",
            ),
        ),
        events=tuple(events),
        relations=(),
        dispositions=(),
        warnings=(),
    )


def _activate(life: GenerationLifecycle, generation_id: str, digest: str) -> None:
    life.activate(
        generation_id,
        source_manifest_id=MANIFEST,
        expected_dataset_digest=digest,
        expected_adapter_families=(FAMILY,),
    )


# ------------------------------------------------------------------- tests


def test_new_activation_keeps_prior_row_and_its_rowid(tmp_path: Path) -> None:
    """新一轮激活不再产出的行必须留着，且老行/更新行的 rowid 不动。

    gen-1 有两条消息（locator jsonl:1、jsonl:2）；gen-2 复用 jsonl:1（正文改了）
    并新增 jsonl:3。旧实现会 clear + 重写整表，于是 jsonl:2 那行被 DELETE、
    jsonl:1 那行被删了再插（rowid 变）；只增实现必须留下它并原地更新 jsonl:1。
    """

    db = tmp_path / "conversations.sqlite"
    life = GenerationLifecycle(db)

    gen_a = _generation("ds-1", (
        (EventKind.USER_MESSAGE, "jsonl:1", "msg-1", "first question"),
        (EventKind.ASSISTANT_MESSAGE, "jsonl:2", "msg-2", "first answer"),
    ))
    life.prepare(gen_a, "gen-1")
    _activate(life, "gen-1", "ds-1")

    sessions_before = _rowids(db, *SESSION_TABLE)
    messages_before = _rowids(db, *MESSAGE_TABLE)
    tools_before = _rowids(db, *TOOL_TABLE)
    assert len(messages_before) == 2
    # The row about to disappear upstream, identified by its native locator.
    dropped = _rows(
        db,
        "SELECT canonical_message_id FROM canonical_messages "
        "WHERE source_message_ref='jsonl:2'",
    )
    assert len(dropped) == 1
    dropped_id = dropped[0][0]
    kept = _rows(
        db,
        "SELECT canonical_message_id FROM canonical_messages "
        "WHERE source_message_ref='jsonl:1'",
    )
    kept_id = kept[0][0]

    gen_b = _generation("ds-2", (
        (EventKind.USER_MESSAGE, "jsonl:1", "msg-1", "rewritten question"),
        (EventKind.ASSISTANT_MESSAGE, "jsonl:3", "msg-3", "second answer"),
    ))
    life.prepare(gen_b, "gen-2")
    _activate(life, "gen-2", "ds-2")

    sessions_after = _rowids(db, *SESSION_TABLE)
    messages_after = _rowids(db, *MESSAGE_TABLE)
    tools_after = _rowids(db, *TOOL_TABLE)

    # (2) a row the new round no longer produces is still in the table, with the
    # very rowid it was collected under.
    assert dropped_id in messages_after, sorted(messages_after)
    assert messages_after[dropped_id] == messages_before[dropped_id]
    # nothing else vanished
    assert set(messages_before) <= set(messages_after)

    # (3) the changed row kept its rowid and got the new value in place.
    assert messages_after[kept_id] == messages_before[kept_id]
    assert _rows(
        db,
        "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
        (kept_id,),
    ) == [("rewritten question",)]

    # the genuinely new row was inserted, and the session row never moved.
    assert len(messages_after) == len(messages_before) + 1
    assert sessions_after == sessions_before
    assert tools_after == tools_before


def test_reactivating_the_same_generation_writes_nothing(tmp_path: Path) -> None:
    """同一代投影跑两遍：三张表 rowid 映射前后完全不变（契约核心断言）。

    A row outside the projection's ownership is seeded first (a higher rowid than
    every projected row), so a clear-then-rewrite would have to allocate fresh
    rowids above it: the rowid map would move. An in-place writer writes nothing
    on the second pass and the map is byte-for-byte the same.
    """

    db = tmp_path / "conversations.sqlite"
    life = GenerationLifecycle(db)
    gen = _generation("ds-1", (
        (EventKind.USER_MESSAGE, "jsonl:1", "msg-1", "one question"),
        (EventKind.ASSISTANT_MESSAGE, "jsonl:2", "msg-2", "one answer"),
    ))
    life.prepare(gen, "gen-1")
    _activate(life, "gen-1", "ds-1")

    # A non-projection row the projection must never touch, pinned at a high
    # rowid so a delete-then-reinsert cannot recycle the projected rowids.
    con = _connect(db)
    try:
        con.execute(
            "INSERT INTO canonical_messages (rowid, canonical_message_id,"
            " canonical_session_id, source, ordinal, role, content,"
            " content_length, is_system, is_sidechain, evidence_scope)"
            " VALUES (1000, 'cm|probe|outside', 'cs|probe|outside', 'legacy',"
            " 1, 'user', 'outside row', 11, 0, 0, 'user')"
        )
        con.execute(
            "INSERT INTO canonical_sessions (rowid, canonical_session_id,"
            " primary_source, evidence_eligible, evidence_scope, merged, lifecycle)"
            " VALUES (1000, 'cs|probe|outside', 'legacy', 1, 'user', 0, 'active')"
        )
        con.execute(
            "INSERT INTO canonical_tool_events (rowid, canonical_tool_id,"
            " canonical_session_id, source, source_kind, status)"
            " VALUES (1000, 'ct|probe|outside', 'cs|probe|outside', 'legacy',"
            " 'call', 'ok')"
        )
        con.commit()
    finally:
        con.close()

    sessions_before = _rowids(db, *SESSION_TABLE)
    messages_before = _rowids(db, *MESSAGE_TABLE)
    tools_before = _rowids(db, *TOOL_TABLE)
    assert sessions_before["cs|probe|outside"] == 1000
    assert messages_before["cm|probe|outside"] == 1000
    assert tools_before["ct|probe|outside"] == 1000

    # Second projection of the very same generation: identical rows.
    _activate(life, "gen-1", "ds-1")

    assert _rowids(db, *SESSION_TABLE) == sessions_before
    assert _rowids(db, *MESSAGE_TABLE) == messages_before
    assert _rowids(db, *TOOL_TABLE) == tools_before
