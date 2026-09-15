from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from personal_knowledge.intelligence.cli import main as cli_main
from personal_knowledge.intelligence.runs import plan_run, publish_run
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.services.api_server import intelligence_rest_contract
from personal_knowledge.services.mcp_server import (
    CORE_TOOL_NAMES,
    intelligence_tool_contract,
)
from tests.integration.test_personal_state_runs import StubResolver, _assertion, _database


def _service(tmp_path):
    db_path = _database(tmp_path)
    resolver = StubResolver()
    first = plan_run(
        db_path,
        [_assertion(value="Target C")],
        producer_version="producer-v1",
        input_manifest={"source": "fixture-1"},
        resolver=resolver,
    )
    second_assertion = replace(
        _assertion(value="Target D"),
        valid_from="2026-07-18T00:00:00Z",
        observed_at="2026-07-18T00:00:00Z",
    )
    second = plan_run(
        db_path,
        [second_assertion],
        producer_version="producer-v2",
        input_manifest={"source": "fixture-2"},
        resolver=resolver,
    )
    publish_run(db_path, first, write=True, resolver=resolver)
    publish_run(db_path, second, write=True, resolver=resolver)
    return db_path, IntelligenceService(db_path, resolver=resolver), first, second


def _counts(db_path):
    con = sqlite3.connect(db_path)
    try:
        return tuple(
            con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "personal_state_runs",
                "personal_state_assertions",
                "personal_state_evidence",
                "personal_state_changes",
                "personal_state_risks",
            )
        )
    finally:
        con.close()


def test_all_four_operations_share_snapshot_run_and_metadata_contract(tmp_path) -> None:
    db_path, service, _, second = _service(tmp_path)
    before = _counts(db_path)
    common = {"snapshot_id": "ss1", "run_id": second.run_id}
    current = service.invoke("state.current", **common)
    history = service.invoke("state.history", **common)
    recent = service.invoke(
        "changes.recent", **common, window_start="2026-07-17T00:00:00Z"
    )
    explain = service.invoke(
        "state.explain",
        **common,
        assertion_kind="goal",
        subject="user",
        domain="work",
        scope="personal",
        predicate="complete_target",
    )

    for result in (current, history, recent, explain):
        assert result["ok"] is True
        assert result["schema_version"] == "personal_state_interface_v1"
        assert result["snapshot"] == {
            "snapshot_id": "ss1",
            "snapshot_hash": "snapshot-hash-1",
        }
        assert result["run"]["run_id"] == second.run_id
        assert result["run"]["run_checksum"] == second.output_manifest_checksum
        assert result["privacy"] == {"metadata_only": True, "private_bodies": 0}
        serialized = json.dumps(result, ensure_ascii=False)
        assert "Target C" not in serialized and "Target D" not in serialized
        assert "content" not in serialized and "raw_text" not in serialized
    assert _counts(db_path) == before


def test_empty_uncertain_and_error_contracts_are_stable(tmp_path) -> None:
    _, service, _, second = _service(tmp_path)
    empty = service.invoke(
        "changes.recent",
        run_id=second.run_id,
        window_start="2026-07-18T00:00:00Z",
        as_of="2026-07-18T00:00:00Z",
    )
    uncertain = service.invoke(
        "state.explain",
        run_id=second.run_id,
        assertion_kind="constraint",
        subject="missing",
        domain="work",
        scope="personal",
        predicate="unknown",
    )
    bad_limit = service.invoke("state.current", run_id=second.run_id, limit=0)
    crossed = service.invoke("state.current", snapshot_id="ss2", run_id=second.run_id)

    assert empty["status"] == "empty"
    assert empty["data"]["items"] == []
    assert uncertain["status"] == "uncertain"
    assert uncertain["data"]["state_status"] == "unknown"
    assert bad_limit["error"]["code"] == "invalid_limit"
    assert crossed["error"]["code"] == "cross_snapshot_run"
    assert bad_limit["status"] == crossed["status"] == "error"


