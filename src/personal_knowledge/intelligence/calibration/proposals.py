"""Immutable, reversible calibration proposals without promotion authority."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum, stable_id


def create_proposal(db_path: Path | str, protocol_id: str, *, parent_version: str,
                    parent_checksum: str, proposal_kind: str, changes: Mapping[str,Any],
                    rationale: tuple[str,...], created_at: str) -> dict[str,Any]:
    if proposal_kind not in {"policy","prompt","threshold"} or not changes or not rationale: raise ValueError("proposal_invalid")
    con=sqlite3.connect(db_path); con.row_factory=sqlite3.Row
    try: verdict=con.execute("SELECT * FROM calibration_verdicts WHERE protocol_id=?",(protocol_id,)).fetchone()
    finally: con.close()
    if verdict is None: raise ValueError("verdict_missing")
    payload={"protocol_id":protocol_id,"verdict_id":verdict["verdict_id"],"verdict_checksum":verdict["payload_checksum"],
             "parent_version":parent_version,"parent_checksum":parent_checksum,"proposal_kind":proposal_kind,
             "changes":dict(changes),"rationale":list(rationale),"rollback_to":{"version":parent_version,"checksum":parent_checksum},
             "auto_promote":False,"historical_rewrite":False}
    digest=checksum(payload); proposal_id=stable_id("calpr",payload)
    con=connect_rw(Path(db_path),timeout=30)
    try:
        con.execute("INSERT INTO calibration_proposals VALUES (?,?,?,?,?,?,?,?,?)",
                    (proposal_id,protocol_id,verdict["verdict_id"],"candidate",parent_version,parent_checksum,canonical_json(payload),digest,created_at)); con.commit()
    finally: con.close()
    return {"proposal_id":proposal_id,"proposal_checksum":digest,"status":"candidate",**payload}


def record_proposal_control(db_path: Path | str, proposal_id: str, *, action: str,
                            reason: str, created_at: str) -> dict[str,Any]:
    if action not in {"rejected","revoked","restored"}: raise ValueError("proposal_control_invalid")
    con=sqlite3.connect(db_path); con.row_factory=sqlite3.Row
    try: source=con.execute("SELECT * FROM calibration_proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
    finally: con.close()
    if source is None: raise ValueError("proposal_missing")
    if source["proposal_status"] != "candidate": raise ValueError("proposal_control_target_invalid")
    if action == "restored":
        con=sqlite3.connect(db_path)
        try:
            rows=con.execute("SELECT payload_json FROM calibration_proposals WHERE protocol_id=? AND proposal_status='revoked'",(source["protocol_id"],)).fetchall()
        finally: con.close()
        if not any(__import__("json").loads(row[0]).get("target_proposal_id")==proposal_id for row in rows):
            raise ValueError("proposal_revoke_required")
    payload={"target_proposal_id":proposal_id,"target_checksum":source["payload_checksum"],"action":action,
             "reason":reason,"compensating":True,"promotion_performed":False}
    digest=checksum(payload); control_id=stable_id("calpr",payload)
    con=connect_rw(Path(db_path),timeout=30)
    try:
        con.execute("INSERT INTO calibration_proposals VALUES (?,?,?,?,?,?,?,?,?)",
                    (control_id,source["protocol_id"],source["verdict_id"],action,source["parent_version"],source["parent_checksum"],canonical_json(payload),digest,created_at)); con.commit()
    finally: con.close()
    return {"proposal_id":control_id,"proposal_checksum":digest,"status":action,**payload}


__all__=["create_proposal","record_proposal_control"]
