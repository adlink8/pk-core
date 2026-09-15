from __future__ import annotations

import json
import sqlite3

from personal_knowledge.intelligence.decision.cli import main as cli_main
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.services.api_server import decision_rest_contract
from personal_knowledge.services.mcp_server import (
    CORE_TOOL_NAMES,
    decision_tool_contract,
)
from tests.integration.test_decision_feedback_concurrency import _published


def test_shared_reads_are_transport_equivalent_and_metadata_only(tmp_path, capsys) -> None:
    db, rec = _published(tmp_path)
    cases = (
        ("recommendations.list", "decision_recommendations_list", {"limit": 5}, ["recommendations", "list", "--limit", "5"]),
        ("recommendations.get", "decision_recommendations_get", {"recommendation_id": rec.recommendation_id}, ["recommendations", "get", "--recommendation-id", rec.recommendation_id]),
        ("recommendations.history", "decision_recommendation_history", {"recommendation_id": rec.recommendation_id, "limit": 5}, ["recommendations", "history", "--recommendation-id", rec.recommendation_id, "--limit", "5"]),
        ("recommendations.outcomes", "decision_recommendation_outcomes", {"recommendation_id": rec.recommendation_id, "limit": 5}, ["recommendations", "outcomes", "--recommendation-id", rec.recommendation_id, "--limit", "5"]),
        ("recommendations.effectiveness", "decision_recommendation_effectiveness", {"recommendation_id": rec.recommendation_id, "limit": 5}, ["recommendations", "effectiveness", "--recommendation-id", rec.recommendation_id, "--limit", "5"]),
    )
    service = DecisionFeedbackService(db)
    for operation, tool, params, cli_args in cases:
        expected = service.invoke(operation, **params)
        assert expected["ok"] is True
        rest = decision_rest_contract(operation, params, db_path=db)
        mcp = decision_tool_contract(tool, params, db_path=db)
        code = cli_main(["--db", str(db), *cli_args, "--json"])
        actual = json.loads(capsys.readouterr().out)
        assert code == 0
        assert actual == rest == mcp == expected
        serialized = json.dumps(expected)
        assert "close_target_d" not in serialized
        assert '"target"' not in serialized
        assert expected["privacy"] == {"metadata_only": True, "private_bodies": 0}
    assert {case[1] for case in cases} <= CORE_TOOL_NAMES


def test_invalid_limit_and_missing_recommendation_are_stable(tmp_path) -> None:
    db, rec = _published(tmp_path)
    service = DecisionFeedbackService(db)
    bad = service.invoke("recommendations.list", limit=0)
    missing = service.invoke("recommendations.get", recommendation_id="missing")
    assert bad["error"]["code"] == "invalid_limit"
    assert missing["error"]["code"] == "recommendation_missing"
    assert decision_rest_contract("recommendations.list", {"limit": "0"}, db_path=db) == bad
    assert decision_tool_contract("decision_recommendations_list", {"limit": 0}, db_path=db) == bad


def test_every_read_hydrates_genesis_and_source_integrity(tmp_path) -> None:
    db, rec = _published(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_decision_events_immutable_update")
    con.execute("UPDATE decision_events SET payload_json='{}' WHERE recommendation_id=? AND sequence=1", (rec.recommendation_id,))
    con.commit(); con.close()
    for operation, params in (
        ("recommendations.list", {}),
        ("recommendations.get", {"recommendation_id": rec.recommendation_id}),
        ("recommendations.history", {"recommendation_id": rec.recommendation_id}),
        ("recommendations.outcomes", {"recommendation_id": rec.recommendation_id}),
        ("recommendations.effectiveness", {"recommendation_id": rec.recommendation_id}),
    ):
        result = DecisionFeedbackService(db).invoke(operation, **params)
        assert result["ok"] is False
        assert result["error"]["code"] == "event_checksum_mismatch"


def test_cli_write_requires_exact_confirmation_and_human_actor(tmp_path, capsys) -> None:
    db, rec = _published(tmp_path)
    base = ["--db", str(db), "confirm", "--recommendation-id", rec.recommendation_id,
            "--recommendation-checksum", rec.payload_checksum, "--decision", "accept",
            "--actor-class", "user", "--actor-identity-hash", "1" * 64,
            "--reason-code", "chosen", "--expected-sequence", "1",
            "--idempotency-key", "k1", "--occurred-at", "2026-07-18T01:00:00Z", "--json"]
    for extra, code in (([], "write_required"), (["--write"], "confirmation_required"),
                        (["--write", "--i-confirm", "wrong"], "confirmation_mismatch")):
        assert cli_main([*base, *extra]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == code
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decision_confirmations").fetchone()[0] == 0
    con.close()


def test_rest_and_mcp_publish_no_decision_mutation_or_execution_surface() -> None:
    forbidden = ("confirm", "accept", "reject", "action", "outcome_write", "execute", "send", "schedule", "purchase", "publish", "dispatch")
    names = {name.lower() for name in CORE_TOOL_NAMES if name.startswith("decision_")}
    assert names
    assert all(not any(word in name for word in forbidden) for name in names)
