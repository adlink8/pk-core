"""Protocol-faithful non-causal paired calibration verdict."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum, stable_id
from .protocols import REQUIRED_METRICS


def evaluate_protocol(
    db_path: Path | str, protocol_id: str, *, arm_outcomes: Mapping[str, Mapping[str, Any]],
    as_of: str, protocol_deviations: tuple[str,...]=(), confounders: tuple[str,...]=(),
) -> dict[str,Any]:
    con=sqlite3.connect(db_path); con.row_factory=sqlite3.Row
    try:
        protocol_row=con.execute("SELECT * FROM calibration_protocols WHERE protocol_id=?",(protocol_id,)).fetchone()
        arms=con.execute("SELECT * FROM calibration_arms WHERE protocol_id=? ORDER BY arm_kind",(protocol_id,)).fetchall()
        members=con.execute("SELECT COUNT(*) FROM calibration_cohort_members WHERE protocol_id=?",(protocol_id,)).fetchone()[0]
    finally: con.close()
    if protocol_row is None or len(arms)!=2: raise ValueError("paired_evidence_incomplete")
    protocol=json.loads(protocol_row["payload_json"])
    missing=[]; metrics={}
    for arm in arms:
        kind=arm["arm_kind"]; outcome=dict(arm_outcomes.get(kind) or {})
        metrics[kind]={name:outcome.get(name) for name in REQUIRED_METRICS}
        missing.extend(f"{kind}:{name}" for name,value in metrics[kind].items() if value is None)
    reasons=[]
    if members < protocol["minimum_evidence"]: reasons.append("sample_below_minimum")
    if as_of < protocol["observation_window"]["end"]: reasons.append("missing_window")
    if missing: reasons.append("missing_measurements")
    if protocol_deviations: reasons.append("protocol_deviation")
    if confounders: reasons.append("confounded_or_ambiguous")
    lower_better={"time_deviation","cost_deviation","side_effects","regret","abstention"}
    gains={}
    if not reasons:
        for name in REQUIRED_METRICS:
            p=float(metrics["personalized"][name]); g=float(metrics["generic"][name])
            gains[name]=(g-p) if name in lower_better else (p-g)
        failed=[name for name,gain in gains.items() if gain<float(protocol["thresholds"][name])]
        status="FAIL" if failed else "PASS"
        if failed: reasons.append("threshold_not_met")
    else:
        status="INCONCLUSIVE"
        failed=[]
        gains={name:None for name in REQUIRED_METRICS}
    payload={"protocol_id":protocol_id,"protocol_checksum":protocol_row["payload_checksum"],"status":status,
             "metrics":metrics,"missing":missing,"protocol_deviations":list(protocol_deviations),
             "confounders":list(confounders),"gains":gains,"failed_thresholds":failed,
             "reason_codes":reasons,"causal_claim":False,"as_of":as_of}
    digest=checksum(payload); verdict_id=stable_id("calv",payload)
    con=connect_rw(Path(db_path),timeout=30)
    try:
        con.execute("INSERT INTO calibration_verdicts VALUES (?,?,?,?,?,?)",
                    (verdict_id,protocol_id,status,canonical_json(payload),digest,as_of)); con.commit()
    finally: con.close()
    return {"verdict_id":verdict_id,"verdict_checksum":digest,**payload}


__all__=["evaluate_protocol"]
