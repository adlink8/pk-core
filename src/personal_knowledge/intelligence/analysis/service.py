"""Checksum-verifying, provider-body-free reads for decision analysis runs."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from .schema import SCHEMA_VERSION, checksum, stable_id


class AnalysisReadError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _ro(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise AnalysisReadError("database_missing", str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _json(row: sqlite3.Row, field: str, code: str) -> Any:
    try:
        return json.loads(str(row[field]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise AnalysisReadError(code) from exc


class AnalysisReadService:
    """Read committed runs while validating their complete stored checksum graph."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AnalysisReadError("invalid_limit", str(limit))
        con = _ro(self.db_path)
        try:
            ids = [str(row[0]) for row in con.execute(
                "SELECT run_id FROM analysis_runs ORDER BY created_at DESC,run_id DESC LIMIT ?",
                (limit,),
            )]
        finally:
            con.close()
        return [self.get_run(run_id, include_detail=False) for run_id in ids]

    def get_run(self, run_id: str, *, include_detail: bool = True) -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            run = con.execute("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise AnalysisReadError("analysis_run_not_found", run_id)
            binding = _json(run, "binding_json", "analysis_run_json_invalid")
            request = _json(run, "request_manifest_json", "analysis_run_json_invalid")
            response = _json(run, "response_manifest_json", "analysis_run_json_invalid")
            if checksum(request) != str(run["request_checksum"]) or checksum(response) != str(run["response_checksum"]):
                raise AnalysisReadError("analysis_run_manifest_drift", run_id)

            candidate = con.execute("SELECT * FROM analysis_candidates WHERE run_id=?", (run_id,)).fetchone()
            receipt = con.execute("SELECT * FROM analysis_provider_receipts WHERE run_id=?", (run_id,)).fetchone()
            events = con.execute("SELECT * FROM analysis_events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
            if candidate is None or receipt is None or len(events) != 1:
                raise AnalysisReadError("analysis_child_count_drift", run_id)
            candidate_payload = _json(candidate, "payload_json", "analysis_child_json_invalid")
            receipt_payload = _json(receipt, "payload_json", "analysis_child_json_invalid")
            event_payload = _json(events[0], "payload_json", "analysis_child_json_invalid")
            candidate_checksum = checksum(candidate_payload)
            receipt_checksum = checksum(receipt_payload)
            if candidate_checksum != str(candidate["payload_checksum"]) or stable_id("dac", candidate_payload) != str(candidate["candidate_id"]):
                raise AnalysisReadError("analysis_candidate_drift", run_id)
            if receipt_checksum != str(receipt["payload_checksum"]):
                raise AnalysisReadError("analysis_receipt_drift", run_id)

            claims: list[dict[str, Any]] = []
            claim_checksums: list[str] = []
            for claim in con.execute(
                "SELECT * FROM analysis_claims WHERE candidate_id=? ORDER BY claim_ordinal",
                (candidate["candidate_id"],),
            ):
                refs: list[dict[str, Any]] = []
                evidence_payloads: list[dict[str, Any]] = []
                for ordinal, ref in enumerate(con.execute(
                    "SELECT * FROM analysis_evidence_refs WHERE claim_id=? ORDER BY evidence_ordinal",
                    (claim["claim_id"],),
                )):
                    payload = _json(ref, "payload_json", "analysis_evidence_json_invalid")
                    if int(ref["evidence_ordinal"]) != ordinal or checksum(payload) != str(ref["payload_checksum"]):
                        raise AnalysisReadError("analysis_evidence_drift", str(claim["claim_id"]))
                    evidence_payloads.append(payload)
                    refs.append({key: ref[key] for key in (
                        "evidence_ref_id", "authority_id", "record_type", "record_id",
                        "record_checksum", "snapshot_id", "snapshot_hash", "payload_checksum",
                    )})
                claim_core = {
                    "claim_id": str(claim["claim_id"]), "claim_type": str(claim["claim_type"]),
                    "statement": str(claim["statement"]), "evidence": evidence_payloads,
                }
                if checksum(claim_core) != str(claim["claim_checksum"]):
                    raise AnalysisReadError("analysis_claim_drift", str(claim["claim_id"]))
                claim_checksums.append(str(claim["claim_checksum"]))
                claims.append({
                    "claim_id": str(claim["claim_id"]), "ordinal": int(claim["claim_ordinal"]),
                    "claim_type": str(claim["claim_type"]), "statement": str(claim["statement"]),
                    "claim_checksum": str(claim["claim_checksum"]), "evidence": refs,
                })
            declared = candidate_payload.get("claims") if isinstance(candidate_payload, dict) else None
            expected = [{"claim_id": item["claim_id"], "claim_checksum": item["claim_checksum"]} for item in claims]
            if declared != expected:
                raise AnalysisReadError("analysis_candidate_claim_drift", run_id)

            run_core = {
                "schema_version": SCHEMA_VERSION, "registry_id": str(run["registry_id"]),
                "binding": binding, "binding_hash": str(run["binding_hash"]),
                "policy_version": str(run["policy_version"]), "policy_checksum": str(run["policy_checksum"]),
                "request_checksum": str(run["request_checksum"]), "response_checksum": str(run["response_checksum"]),
                "candidate_id": str(candidate["candidate_id"]), "candidate_checksum": candidate_checksum,
                "claim_checksums": claim_checksums, "receipt_checksum": receipt_checksum,
            }
            if checksum(run_core) != str(run["run_checksum"]) or stable_id("dar", run_core) != run_id:
                raise AnalysisReadError("analysis_run_checksum_drift", run_id)
            event_core = {"sequence": 1, "event_type": str(events[0]["event_type"]), "previous_event_checksum": "GENESIS", "payload": event_payload}
            if int(events[0]["sequence"]) != 1 or str(events[0]["previous_event_checksum"]) != "GENESIS" or checksum(event_core) != str(events[0]["payload_checksum"]):
                raise AnalysisReadError("analysis_event_drift", run_id)

            result: dict[str, Any] = {
                "run_id": run_id, "status": str(run["status"]), "created_at": str(run["created_at"]),
                "run_checksum": str(run["run_checksum"]), "binding_hash": str(run["binding_hash"]),
                "policy_version": str(run["policy_version"]), "policy_checksum": str(run["policy_checksum"]),
                "request_checksum": str(run["request_checksum"]), "response_checksum": str(run["response_checksum"]),
                "candidate_id": str(candidate["candidate_id"]), "candidate_status": str(candidate["candidate_status"]),
                "candidate_checksum": candidate_checksum, "claim_count": len(claims),
                "provider_receipt": {
                    "receipt_id": str(receipt["receipt_id"]), "payload_checksum": receipt_checksum,
                    "created_at": str(receipt["created_at"]),
                    "provider": receipt_payload.get("provider") if isinstance(receipt_payload, dict) else None,
                    "model": receipt_payload.get("model") if isinstance(receipt_payload, dict) else None,
                    "usage": receipt_payload.get("usage") if isinstance(receipt_payload, dict) else None,
                    "latency_ms": receipt_payload.get("latency_ms") if isinstance(receipt_payload, dict) else None,
                },
            }
            if include_detail:
                result.update({"binding": binding, "candidate": candidate_payload, "claims": claims})
            return result
        finally:
            con.close()

    def explain(self, run_id: str) -> dict[str, Any]:
        return {
            "run": self.get_run(run_id),
            "authoritative_fact": False,
            "authoritative_decision": False,
            "provider_body_included": False,
            "limitations": ["LLM output is a recommendation candidate", "evidence references are metadata-only"],
            "next_actions": ["inspect referenced Personal/External evidence", "review the linked pilot case if one exists"],
        }


__all__ = ["AnalysisReadError", "AnalysisReadService"]
