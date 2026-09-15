from __future__ import annotations

from pathlib import Path

from personal_knowledge.intelligence.calibration.evaluation import evaluate_protocol
from personal_knowledge.intelligence.calibration.paired import build_paired_requests,freeze_arm_assignments
from personal_knowledge.intelligence.calibration.protocols import freeze_protocol,REQUIRED_METRICS
from tests.integration.test_calibration_authority import setup_protocol


def _ready(tmp_path:Path):
    env,db,p=setup_protocol(tmp_path); freeze_protocol(db,env["pilot"],p,write=True)
    import sqlite3
    con=sqlite3.connect(db); m=con.execute("SELECT member_id FROM calibration_cohort_members").fetchone()[0]; con.close()
    arms=build_paired_requests(db,p.protocol_id,member_id=m,external_context={"runtime":"current"},personal_context={"workflow":"evidence-gated"})
    freeze_arm_assignments(db,p.protocol_id,member_id=m,arms=arms,created_at="2026-07-18T13:01:00Z")
    return db,p


def test_small_real_cohort_and_missing_generic_outcome_are_inconclusive(tmp_path:Path)->None:
    db,p=_ready(tmp_path)
    personal={name:1 for name in REQUIRED_METRICS}; generic={name:None for name in REQUIRED_METRICS}
    verdict=evaluate_protocol(db,p.protocol_id,arm_outcomes={"personalized":personal,"generic":generic},as_of="2026-07-18T15:01:00Z")
    assert verdict["status"]=="INCONCLUSIVE" and not verdict["causal_claim"]
    assert "sample_below_minimum" in verdict["reason_codes"] and "missing_measurements" in verdict["reason_codes"]


def test_incomplete_window_or_confounder_never_becomes_gain(tmp_path:Path)->None:
    db,p=_ready(tmp_path)
    values={name:1 for name in REQUIRED_METRICS}
    verdict=evaluate_protocol(db,p.protocol_id,arm_outcomes={"personalized":values,"generic":values},
                              as_of="2026-07-18T14:30:00Z",confounders=("rubric ambiguity",))
    assert verdict["status"]=="INCONCLUSIVE"
    assert {"sample_below_minimum","missing_window","confounded_or_ambiguous"}<=set(verdict["reason_codes"])


def test_sufficient_complete_evidence_below_threshold_is_fail(tmp_path:Path)->None:
    db,p=_ready(tmp_path)
    import sqlite3,json
    from personal_knowledge.intelligence.analysis.schema import canonical_json,checksum,stable_id
    payload={"protocol_id":p.protocol_id,"ordinal":1,"case_id":"case-2","case_checksum":"4"*64,"outcome_event_checksum":"5"*64}
    con=sqlite3.connect(db)
    con.execute("INSERT INTO calibration_cohort_members VALUES (?,?,?,?,?,?,?,?,?)",
                (stable_id("calm",payload),p.protocol_id,1,"case-2","4"*64,"5"*64,canonical_json(payload),checksum(payload),"2026-07-18T13:00:00Z"))
    con.commit(); con.close()
    good={name:1 for name in REQUIRED_METRICS}; worse={name:0 for name in REQUIRED_METRICS}
    verdict=evaluate_protocol(db,p.protocol_id,arm_outcomes={"personalized":worse,"generic":good},as_of="2026-07-18T15:01:00Z")
    assert verdict["status"]=="FAIL" and verdict["reason_codes"]==["threshold_not_met"]
    assert verdict["failed_thresholds"]
