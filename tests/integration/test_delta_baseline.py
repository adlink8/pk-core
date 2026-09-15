"""Explicit first baseline through the public CLI; no live database writes."""
import json
import sqlite3

import pytest

from personal_knowledge.application.conversation.event_generations import GenerationLifecycle
from personal_knowledge.application.ku import main


def test_preview_then_initialize_all_events_without_advancing_cursor(tmp_path, capsys, _generation, _activate):
    db = tmp_path / "pre-delta.sqlite"
    life = GenerationLifecycle(db)
    original = _generation("d-1", "Use PowerShell.")
    life.prepare(original, "g-1")
    _activate(life, "g-1", digest="d-1")
    # Reproduce the database schema from before activation deltas existed.
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE ce_generation_delta_events")
        con.execute("DROP TABLE ce_generation_deltas")
    before = db.read_bytes()
    assert main(["view-baseline", "--conversation-db", str(db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["event_count"] == len(original.events)
    assert preview["scope"] == "all_current_events_once"
    assert db.read_bytes() == before
    command = ["view-baseline", "--conversation-db", str(db), "--write", "--approval", preview["confirmation"]]
    assert main(command) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["status"] == "initialized"
    assert life.authority_generation_id() == "g-1"
    after = db.read_bytes()
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_initialized"
    assert db.read_bytes() == after
    assert main(["view-status", "--conversation-db", str(db)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["pending_event_count"] == len(original.events)
    assert status["prepare_caught_up"] is False
    assert status["prepared_run_count"] == 0


@pytest.fixture
def legacy_db(tmp_path, _generation, _activate):
    db = tmp_path / "legacy.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "Use PowerShell."), "g-1")
    _activate(life, "g-1", digest="d-1")
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM ce_generation_delta_events")
        con.execute("DELETE FROM ce_generation_deltas")
    return db


@pytest.mark.parametrize("change", [
    "UPDATE ce_events SET summary='new evidence' WHERE kind='user_message'",
    "UPDATE ce_sessions SET title='new context'",
    "UPDATE ce_activation_bindings SET value='other-fingerprint' WHERE kind<>'projection_version'",
])
def test_source_change_invalidates_preview_approval(legacy_db, capsys, change):
    assert main(["view-baseline", "--conversation-db", str(legacy_db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    with sqlite3.connect(legacy_db) as con:
        con.execute(change)
    before = legacy_db.read_bytes()
    assert main(["view-baseline", "--conversation-db", str(legacy_db), "--write", "--approval", preview["confirmation"]]) == 2
    assert json.loads(capsys.readouterr().out)["written"] is False
    assert legacy_db.read_bytes() == before


def test_storage_failure_rolls_back_header_rows_and_audit(legacy_db, capsys):
    with sqlite3.connect(legacy_db) as con:
        con.execute("CREATE TRIGGER baseline_abort BEFORE INSERT ON ce_generation_delta_events BEGIN SELECT RAISE(ABORT, 'storage unavailable'); END")
    assert main(["view-baseline", "--conversation-db", str(legacy_db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    before = legacy_db.read_bytes()
    assert main(["view-baseline", "--conversation-db", str(legacy_db), "--write", "--approval", preview["confirmation"]]) == 2
    assert legacy_db.read_bytes() == before


def test_missing_database_and_missing_approval_never_create_or_write(tmp_path, capsys, legacy_db):
    missing = tmp_path / "absent.sqlite"
    assert main(["view-baseline", "--conversation-db", str(missing)]) == 2
    assert not missing.exists()
    before = legacy_db.read_bytes()
    assert main(["view-baseline", "--conversation-db", str(legacy_db), "--write"]) == 2
    assert legacy_db.read_bytes() == before


def test_replay_rejects_missing_baseline_events(legacy_db, capsys):
    assert main(["view-baseline", "--conversation-db", str(legacy_db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    command = ["view-baseline", "--conversation-db", str(legacy_db), "--write", "--approval", preview["confirmation"]]
    assert main(command) == 0
    with sqlite3.connect(legacy_db) as con:
        con.execute("DELETE FROM ce_generation_delta_events")
    before = legacy_db.read_bytes()
    assert main(command) == 2
    assert legacy_db.read_bytes() == before


def test_concurrent_initialization_creates_one_delta_and_consumer_can_resume(legacy_db):
    from concurrent.futures import ThreadPoolExecutor
    from personal_knowledge.application.conversation.delta_baseline import preview_delta_baseline, initialize_delta_baseline
    from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates

    preview = preview_delta_baseline(legacy_db)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: initialize_delta_baseline(legacy_db, approval=preview["confirmation"]), range(3)))
    assert [result["status"] for result in results].count("initialized") == 1
    assert len({result["delta_id"] for result in results}) == 1
    assert len(GenerationDeltaRepository(legacy_db).pending()) == 1
    assert consume_delta_candidates(legacy_db, max_events=1)["event_count"] == 1
    assert consume_delta_candidates(legacy_db)["status"] == "prepared"
    assert consume_delta_candidates(legacy_db)["status"] == "idle"


def test_existing_history_or_consumer_is_not_reinitialized(legacy_db, capsys, _generation, _activate):
    life = GenerationLifecycle(legacy_db)
    life.prepare(_generation("d-2", "New preference"), "g-2")
    _activate(life, "g-2", digest="d-2")
    before = legacy_db.read_bytes()
    assert main(["view-baseline", "--conversation-db", str(legacy_db)]) == 2
    assert legacy_db.read_bytes() == before
