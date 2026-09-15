from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from personal_knowledge.intelligence.proactive.controls import ControlCommand, ControlTarget, append_control
from personal_knowledge.intelligence.proactive.schema import checksum
from tests.unit.test_proactive_controls import ACTOR, AS_OF, _published_candidate


def _command(target, key, *, expected=0, operation="suppress"):
    return ControlCommand(target, operation, "global", "user", ACTOR, expected, key,
                          "user_declared", AS_OF, None, None, {})


def _count(db):
    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM proactive_control_events").fetchone()[0]


def test_concurrent_same_command_converges_and_stale_writer_fails(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    command = _command(target, "same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: append_control(db, command, write=True), range(2)))
    assert sorted((r.written, r.existing) for r in results) == [(False, True), (True, False)]
    assert _count(db) == 1
    with pytest.raises(ValueError, match="stale_sequence"):
        append_control(db, _command(target, "stale", expected=0, operation="revoke"), write=True)
    assert _count(db) == 1


def test_idempotency_conflict_target_drift_and_fault_rollback(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    append_control(db, _command(target, "same"), write=True)
    with pytest.raises(ValueError, match="idempotency_conflict"):
        append_control(db, _command(target, "same", expected=1, operation="revoke"), write=True)
    drifted = ControlTarget(target.authority, target.record_type, target.record_id, "0" * 64)
    with pytest.raises(ValueError, match="target_drift"):
        append_control(db, _command(drifted, "drift"), write=True)
    with pytest.raises(RuntimeError, match="injected"):
        append_control(db, _command(target, "fault", expected=1), write=True, inject_failure=True)
    assert _count(db) == 1


def test_tampered_chain_fails_closed_without_new_event(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    append_control(db, _command(target, "one"), write=True)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_proactive_control_events_immutable_update")
    con.execute("UPDATE proactive_control_events SET payload_json='{}'")
    con.commit(); con.close()
    with pytest.raises(ValueError, match="control_event_tampered"):
        append_control(db, _command(target, "two", expected=1), write=True)
    assert _count(db) == 1


def test_semantically_rechecks_projected_restore_receipts(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    append_control(db, _command(target, "one"), write=True)
    con = sqlite3.connect(db)
    row = con.execute("SELECT payload_json FROM proactive_control_events").fetchone()
    import json
    payload = json.loads(row[0])
    payload["after_projected_checksum"] = "0" * 64
    con.execute("DROP TRIGGER trg_proactive_control_events_immutable_update")
    con.execute("UPDATE proactive_control_events SET payload_json=?,payload_checksum=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), checksum(payload)))
    con.commit(); con.close()
    with pytest.raises(ValueError, match="control_projection_tampered"):
        append_control(db, _command(target, "two", expected=1), write=True)
    assert _count(db) == 1
