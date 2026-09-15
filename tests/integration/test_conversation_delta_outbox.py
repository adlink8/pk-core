import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from personal_knowledge.application.conversation.delta_outbox import (
    DeltaBodyConflict,
    drain_conversation_delta_outbox,
    enqueue_conversation_delta,
    outbox_status,
)


def body(checksum="a" * 64):
    return {
        "producer": "pk-sync",
        "scope": "agent.conversation",
        "source_checksum": "b" * 64,
        "canonical_checksum": checksum,
        "watermark": checksum,
        "publication_version": "v1",
        "occurred_at": "2026-09-05T00:00:00.000Z",
        "idempotency_key": "sync-" + checksum,
        "committed": True,
    }


def test_failure_survives_restart_and_retry_marks_sent(tmp_path):
    path = tmp_path / "outbox.sqlite"
    queued = enqueue_conversation_delta(body(), path=path)
    assert queued["status"] == "pending"
    assert outbox_status(path=path)["pending"] == 1

    assert drain_conversation_delta_outbox(path=path, sender=lambda _: {"published": False, "reason": "offline"})["sent"] == 0
    assert outbox_status(path=path)["pending"] == 1

    sent = drain_conversation_delta_outbox(
        path=path,
        sender=lambda _: {"published": True, "event_id": "pi_evt_1", "sequence": 7},
    )
    assert sent["sent"] == 1
    assert outbox_status(path=path)["pending"] == 0
    assert drain_conversation_delta_outbox(path=path, sender=lambda _: pytest.fail("duplicate send"))["sent"] == 0


def test_same_event_is_idempotent_but_divergent_body_is_rejected(tmp_path):
    path = tmp_path / "outbox.sqlite"
    first = enqueue_conversation_delta(body(), path=path)
    replay = enqueue_conversation_delta(body(), path=path)
    assert replay["idempotent"] is True
    assert replay["body"] == first["body"]
    changed = body()
    changed["watermark"] = "c" * 64
    with pytest.raises(DeltaBodyConflict):
        enqueue_conversation_delta(changed, path=path)


def test_repeated_checksum_with_new_observation_time_keeps_original_body(tmp_path):
    path = tmp_path / "outbox.sqlite"
    first = body()
    original = enqueue_conversation_delta(first, path=path)
    changed_observation = dict(first, occurred_at="2026-09-06T00:00:00.000Z", publication_version="v2")
    replay = enqueue_conversation_delta(changed_observation, path=path)
    assert replay["idempotent"] is True
    assert replay["body"] == original["body"]


def test_metadata_only_and_invalid_precommit_do_not_create_ledger(tmp_path):
    path = tmp_path / "outbox.sqlite"
    for invalid in ({"committed": False}, {"committed": True, "prompt": "secret"}):
        with pytest.raises(ValueError):
            enqueue_conversation_delta(invalid, path=path)
    assert not path.exists()


def test_status_is_recoverable_from_sqlite_file(tmp_path):
    path = tmp_path / "outbox.sqlite"
    enqueue_conversation_delta(body(), path=path)
    assert json.loads(json.dumps(outbox_status(path=path))) == {"pending": 1, "sent": 0, "total": 1}


def test_concurrent_drains_claim_one_delivery(tmp_path):
    path = tmp_path / "outbox.sqlite"
    enqueue_conversation_delta(body(), path=path)
    entered, release = Event(), Event()
    calls = []

    def sender(event):
        calls.append(event["idempotency_key"])
        entered.set()
        assert release.wait(5)
        return {"published": True, "event_id": "pi_evt_once", "sequence": 1}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(drain_conversation_delta_outbox, path=path, sender=sender)
        assert entered.wait(5)
        second = pool.submit(drain_conversation_delta_outbox, path=path, sender=sender)
        try:
            assert second.result(timeout=2)["sent"] == 0
        finally:
            release.set()
        assert first.result(timeout=5)["sent"] == 1
    assert len(calls) == 1


def test_invalid_source_checksum_never_enters_queue(tmp_path):
    path = tmp_path / "outbox.sqlite"
    with pytest.raises(ValueError):
        enqueue_conversation_delta(dict(body(), source_checksum="bad-checksum"), path=path)
    assert not path.exists()


def test_expired_lease_recovers_after_sender_process_crash(tmp_path, monkeypatch):
    import personal_knowledge.application.conversation.delta_outbox as outbox
    path = tmp_path / "outbox.sqlite"
    enqueue_conversation_delta(body(), path=path)
    monkeypatch.setattr(outbox.time, "time", lambda: 1000)

    def crash(_):
        raise SystemExit("simulated process termination before ack")

    with pytest.raises(SystemExit):
        drain_conversation_delta_outbox(path=path, sender=crash)
    assert drain_conversation_delta_outbox(path=path, sender=lambda _: pytest.fail("lease still live"))["sent"] == 0
    monkeypatch.setattr(outbox.time, "time", lambda: 1061)
    assert drain_conversation_delta_outbox(path=path, sender=lambda _: {"published": True})["sent"] == 1
