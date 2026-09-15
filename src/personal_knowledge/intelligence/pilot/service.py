"""Checksum-verifying read service and metadata-only pilot acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.intelligence.analysis.schema import checksum
from personal_knowledge.intelligence.decision.context_binding import validate_decision_context_binding

from .cases import _validate_analysis_candidate
from .outcomes import assess_outcome
from .schema import inspect_schema
from .workflow import PilotWorkflowError, read_event_stream


class PilotServiceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _ro(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path)
    con = sqlite3.connect(f"file:{resolved.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise PilotServiceError("pilot_payload_invalid") from exc
    if checksum(value) != str(row["payload_checksum"]):
        raise PilotServiceError("pilot_payload_checksum_mismatch")
    return value


def get_case(db_path: Path | str, case_id: str) -> dict[str, Any]:
    con = _ro(db_path)
    try:
        case = con.execute("SELECT * FROM pilot_cases WHERE case_id=?", (case_id,)).fetchone()
        recommendation = con.execute(
            "SELECT * FROM pilot_recommendations WHERE case_id=?", (case_id,),
        ).fetchone()
        protocol = con.execute("SELECT * FROM pilot_protocols WHERE case_id=?", (case_id,)).fetchone()
        if case is None or recommendation is None or protocol is None:
            raise PilotServiceError("pilot_case_incomplete", case_id)
        return {"case": {**dict(case), "payload": _payload(case)},
                "recommendation": {**dict(recommendation), "payload": _payload(recommendation)},
                "protocol": {**dict(protocol), "payload": _payload(protocol)}}
    finally:
        con.close()


def list_cases(db_path: Path | str) -> tuple[dict[str, Any], ...]:
    con = _ro(db_path)
    try:
        ids = [str(row[0]) for row in con.execute("SELECT case_id FROM pilot_cases ORDER BY created_at,case_id")]
    finally:
        con.close()
    return tuple(get_case(db_path, case_id)["case"] for case_id in ids)


def history(db_path: Path | str, case_id: str) -> tuple[dict[str, Any], ...]:
    return read_event_stream(db_path, case_id)


def controls(db_path: Path | str, case_id: str) -> dict[str, Any]:
    stream = read_event_stream(db_path, case_id)
    revoked: set[str] = set()
    restored: set[str] = set()
    corrections: list[dict[str, Any]] = []
    snapshot_state = "BOUND"
    for event in stream:
        body = event["payload"].get("body", {})
        if event["event_type"] == "revoke":
            revoked.add(str(body["target_checksum"]))
        elif event["event_type"] == "restore":
            restored.add(str(body["restored_target_checksum"]))
        elif event["event_type"] == "correction":
            corrections.append(body)
        elif event["event_type"] == "snapshot_rollback":
            snapshot_state = "UNBOUND"
        elif event["event_type"] == "snapshot_forward_restore":
            snapshot_state = "BOUND"
    return {"case_id": case_id, "revoked_target_checksums": sorted(revoked - restored),
            "restored_target_checksums": sorted(restored), "corrections": corrections,
            "snapshot_state": snapshot_state}


def explain(db_path: Path | str, case_id: str, *, as_of: str | None = None) -> dict[str, Any]:
    case = get_case(db_path, case_id)
    events = history(db_path, case_id)
    has_protocol = any(item["event_type"] == "outcome_preregistered" for item in events)
    outcome = None
    if has_protocol and as_of is not None:
        outcome = assess_outcome(db_path, case_id, as_of=as_of).__dict__
    return {**case, "history": events, "controls": controls(db_path, case_id),
            "outcome": outcome, "authoritative_decision": False,
            "system_external_actions": sum(int(item["payload"].get("system_external_actions", 0)) for item in events)}


def _fingerprint(path: Path | str) -> str:
    root = Path(path)
    digest = hashlib.sha256()
    if root.is_file():
        with root.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    elif root.is_dir():
        for item in sorted((path for path in root.rglob("*") if path.is_file()), key=lambda value: value.as_posix()):
            digest.update(item.relative_to(root).as_posix().encode("utf-8"))
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    else:
        raise PilotServiceError("authority_path_missing", str(root))
    return digest.hexdigest()


def acceptance_report(
    *, pilot_db_path: Path | str, knowledge_authority_path: Path | str,
    personal_db_path: Path | str, external_db_path: Path | str,
    analysis_db_path: Path | str, as_of: str,
) -> dict[str, Any]:
    paths = {"knowledge": knowledge_authority_path, "personal": personal_db_path,
             "external": external_db_path, "analysis": analysis_db_path, "pilot": pilot_db_path}
    before = {name: _fingerprint(path) for name, path in paths.items()}
    schema = inspect_schema(pilot_db_path)
    if schema.get("schema_state") != "applied":
        raise PilotServiceError("pilot_schema_invalid")
    cases = list_cases(pilot_db_path)
    if not cases:
        raise PilotServiceError("pilot_case_missing")
    reports: list[dict[str, Any]] = []
    action_count = 0
    for summary in cases:
        case_id = str(summary["case_id"])
        detail = get_case(pilot_db_path, case_id)
        option_id = str(detail["recommendation"]["option_id"])
        source = detail["case"]["payload"]["source"]
        admitted = _validate_analysis_candidate(
            analysis_db_path, run_id=str(source["run_id"]),
            candidate_id=str(source["candidate_id"]), selected_option_id=option_id,
        )
        validate_decision_context_binding(
            admitted["binding"], personal_db_path, external_db_path, now=as_of,
        )
        view = explain(pilot_db_path, case_id, as_of=as_of)
        action_count += int(view["system_external_actions"])
        reports.append({"case_id": case_id, "events": len(view["history"]),
                        "snapshot_state": view["controls"]["snapshot_state"],
                        "outcome": view["outcome"]})
    after = {name: _fingerprint(path) for name, path in paths.items()}
    unchanged = before == after
    return {"ok": unchanged and action_count == 0, "metadata_only": True,
            "schema": schema, "cases": reports, "authority_fingerprints_before": before,
            "authority_fingerprints_after": after, "unchanged": unchanged,
            "provider_calls": 0, "network_calls": 0,
            "system_external_actions": action_count, "unauthorized_knowledge_writes": 0}


__all__ = [
    "PilotServiceError", "acceptance_report", "controls", "explain", "get_case",
    "history", "list_cases",
]
