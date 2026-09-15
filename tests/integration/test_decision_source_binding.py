from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.intelligence.decision.cli import main as cli_main
from personal_knowledge.intelligence.decision.effectiveness import (
    EffectivenessRule,
    assess_outcome,
    load_outcome,
)
from personal_knowledge.intelligence.decision.state_machine import (
    DecisionStateError,
    record_assessment,
)
from tests.integration.test_decision_feedback_concurrency import (
    _action,
    _complete,
    _confirm,
    _outcome,
    _published,
)


def _stream_counts(db: Path, recommendation_id: str) -> tuple[int, int, int, int, int]:
    con = sqlite3.connect(db)
    try:
        typed = tuple(
            con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()[0]
            for table in (
                "decision_confirmations",
                "decision_actions",
                "decision_outcomes",
                "decision_effectiveness",
            )
        )
        events = con.execute(
            "SELECT COUNT(*) FROM decision_events WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()[0]
        return (*typed, events)
    finally:
        con.close()


def _tamper_source(db: Path, kind: str) -> None:
    con = sqlite3.connect(db)
    try:
        if kind == "output_manifest":
            con.execute("DROP TRIGGER trg_personal_state_runs_immutable_update")
            con.execute("UPDATE personal_state_runs SET output_manifest_json='{}'")
        elif kind == "input_checksum":
            con.execute("DROP TRIGGER trg_personal_state_runs_immutable_update")
            con.execute("UPDATE personal_state_runs SET input_manifest_checksum=?", ("0" * 64,))
        elif kind == "publication_sequence":
            con.execute("DROP TRIGGER trg_personal_state_publications_immutable_update")
            con.execute("UPDATE personal_state_publications SET publication_sequence=publication_sequence+7")
        elif kind == "snapshot":
            con.execute("DROP TRIGGER trg_personal_state_runs_immutable_update")
            con.execute("DROP TRIGGER trg_personal_state_runs_snapshot_match")
            con.execute("UPDATE personal_state_runs SET snapshot_hash='tampered-snapshot-hash'")
        else:  # pragma: no cover - test helper contract
            raise AssertionError(kind)
        con.commit()
    finally:
        con.close()


def test_cli_confirmation_rejects_phase25_output_manifest_tamper_without_append(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, rec = _published(tmp_path)
    before = _stream_counts(db, rec.recommendation_id)
    _tamper_source(db, "output_manifest")

    code = cli_main([
        "--db", str(db), "confirm", "--recommendation-id", rec.recommendation_id,
        "--recommendation-checksum", rec.payload_checksum, "--decision", "accept",
        "--actor-class", "user", "--actor-identity-hash", "1" * 64,
        "--reason-code", "chosen", "--expected-sequence", "1",
        "--idempotency-key", "source-drift-confirm", "--occurred-at", "2026-07-18T01:00:00Z",
        "--write", "--i-confirm", rec.recommendation_id, "--json",
    ])

    result = json.loads(capsys.readouterr().out)
    assert code == 2
    assert result["error"]["code"] == "source_binding_invalid"
    assert _stream_counts(db, rec.recommendation_id) == before


def test_action_rejects_phase25_input_checksum_tamper_without_append(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _confirm(db, rec)
    before = _stream_counts(db, rec.recommendation_id)
    _tamper_source(db, "input_checksum")

    with pytest.raises(DecisionStateError, match="source_binding_invalid"):
        _action(db, rec)

    assert _stream_counts(db, rec.recommendation_id) == before


def test_outcome_rejects_phase25_publication_drift_without_append(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    before = _stream_counts(db, rec.recommendation_id)
    _tamper_source(db, "publication_sequence")

    with pytest.raises(DecisionStateError, match="source_binding_invalid"):
        _outcome(db, rec)

    assert _stream_counts(db, rec.recommendation_id) == before


def test_assessment_rejects_phase25_snapshot_drift_without_append(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    outcome_receipt = _outcome(db, rec)
    outcome = load_outcome(db, outcome_receipt.record_id)
    assessment = assess_outcome(
        outcome,
        EffectivenessRule(
            "observed_goal_attainment", "1", "focus_blocks", "count/week", "increase", 86400
        ),
        action_state="completed",
    )
    before = _stream_counts(db, rec.recommendation_id)
    _tamper_source(db, "snapshot")

    with pytest.raises(DecisionStateError, match="source_binding_invalid"):
        record_assessment(
            db,
            assessment=assessment,
            expected_sequence=6,
            idempotency_key="source-drift-assessment",
            occurred_at="2026-07-25T01:02:00Z",
        )

    assert _stream_counts(db, rec.recommendation_id) == before
