"""Checksum-verifying calibration reads and metadata-only acceptance."""
from __future__ import annotations

import hashlib,json
from pathlib import Path
import sqlite3
from typing import Any
from personal_knowledge.intelligence.analysis.schema import checksum
from .schema import TABLES,inspect_schema


def _fingerprint(path:Path|str)->str:
    d=hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): d.update(chunk)
    return d.hexdigest()


def _rows(db_path:Path|str,table:str,protocol_id:str)->list[dict[str,Any]]:
    con=sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro",uri=True); con.row_factory=sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        rows=con.execute(f"SELECT * FROM {table} WHERE protocol_id=?",(protocol_id,)).fetchall()
    finally: con.close()
    result=[]
    for row in rows:
        item=dict(row)
        for key in ("payload_json","value_json","request_json"):
            if key in item and item[key] is not None:
                payload=json.loads(item[key]); item[key.removesuffix("_json")]=payload
                digest_key="payload_checksum" if key!="request_json" else "request_checksum"
                if digest_key in item:
                    expected=checksum(payload if key!="request_json" else {k:v for k,v in payload.items() if k!="request_checksum"})
                    if expected!=item[digest_key]: raise ValueError(f"{table}_checksum_mismatch")
        result.append(item)
    return result


def explain(db_path:Path|str,protocol_id:str)->dict[str,Any]:
    return {"protocol":_rows(db_path,"calibration_protocols",protocol_id),
            "cohort":_rows(db_path,"calibration_cohort_members",protocol_id),
            "arms":_rows(db_path,"calibration_arms",protocol_id),
            "measurements":_rows(db_path,"calibration_measurements",protocol_id),
            "verdicts":_rows(db_path,"calibration_verdicts",protocol_id),
            "proposals":_rows(db_path,"calibration_proposals",protocol_id),
            "limitations":["single real cohort member","generic outcome window unavailable","actual token budget deviated"],
            "causal_claim":False,"promotion_available":False,"external_action_available":False}


def acceptance_report(*,db_path:Path|str,protocol_id:str,source_paths:dict[str,Path|str])->dict[str,Any]:
    paths={**source_paths,"calibration":db_path}; before={k:_fingerprint(v) for k,v in paths.items()}
    view=explain(db_path,protocol_id); schema=inspect_schema(db_path)
    after={k:_fingerprint(v) for k,v in paths.items()}; unchanged=before==after
    verdict=view["verdicts"][0] if view["verdicts"] else None
    return {"ok":unchanged and schema["schema_state"]=="applied","metadata_only":True,"protocol_id":protocol_id,
            "verdict":verdict["verdict_status"] if verdict else None,"view":view,"schema":schema,
            "fingerprints_before":before,"fingerprints_after":after,"unchanged":unchanged,
            "provider_calls":0,"network_calls":0,"external_actions":0,"source_writes":0,"promotions":0}


__all__=["acceptance_report","explain"]