def test_cli_matches_shared_backend_and_write_is_rejected(tmp_path, capsys) -> None:
    db_path, service, _, second = _service(tmp_path)
    expected = service.invoke("state.current", run_id=second.run_id, limit=3)
    code = cli_main([
        "--db", str(db_path), "state", "current",
        "--run-id", second.run_id, "--limit", "3", "--json",
    ])
    actual = json.loads(capsys.readouterr().out)
    assert code == 0
    assert actual == expected

    code = cli_main(["build", "--write", "--json"])
    rejected = json.loads(capsys.readouterr().out)
    assert code == 2
    assert rejected["error"]["code"] == "write_not_available"


def test_cli_rest_and_mcp_use_identical_normalized_contract(tmp_path, capsys) -> None:
    db_path, _, _, second = _service(tmp_path)
    service = IntelligenceService(db_path)
    cases = (
        (
            "state.current", "personal_state_current",
            {"run_id": second.run_id, "limit": 3},
            ["state", "current", "--run-id", second.run_id, "--limit", "3", "--json"],
        ),
        (
            "state.history", "personal_state_history",
            {"run_id": second.run_id, "limit": 3},
            ["state", "history", "--run-id", second.run_id, "--limit", "3", "--json"],
        ),
        (
            "changes.recent", "personal_changes_recent",
            {
                "run_id": second.run_id,
                "window_start": "2026-07-17T00:00:00Z",
                "limit": 3,
            },
            [
                "changes", "recent", "--run-id", second.run_id,
                "--window-start", "2026-07-17T00:00:00Z",
                "--limit", "3", "--json",
            ],
        ),
        (
            "state.explain", "personal_state_explain",
            {
                "run_id": second.run_id,
                "assertion_kind": "goal",
                "subject": "user",
                "domain": "work",
                "scope": "personal",
                "predicate": "complete_target",
            },
            [
                "state", "explain", "--run-id", second.run_id,
                "--assertion-kind", "goal", "--subject", "user",
                "--domain", "work", "--scope", "personal",
                "--predicate", "complete_target", "--json",
            ],
        ),
    )
    for operation, tool_name, params, cli_args in cases:
        expected = service.invoke(operation, **params)
        rest_params = {
            key: str(value) if key == "limit" else value for key, value in params.items()
        }
        rest = intelligence_rest_contract(operation, rest_params, db_path=db_path)
        mcp = intelligence_tool_contract(tool_name, params, db_path=db_path)
        code = cli_main(["--db", str(db_path), *cli_args])
        cli = json.loads(capsys.readouterr().out)
        assert code == 0
        assert cli == rest == mcp == expected
    assert {
        "personal_state_current",
        "personal_state_history",
        "personal_changes_recent",
        "personal_state_explain",
    } <= CORE_TOOL_NAMES


def test_transport_validation_errors_are_equivalent(tmp_path) -> None:
    db_path, service, _, second = _service(tmp_path)
    expected = service.invoke("state.current", run_id=second.run_id, limit=0)
    rest = intelligence_rest_contract(
        "state.current", {"run_id": second.run_id, "limit": "0"}, db_path=db_path
    )
    mcp = intelligence_tool_contract(
        "personal_state_current", {"run_id": second.run_id, "limit": 0}, db_path=db_path
    )
    assert expected == rest == mcp


