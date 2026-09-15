"""Read-only operational visibility through the existing view-status CLI."""
import json
import sqlite3
from dataclasses import replace

import pytest

from personal_knowledge.application.conversation.event_generations import GenerationLifecycle
from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates
from personal_knowledge.application.ku import main


def test_status_tracks_partial_page_and_keeps_prepared_distinct_from_learned(tmp_path, capsys, _activate, _generation):
    db = tmp_path / "events.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "Use PowerShell."), "g-1")
    _activate(life, "g-1", digest="d-1")
    before = db.read_bytes()
    assert main(["view-status", "--conversation-db", str(db)]) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["status"] == "pending"
    assert initial["pending_delta_count"] == 1
    assert initial["pending_event_count"] > 1
    assert initial["prepare_caught_up"] is False
    assert db.read_bytes() == before


    consume_delta_candidates(db, max_events=1)
    assert main(["view-status", "--conversation-db", str(db)]) == 0
    partial = json.loads(capsys.readouterr().out)
    assert partial["pending_event_count"] == initial["pending_event_count"] - 1
    assert partial["pending_delta_count"] == 1
    consume_delta_candidates(db)
    before = db.read_bytes()
    assert main(["view-status", "--conversation-db", str(db)]) == 0
    finished = json.loads(capsys.readouterr().out)
    assert finished["pending_event_count"] == 0
    assert finished["pending_delta_count"] == 0
    assert finished["prepare_caught_up"] is True
    assert finished["memory_ready"] is False
    assert finished["prepared_run_count"] >= 1
    assert db.read_bytes() == before


@pytest.mark.parametrize("existing", [False, True])
def test_missing_history_is_not_reported_as_caught_up(tmp_path, capsys, existing, _generation):
    db = tmp_path / "missing.sqlite"
    if existing:
        life = GenerationLifecycle(db)
        life.prepare(_generation("d-1", "Use PowerShell."), "g-1")
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO ce_generation_authority VALUES ('g-1',1,'2026-09-07')")
    before = db.read_bytes() if existing else None
    assert main(["view-status", "--conversation-db", str(db)]) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "uninitialized"
    assert status["prepare_caught_up"] is False
    assert status["pending_event_count"] is None
    assert (db.read_bytes() if db.exists() else None) == before


def test_withdrawal_remains_visible_after_all_pages_are_consumed(tmp_path, capsys, _activate, _generation):
    db = tmp_path / "withdraw.sqlite"
    life = GenerationLifecycle(db)
    original = _generation("d-1", "Use PowerShell.")
    life.prepare(original, "g-1")
    _activate(life, "g-1", digest="d-1")
    consume_delta_candidates(db)
    empty = replace(original, dataset_digest="d-2", events=(), relations=(), dispositions=())
    life.prepare(empty, "g-2")
    _activate(life, "g-2", digest="d-2")
    consume_delta_candidates(db)
    assert consume_delta_candidates(db)["status"] == "idle"
    before = db.read_bytes()
    assert main(["view-status", "--conversation-db", str(db)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "withdrawal_required"
    assert status["withdrawal_ref_count"] == len(original.events)
    assert status["prepare_caught_up"] is True
    assert status["memory_ready"] is False
    assert db.read_bytes() == before


def test_active_generation_without_its_own_history_is_uninitialized(tmp_path, capsys, _activate, _generation):
    db = tmp_path / "outdated-history.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "Use PowerShell."), "g-1")
    _activate(life, "g-1", digest="d-1")
    consume_delta_candidates(db)
    life.prepare(_generation("d-2", "Use Bash."), "g-2")
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ce_generation_authority SET active=0")
        con.execute("INSERT INTO ce_generation_authority VALUES ('g-2',1,'2026-09-07')")
    before = db.read_bytes()
    assert main(["view-status", "--conversation-db", str(db)]) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["reason"] == "activation_delta_history_missing"
    assert status["prepare_caught_up"] is False
    assert db.read_bytes() == before
    with pytest.raises(ValueError, match="history"):
        consume_delta_candidates(db)
    assert db.read_bytes() == before


def test_consumer_rejects_missing_batch_instead_of_silently_skipping_work(tmp_path, _activate, _generation):
    db = tmp_path / "consumer-checkpoint.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "Use PowerShell."), "g-1")
    _activate(life, "g-1", digest="d-1")
    consume_delta_candidates(db)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM ce_delta_prepare_batches")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="checkpoint"):
        consume_delta_candidates(db)
    assert db.read_bytes() == before


@pytest.mark.parametrize("damage", [
    "UPDATE ce_delta_prepare_cursor SET delta_id=999",
    "UPDATE ce_delta_prepare_cursor SET event_cursor='missing-event'",
    "DROP TABLE ce_delta_prepare_batches",
    "DELETE FROM ce_delta_prepare_batches",
])
def test_invalid_checkpoint_fails_closed_without_writes(tmp_path, capsys, damage, _activate, _generation):
    db = tmp_path / "damaged.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "Use PowerShell."), "g-1")
    _activate(life, "g-1", digest="d-1")
    consume_delta_candidates(db)
    with sqlite3.connect(db) as con:
        con.execute(damage)
    before = db.read_bytes()
    assert main(["view-status", "--conversation-db", str(db)]) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "degraded"
    assert status["prepare_caught_up"] is False
    assert db.read_bytes() == before
