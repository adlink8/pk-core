from pathlib import Path
import sqlite3

from personal_knowledge.intelligence.analysis.migrate import migrate
from personal_knowledge.intelligence.analysis.runs import publish_run
from personal_knowledge.intelligence.analysis.service import AnalysisReadService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService
from tests.integration.test_analysis_authority_schema import POLICY, _run


def _analysis_db(tmp_path: Path) -> tuple[Path, str]:
    db = tmp_path / "analysis.sqlite"
    migrate(db, write=True)
    run = _run()
    publish_run(db, run, policy_path=POLICY, write=True)
    return db, run.run_id


def test_analysis_read_service_lists_gets_and_explains_without_provider_body(tmp_path):
    db, run_id = _analysis_db(tmp_path)
    service = AnalysisReadService(db)

    listed = service.list_runs(limit=5)
    detail = service.get_run(run_id)
    explained = service.explain(run_id)

    assert [item["run_id"] for item in listed] == [run_id]
    assert detail["candidate_id"].startswith("dac_")
    assert detail["claims"][0]["evidence"][0]["authority_id"] in {
        "a.personal_change", "s.external_fact"
    }
    assert "request_manifest" not in detail
    assert "response_manifest" not in detail
    assert explained["provider_body_included"] is False
    assert explained["authoritative_decision"] is False


def test_analysis_read_service_fails_closed_on_tamper(tmp_path):
    db, run_id = _analysis_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_analysis_candidates_no_update")
    con.execute("UPDATE analysis_candidates SET payload_json='{}' WHERE run_id=?", (run_id,))
    con.commit()
    con.close()

    result = DecisionIntelligenceReadService(analysis_db=db).invoke(
        "analysis.get", run_id=run_id
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "analysis_candidate_drift"


def test_shared_contract_has_stable_bounds_and_errors(tmp_path):
    db, run_id = _analysis_db(tmp_path)
    service = DecisionIntelligenceReadService(analysis_db=db)

    assert service.invoke("analysis.list", limit=0)["error"]["code"] == "invalid_limit"
    assert service.invoke("analysis.get", run_id="missing")["error"]["code"] == "analysis_run_not_found"
    assert service.invoke("unknown.operation")["error"]["code"] == "unknown_operation"
    result = service.invoke("analysis.explain", run_id=run_id)
    assert result["schema_version"] == "decision_intelligence_read_v1"
    assert result["privacy"] == {
        "metadata_only": True, "provider_bodies": 0, "credentials": 0, "writes": 0
    }