@pytest.mark.parametrize(
    ("statement", "error_code"),
    [
        ("UPDATE personal_state_assertions SET value_json='\"tampered\"'", "assertion_payload_mismatch"),
        ("UPDATE personal_state_assertions SET payload_checksum=printf('%064d',0)", "assertion_payload_checksum_mismatch"),
        ("UPDATE personal_state_evidence SET privacy_class='R3'", "evidence_integrity_mismatch"),
        ("UPDATE personal_state_evidence SET artifact_version_id='av2'", "evidence_snapshot_mismatch"),
    ],
)
def test_read_hydration_fails_closed_on_row_tampering(tmp_path, statement, error_code) -> None:
    db_path, _, _, second = _service(tmp_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.executescript(
        "DROP TRIGGER trg_personal_state_assertions_immutable_update;"
        "DROP TRIGGER trg_personal_state_evidence_immutable_update;"
    )
    con.execute(
        statement + " WHERE assertion_id=(SELECT assertion_id FROM personal_state_assertions WHERE run_id=? LIMIT 1)", (second.run_id,)
    )
    con.commit()
    con.close()
    result = IntelligenceService(db_path, resolver=StubResolver()).invoke(
        "state.current", run_id=second.run_id
    )
    assert result["ok"] is False
    assert result["error"]["code"] == error_code


def test_as_of_excludes_assertions_before_observation_and_future_formation(tmp_path) -> None:
    _, service, first, second = _service(tmp_path)
    as_of = "2026-07-17T12:00:00Z"
    current = service.invoke("state.current", run_id=second.run_id, as_of=as_of)
    history = service.invoke("state.history", run_id=second.run_id, as_of=as_of)
    explain = service.invoke(
        "state.explain", run_id=second.run_id, as_of=as_of,
        assertion_kind="goal", subject="user", domain="work",
        scope="personal", predicate="complete_target",
    )
    assert current["data"]["items"][0]["current_assertion_id"] == first.assertions[0].assertion_id
    assert {row["assertion_id"] for row in history["data"]["items"]} == {
        first.assertions[0].assertion_id
    }
    assert [row["assertion_id"] for row in explain["data"]["formation_path"]] == [
        first.assertions[0].assertion_id
    ]


def test_explicit_publication_sequence_orders_same_second_and_survives_vacuum(
    tmp_path, monkeypatch
) -> None:
    import personal_knowledge.intelligence.runs as runs_module

    db_path = _database(tmp_path)
    resolver = StubResolver()
    first = plan_run(
        db_path, [_assertion(value="first")], producer_version="same-second-1",
        input_manifest={"source": "first"}, resolver=resolver,
    )
    second = plan_run(
        db_path, [_assertion(value="second")], producer_version="same-second-2",
        input_manifest={"source": "second"}, resolver=resolver,
    )
    monkeypatch.setattr(runs_module, "_now", lambda: "2026-07-18T00:00:00Z")
    publish_run(db_path, first, write=True, resolver=resolver)
    publish_run(db_path, second, write=True, resolver=resolver)
    con = sqlite3.connect(db_path)
    before = con.execute(
        "SELECT publication_sequence,run_id FROM personal_state_publications ORDER BY publication_sequence"
    ).fetchall()
    con.execute("VACUUM")
    after = con.execute(
        "SELECT publication_sequence,run_id FROM personal_state_publications ORDER BY publication_sequence"
    ).fetchall()
    con.close()
    assert before == after == [(1, first.run_id), (2, second.run_id)]
    service = IntelligenceService(db_path, resolver=resolver)
    assert service.invoke("state.current")["run"]["run_id"] == second.run_id
    assert service.invoke("state.history", run_id=first.run_id)["run"]["run_id"] == first.run_id


def test_concurrent_publications_receive_one_total_order(tmp_path, monkeypatch) -> None:
    import personal_knowledge.intelligence.runs as runs_module

    db_path = _database(tmp_path)
    resolver = StubResolver()
    plans = tuple(
        plan_run(
            db_path, [_assertion(value=f"v-{i}")], producer_version=f"concurrent-{i}",
            input_manifest={"source": f"concurrent-{i}"}, resolver=resolver,
        )
        for i in range(2)
    )
    monkeypatch.setattr(runs_module, "_now", lambda: "2026-07-18T00:00:00Z")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda run: publish_run(db_path, run, write=True, resolver=resolver), plans))
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT publication_sequence,run_id FROM personal_state_publications ORDER BY publication_sequence"
    ).fetchall()
    con.close()
    assert [row[0] for row in rows] == [1, 2]
    assert IntelligenceService(db_path, resolver=resolver).invoke("state.current")["run"]["run_id"] == rows[-1][1]
