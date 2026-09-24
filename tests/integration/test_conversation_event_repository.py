"""Phase 62-01 Task 3: generation-bound canonical v2 schema and repository.

RED tests for :mod:`personal_knowledge.application.conversation.event_schema`
and :mod:`personal_knowledge.application.conversation.event_repository`:
  - additive v2 DDL that leaves existing compatibility tables untouched
  - FK/unique enforcement and idempotent replay of a full generation
  - generation isolation (no cross-generation reads/writes/relations)
  - native locator lookup and event/relation ordering
  - unknown-native preservation
  - read-only active-generation queries; the repository never activates a
    generation or builds views/projections

All tests run against temporary SQLite files under tmp_path. No live database,
no var/, no network, no provider calls (D-31).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    EventRelation,
    FieldDisposition,
    FieldDispositionRecord,
    FidelityLevel,
    FidelityProfile,
    FidelityDimension,
    Provenance,
    RelationKind,
    TypedEvent,
    make_event_id,
)
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.application.conversation.event_schema import (
    SCHEMA_VERSION,
    V2_TABLES,
    create_v2_schema,
)
from personal_knowledge.application.conversation.event_repository import (
    EventRepository,
    EventRepositoryError,
    GenerationInput,
)


def _prov(native_event_id: str, locator: str) -> Provenance:
    return Provenance(
        artifact_id="art-a", artifact_hash="h" * 8, native_locator=locator,
        native_session_id="s-1", native_event_id=native_event_id,
        contract_version="1",
    )


def _artifact() -> SourceArtifact:
    return SourceArtifact(
        artifact_id="art-a", family="codex", source_kind="file",
        content_hash="h" * 8, capture_method="sha256",
        relative_path="rollout.jsonl", byte_size=10,
    )


def test_v2_schema_has_family_scale_read_path_indexes(tmp_path: Path) -> None:
    db = tmp_path / "indexed.sqlite"
    create_v2_schema(db)
    con = sqlite3.connect(db)
    try:
        names = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    finally:
        con.close()
    assert {
        "ce_sessions_generation_family",
        "ce_events_generation_session",
        "ce_relations_generation_source",
        "ce_dispositions_generation_event",
    } <= names


def _session() -> AdaptedSession:
    return AdaptedSession(
        session_id="s-1",
        provenance=_prov("s-1", "jsonl:1"),
        fidelity=FidelityProfile.complete(),
        native_session_id="s-1",
    )


def _events(n: int = 2, *, base_locator: int = 1) -> tuple[TypedEvent, ...]:
    kinds = (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE)
    events: list[TypedEvent] = []
    for i in range(n):
        locator = f"jsonl:{base_locator + i}"
        events.append(
            TypedEvent(
                event_id=make_event_id(
                    "codex", "art-a", "1", f"native-{base_locator + i}"
                ),
                session_id="s-1",
                kind=kinds[i % 2],
                provenance=_prov(f"native-{base_locator + i}", locator),
                fidelity=FidelityProfile.complete(),
                ordinal=i + 1,
                occurred_at=f"2026-08-12T00:00:{i:02d}Z",
            )
        )
    return tuple(events)


def _gen(
    events: tuple[TypedEvent, ...],
    relations: tuple[EventRelation, ...] = (),
    dispositions: tuple[FieldDispositionRecord, ...] = (),
    *,
    generation_id: str = "gen-1",
) -> GenerationInput:
    return GenerationInput(
        family="codex",
        adapter_version="1",
        contract_version="1",
        capability_digest="cap-1",
        source_manifest_id="manifest-1",
        dataset_digest="ds-1",
        artifacts=(_artifact(),),
        sessions=(_session(),),
        events=events,
        relations=relations,
        dispositions=dispositions,
        warnings=(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> EventRepository:
    db = tmp_path / "conversations.sqlite"
    repository = EventRepository(db)
    repository.create_schema()
    return repository


def _insert_legacy_table(db: Path) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS canonical_sessions "
        "(canonical_session_id TEXT PRIMARY KEY, agent TEXT)"
    )
    con.execute(
        "INSERT OR IGNORE INTO canonical_sessions VALUES ('cs-1', 'codex')"
    )
    con.commit()
    con.close()


def test_schema_created_additively_keeps_legacy_tables(tmp_path: Path) -> None:
    db = tmp_path / "conversations.sqlite"
    _insert_legacy_table(db)
    repo = EventRepository(db)
    repo.create_schema()
    con = sqlite3.connect(str(db))
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    legacy_count = con.execute(
        "SELECT COUNT(*) FROM canonical_sessions"
    ).fetchone()[0]
    con.close()
    assert "canonical_sessions" in tables
    assert legacy_count == 1  # untouched compatibility row
    for table in V2_TABLES:
        assert table in tables, f"missing v2 table {table}"


def test_write_generation_persists_events_sessions_relations(
    repo: EventRepository,
) -> None:
    events = _events(3)
    relation = EventRelation(
        "rel-1", events[0].event_id, events[1].event_id, RelationKind.CALL_RESULT
    )
    repo.write_generation(
        _gen(events, relations=(relation,)), generation_id="gen-1"
    )
    result = repo.validate_generation("gen-1")
    assert result["ok"] is True
    assert result["events"] == 3
    assert result["sessions"] == 1
    assert result["relations"] == 1
    assert len(repo.iter_events("gen-1")) == 3
    assert len(repo.iter_relations("gen-1")) == 1


def test_idempotent_replay_no_duplicates(repo: EventRepository) -> None:
    gen = _gen(_events(2))
    repo.write_generation(gen, generation_id="gen-1")
    repo.write_generation(gen, generation_id="gen-1")
    result = repo.validate_generation("gen-1")
    assert result["events"] == 2
    assert result["sessions"] == 1
    assert result["relations"] == 0
    assert len(repo.iter_events("gen-1")) == 2


def test_repository_round_trips_exact_message_content(repo: EventRepository) -> None:
    event = replace(
        _events(1)[0],
        content="verbatim body\nwith exact spacing",
        summary="bounded synopsis",
    )
    repo.write_generation(_gen((event,)), generation_id="gen-content")

    row = repo.iter_events("gen-content")[0]
    assert row["content"] == "verbatim body\nwith exact spacing"
    assert row["summary"] == "bounded synopsis"
    located = repo.lookup_by_native_locator(
        "jsonl:1", generation_id="gen-content"
    )
    assert located[0]["content"] == "verbatim body\nwith exact spacing"


def test_multi_family_cohort_is_one_atomic_generation(repo: EventRepository) -> None:
    codex = _gen(_events(2))
    pi_dict = codex.__dict__.copy()
    pi_dict.update({
        "family": "pi",
        "adapter_version": "2",
        "capability_digest": "cap-pi",
        "artifacts": (),
        "sessions": (),
        "events": (),
    })
    repo.write_generation_cohort(
        (codex, GenerationInput(**pi_dict)),
        generation_id="cohort-1",
        source_manifest_id="manifest-cohort",
        dataset_digest="digest-cohort",
    )
    con = sqlite3.connect(str(repo.db))
    families = {
        row[0] for row in con.execute(
            "SELECT family FROM ce_adapter_runs WHERE generation_id='cohort-1'"
        )
    }
    headers = con.execute(
        "SELECT COUNT(*) FROM ce_event_generations WHERE generation_id='cohort-1'"
    ).fetchone()[0]
    con.close()
    assert families == {"codex", "pi"}
    assert headers == 1


def test_generation_isolation_between_generations(repo: EventRepository) -> None:
    gen_a = _gen(_events(2, base_locator=1), generation_id="gen-a")
    gen_b = _gen(_events(2, base_locator=100), generation_id="gen-b")
    repo.write_generation(gen_a, generation_id="gen-a")
    repo.write_generation(gen_b, generation_id="gen-b")
    assert len(repo.iter_events("gen-a")) == 2
    assert len(repo.iter_events("gen-b")) == 2
    ids_a = {e["event_id"] for e in repo.iter_events("gen-a")}
    ids_b = {e["event_id"] for e in repo.iter_events("gen-b")}
    assert not (ids_a & ids_b)


def test_import_generation_promotes_exact_shadow_rows_without_activation(
    tmp_path: Path,
) -> None:
    shadow = EventRepository(tmp_path / "shadow.sqlite")
    shadow.create_schema()
    shadow.write_generation(_gen(_events(3)), generation_id="verified-gen")
    live = EventRepository(tmp_path / "live.sqlite")
    result = live.import_generation(shadow.db, "verified-gen")
    assert result["ok"] is True
    assert result["counts"]["ce_events"] == 3
    assert live.validate_generation("verified-gen")["ok"] is True
    assert live.authority_generation_id() is None


def test_import_generation_fails_closed_when_source_is_absent(tmp_path: Path) -> None:
    source = EventRepository(tmp_path / "source.sqlite")
    source.create_schema()
    live = EventRepository(tmp_path / "live.sqlite")
    with pytest.raises(EventRepositoryError, match="absent"):
        live.import_generation(source.db, "missing")
    assert live.validate_generation("missing")["ok"] is False


def test_native_locator_lookup_within_generation(repo: EventRepository) -> None:
    events = _events(3)
    repo.write_generation(_gen(events), generation_id="gen-1")
    found = repo.lookup_by_native_locator("jsonl:2", generation_id="gen-1")
    assert len(found) == 1
    assert found[0]["event_id"] == events[1].event_id
    assert found[0]["native_locator"] == "jsonl:2"


def test_event_and_relation_ordering(repo: EventRepository) -> None:
    events = _events(3)
    repo.write_generation(_gen(events), generation_id="gen-1")
    ordinals = [e["ordinal"] for e in repo.iter_events("gen-1")]
    assert ordinals == [1, 2, 3]


def test_unknown_native_preserved_by_reference(repo: EventRepository) -> None:
    prov = _prov("unk-1", "jsonl:99")
    event = TypedEvent(
        event_id=make_event_id("pi", "art-a", "1", "unk-1"),
        session_id="s-1",
        kind=EventKind.UNKNOWN_NATIVE,
        provenance=prov,
        fidelity=FidelityProfile.from_levels(
            {FidelityDimension.STRUCTURE_COMPLETENESS: FidelityLevel.PARTIAL}
        ),
        native_payload_ref="art-a:jsonl:99",
        field_dispositions=(
            FieldDispositionRecord(
                "native_body", FieldDisposition.PRESERVED_BY_REFERENCE,
                "raw row kept in immutable artifact slice",
            ),
        ),
    )
    repo.write_generation(_gen((event,)), generation_id="gen-1")
    rows = repo.iter_events("gen-1")
    assert rows[0]["kind"] == EventKind.UNKNOWN_NATIVE.value
    assert rows[0]["native_payload_ref"] == "art-a:jsonl:99"
    disps = repo.iter_dispositions("gen-1")
    assert disps[0]["field_name"] == "native_body"
    assert disps[0]["disposition"] == FieldDisposition.PRESERVED_BY_REFERENCE.value


def test_no_cross_generation_relation(repo: EventRepository) -> None:
    events_a = _events(2, base_locator=1)
    repo.write_generation(_gen(events_a), generation_id="gen-a")
    ghost = EventRelation(
        "rel-ghost", events_a[0].event_id, "event-from-another-generation",
        RelationKind.CALL_RESULT,
    )
    with pytest.raises(Exception, match="relation|endpoint|generation|FK|foreign"):
        repo.write_generation(
            _gen((_events(1, base_locator=50)[0],), relations=(ghost,)),
            generation_id="gen-b",
        )


def test_foreign_key_enforced_on_unknown_session(repo: EventRepository) -> None:
    event = _events(1)[0]
    # events always reference a session; tamper the input to a session the repo
    # never wrote, the write must reject it rather than persist a dangling row
    tampered = _gen((event,))
    tampered_dict = tampered.__dict__.copy()
    tampered_dict["sessions"] = ()
    tampered_dict["events"] = (
        TypedEvent(
            event_id=event.event_id,
            session_id="missing-session",
            kind=event.kind,
            provenance=event.provenance,
            fidelity=event.fidelity,
        ),
    )
    with pytest.raises(Exception, match="session|FK|foreign|constraint"):
        repo.write_generation(GenerationInput(**tampered_dict), generation_id="gen-1")


def test_active_generation_queries_are_read_only_and_empty_until_activated(
    repo: EventRepository,
) -> None:
    repo.write_generation(_gen(_events(2)), generation_id="gen-1")
    # the repository never activates: authority stays empty
    assert repo.authority_generation_id() is None
    # active-scoped reads return nothing until an authority activates the gen
    assert repo.query_authority_events_by_native_locator("jsonl:1") == []
    # and there is no public mutation seam for the authority
    public = [n for n in dir(repo) if not n.startswith("_")]
    assert not any("activ" in n.lower() for n in public)


def test_write_generation_rolls_back_on_failure(repo: EventRepository) -> None:
    event = _events(1)[0]
    bad = _gen((event,))
    bad_dict = bad.__dict__.copy()
    bad_dict["relations"] = (
        EventRelation(
            "rel-x", event.event_id, "no-such-event", RelationKind.CALL_RESULT
        ),
    )
    with pytest.raises(Exception, match="relation|endpoint|generation|FK|foreign"):
        repo.write_generation(GenerationInput(**bad_dict), generation_id="gen-1")
    # nothing persisted from the failed write
    assert repo.validate_generation("gen-1")["ok"] is False


def test_schema_version_is_versioned(repo: EventRepository) -> None:
    assert isinstance(SCHEMA_VERSION, str)
    assert SCHEMA_VERSION.startswith("v2.")


def test_schema_migration_adds_optional_content_without_replacing_existing_events(
    tmp_path: Path,
) -> None:
    db = tmp_path / "old-v2.sqlite"
    con = sqlite3.connect(db)
    try:
        con.execute(
            """CREATE TABLE ce_events (
                generation_id TEXT NOT NULL, event_id TEXT NOT NULL,
                session_id TEXT NOT NULL, kind TEXT NOT NULL,
                artifact_id TEXT NOT NULL, native_locator TEXT NOT NULL,
                native_event_id TEXT, occurred_at TEXT, ordinal INTEGER,
                native_payload_ref TEXT, summary TEXT,
                contract_version TEXT NOT NULL, fidelity_json TEXT NOT NULL,
                PRIMARY KEY (generation_id, event_id)
            )"""
        )
        con.execute(
            "INSERT INTO ce_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "gen-old", "event-old", "session-old", "user_message",
                "artifact-old", "jsonl:1", "native-old", None, 1, None,
                "legacy summary", "1", "{}",
            ),
        )
        con.commit()
    finally:
        con.close()

    create_v2_schema(db)
    create_v2_schema(db)

    con = sqlite3.connect(db)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(ce_events)")}
        row = con.execute(
            "SELECT event_id, content, summary FROM ce_events WHERE event_id='event-old'"
        ).fetchone()
    finally:
        con.close()
    assert "content" in columns
    assert row == ("event-old", None, "legacy summary")


def test_import_generation_maps_columns_across_v20_migration_order(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "fresh-v21.sqlite"
    source = EventRepository(source_db)
    source.create_schema()
    event = replace(
        _events(1)[0], content="exact imported body", summary="imported summary"
    )
    source.write_generation(_gen((event,)), generation_id="gen-import")

    target_db = tmp_path / "migrated-v20.sqlite"
    con = sqlite3.connect(target_db)
    try:
        con.execute(
            """CREATE TABLE ce_events (
                generation_id TEXT NOT NULL, event_id TEXT NOT NULL,
                session_id TEXT NOT NULL, kind TEXT NOT NULL,
                artifact_id TEXT NOT NULL, native_locator TEXT NOT NULL,
                native_event_id TEXT, occurred_at TEXT, ordinal INTEGER,
                native_payload_ref TEXT, summary TEXT,
                contract_version TEXT NOT NULL, fidelity_json TEXT NOT NULL,
                PRIMARY KEY (generation_id, event_id)
            )"""
        )
        con.commit()
    finally:
        con.close()
    target = EventRepository(target_db)

    target.import_generation(source_db, "gen-import")

    row = target.iter_events("gen-import")[0]
    assert row["content"] == "exact imported body"
    assert row["summary"] == "imported summary"
    assert row["contract_version"] == "1"
    assert row["fidelity_json"] == source.iter_events("gen-import")[0][
        "fidelity_json"
    ]


def test_import_generation_maps_missing_optional_content_from_v20_source(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "legacy-v20-source.sqlite"
    source = EventRepository(source_db)
    source.create_schema()
    event = replace(
        _events(1)[0], content=None, summary="legacy source summary"
    )
    source.write_generation(_gen((event,)), generation_id="gen-legacy")
    expected_fidelity = source.iter_events("gen-legacy")[0]["fidelity_json"]
    con = sqlite3.connect(source_db)
    try:
        con.execute("ALTER TABLE ce_events DROP COLUMN content")
        con.commit()
    finally:
        con.close()

    target = EventRepository(tmp_path / "fresh-v21-target.sqlite")
    target.import_generation(source_db, "gen-legacy")

    row = target.iter_events("gen-legacy")[0]
    assert row["content"] is None
    assert row["summary"] == "legacy source summary"
    assert row["contract_version"] == "1"
    assert row["fidelity_json"] == expected_fidelity
