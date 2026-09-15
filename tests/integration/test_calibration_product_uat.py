from __future__ import annotations
from pathlib import Path
from personal_knowledge.intelligence.calibration.evaluation import evaluate_protocol
from personal_knowledge.intelligence.calibration.paired import build_paired_requests,freeze_arm_assignments
from personal_knowledge.intelligence.calibration.protocols import freeze_protocol,REQUIRED_METRICS
from personal_knowledge.intelligence.calibration.proposals import create_proposal,record_proposal_control
from personal_knowledge.intelligence.calibration.service import acceptance_report,explain
from tests.integration.test_calibration_authority import setup_protocol

def _ready(tmp_path:Path):
    env,db,p=setup_protocol(tmp_path); freeze_protocol(db,env["pilot"],p,write=True)
    import sqlite3
    con=sqlite3.connect(db); m=con.execute("SELECT member_id FROM calibration_cohort_members").fetchone()[0]; con.close()
    arms=build_paired_requests(db,p.protocol_id,member_id=m,external_context={"runtime":"current"},personal_context={"result":1})
    freeze_arm_assignments(db,p.protocol_id,member_id=m,arms=arms,created_at="2026-07-18T13:01:00Z")
    vals={name:1 for name in REQUIRED_METRICS}
    verdict=evaluate_protocol(db,p.protocol_id,arm_outcomes={"personalized":vals,"generic":vals},as_of="2026-07-18T15:01:00Z")
    return env,db,p,verdict

def test_proposal_reject_revoke_restore_are_append_only_and_parent_stable(tmp_path:Path)->None:
    env,db,p,v=_ready(tmp_path)
    parent="4"*64
    proposal=create_proposal(db,p.protocol_id,parent_version="calibration-paired-v1",parent_checksum=parent,
        proposal_kind="policy",changes={"minimum_evidence":4,"enforce_actual_token_budget":True},
        rationale=("current cohort is insufficient","actual tokens exceeded freeze"),created_at="2026-07-18T15:02:00Z")
    import pytest
    with pytest.raises(ValueError,match="proposal_revoke_required"):
        record_proposal_control(db,proposal["proposal_id"],action="restored",reason="too early",created_at="2026-07-18T15:02:30Z")
    reject=record_proposal_control(db,proposal["proposal_id"],action="rejected",reason="insufficient evidence",created_at="2026-07-18T15:03:00Z")
    revoke=record_proposal_control(db,proposal["proposal_id"],action="revoked",reason="rollback drill",created_at="2026-07-18T15:04:00Z")
    restore=record_proposal_control(db,proposal["proposal_id"],action="restored",reason="forward restore drill",created_at="2026-07-18T15:05:00Z")
    assert all(item["target_checksum"]==proposal["proposal_checksum"] for item in (reject,revoke,restore))
    view=explain(db,p.protocol_id)
    assert len(view["proposals"])==4 and not view["promotion_available"] and not view["causal_claim"]
    assert all(item["parent_checksum"]==parent for item in view["proposals"])

def test_metadata_acceptance_reconstructs_all_layers_without_side_effect(tmp_path:Path)->None:
    env,db,p,v=_ready(tmp_path)
    report=acceptance_report(db_path=db,protocol_id=p.protocol_id,source_paths={"pilot":env["pilot"],"personal":env["personal"],"external":env["external"],"analysis":env["analysis"]})
    assert report["ok"] and report["unchanged"] and report["verdict"]=="INCONCLUSIVE"
    assert report["provider_calls"]==report["network_calls"]==report["external_actions"]==report["source_writes"]==report["promotions"]==0
