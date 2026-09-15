from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from personal_knowledge.intelligence.pilot.cases import admit_project_case

from tests.integration.test_project_pilot_authority import setup_authorities


def test_admitted_option_preserves_confirmed_input_and_remains_non_authoritative(tmp_path: Path) -> None:
    env = setup_authorities(tmp_path)
    result = admit_project_case(
        pilot_db_path=env["pilot"], analysis_db_path=env["analysis"],
        personal_db_path=env["personal"], external_db_path=env["external"],
        run_id=env["run_id"], candidate_id=env["candidate_id"],
        selected_option_id="validate-first", case_confirmation_event_id="case-confirm-1",
        now="2026-07-18T09:30:00Z",
    )
    assert result.status == "candidate" and not result.written
    assert result.case is not None and result.recommendation is not None
    assert result.case.goal == "Choose a compatible local runtime"
    assert result.case.constraints == ("no deployment", "manual action only")
    assert result.case.weights == {"safety": 0.7, "speed": 0.3}
    assert result.case.risk_budget == "low" and result.case.no_action_baseline
    assert len(result.case.alternatives) == 2 and result.case.stop_conditions
    assert result.recommendation.option_id == "validate-first"
    payload = asdict(result.recommendation)
    assert payload["status"] == "candidate"
    assert "decision" not in payload and "action" not in payload
    assert result.authority_fingerprints_before == result.authority_fingerprints_after


def test_missing_or_unadmitted_option_abstains_without_pilot_write(tmp_path: Path) -> None:
    env = setup_authorities(tmp_path)
    result = admit_project_case(
        pilot_db_path=env["pilot"], analysis_db_path=env["analysis"],
        personal_db_path=env["personal"], external_db_path=env["external"],
        run_id=env["run_id"], candidate_id=env["candidate_id"],
        selected_option_id="not-present", case_confirmation_event_id="case-confirm-2",
        write=True, now="2026-07-18T09:30:00Z",
    )
    assert result.status == "abstain"
    assert result.reason_codes == ("analysis_option_missing",)
    import sqlite3
    con = sqlite3.connect(env["pilot"])
    assert con.execute("SELECT COUNT(*) FROM pilot_cases").fetchone()[0] == 0
    con.close()
