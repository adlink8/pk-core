"""Preregister and freeze a reproducible paired-comparison protocol."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum, stable_id
from personal_knowledge.intelligence.pilot.service import get_case, history

from .schema import REGISTRY_ID, SCHEMA_VERSION, inspect_schema


class CalibrationProtocolError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code; self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


REQUIRED_METRICS = ("acceptance", "execution", "completion", "time_deviation", "cost_deviation",
                    "quality", "satisfaction", "side_effects", "regret", "abstention")


def _utc(value: str) -> datetime:
    if not value.endswith("Z"): raise CalibrationProtocolError("timestamp_invalid")
    try: return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc: raise CalibrationProtocolError("timestamp_invalid") from exc


@dataclass(frozen=True)
class FrozenProtocol:
    protocol_id: str
    payload: Mapping[str, Any]
    payload_checksum: str


def build_protocol(
    *, question: str, domain: str, external_snapshot_id: str, external_snapshot_hash: str,
    provider: str, model: str, prompt_version: str, schema_version: str,
    temperature: float, max_output_tokens: int, max_total_tokens: int,
    cohort: Sequence[Mapping[str, str]], exclusions: Sequence[str],
    window_start: str, window_end: str, thresholds: Mapping[str, float],
    minimum_evidence: int, frozen_at: str,
) -> FrozenProtocol:
    if domain != "project" or not question.strip() or not cohort or not exclusions:
        raise CalibrationProtocolError("protocol_scope_invalid")
    if len(external_snapshot_hash) != 64 or _utc(window_end) < _utc(window_start) or _utc(frozen_at) > _utc(window_start):
        raise CalibrationProtocolError("protocol_chronology_invalid")
    if minimum_evidence < 2 or set(thresholds) != set(REQUIRED_METRICS):
        raise CalibrationProtocolError("protocol_thresholds_invalid")
    if temperature != 0 or max_output_tokens <= 0 or max_total_tokens < max_output_tokens:
        raise CalibrationProtocolError("protocol_budget_invalid")
    payload = {
        "schema_version": SCHEMA_VERSION, "registry_id": REGISTRY_ID,
        "question": question.strip(), "domain": domain,
        "common_external_snapshot": {"snapshot_id": external_snapshot_id, "snapshot_hash": external_snapshot_hash},
        "common_generation": {"provider": provider, "model": model, "prompt_version": prompt_version,
                              "schema_version": schema_version, "temperature": temperature,
                              "max_output_tokens": max_output_tokens, "max_total_tokens": max_total_tokens},
        "only_arm_difference": "personal_snapshot_and_history_access",
        "generic_forbidden": ["personal_snapshot", "personal_history", "derived_personal_feature", "identifying_metadata"],
        "cohort": [dict(item) for item in cohort], "exclusions": list(exclusions),
        "observation_window": {"start": window_start, "end": window_end},
        "metrics": list(REQUIRED_METRICS), "thresholds": dict(sorted(thresholds.items())),
        "minimum_evidence": minimum_evidence,
        "inconclusive_rules": ["sample_below_minimum", "missing_window", "protocol_deviation", "confounded_or_ambiguous"],
        "causal_claim": False, "frozen_at": frozen_at,
    }
    digest = checksum(payload)
    return FrozenProtocol(stable_id("calp", payload), payload, digest)


def freeze_protocol(
    db_path: Path | str, pilot_db_path: Path | str, protocol: FrozenProtocol,
    *, write: bool = False, fault_at: str | None = None,
) -> dict[str, Any]:
    if inspect_schema(db_path).get("schema_state") != "applied": raise CalibrationProtocolError("schema_not_applied")
    members = []
    seen_case_ids: set[str] = set()
    seen_source_candidates: set[str] = set()
    for ordinal, item in enumerate(protocol.payload["cohort"]):
        case_id = str(item["case_id"])
        if case_id in seen_case_ids:
            raise CalibrationProtocolError("cohort_case_duplicate", case_id)
        seen_case_ids.add(case_id)
        detail = get_case(pilot_db_path, item["case_id"])
        if detail["case"]["payload_checksum"] != item["case_checksum"]:
            raise CalibrationProtocolError("cohort_case_checksum_mismatch")
        source = detail["case"]["payload"].get("source", {})
        source_candidate_id = str(source.get("candidate_id") or "")
        if not source_candidate_id:
            raise CalibrationProtocolError("cohort_source_missing", case_id)
        if source_candidate_id in seen_source_candidates:
            raise CalibrationProtocolError("cohort_source_duplicate", source_candidate_id)
        seen_source_candidates.add(source_candidate_id)
        events = history(pilot_db_path, item["case_id"])
        outcome = next((event for event in events if event["event_type"] == "outcome_observed"), None)
        if outcome is None or outcome["payload_checksum"] != item["outcome_event_checksum"]:
            raise CalibrationProtocolError("cohort_outcome_checksum_mismatch")
        member_payload = {"protocol_id": protocol.protocol_id, "ordinal": ordinal, **dict(item)}
        members.append((stable_id("calm", member_payload), member_payload, checksum(member_payload)))
    if not write: return {"dry_run": True, "protocol_id": protocol.protocol_id, "members": len(members)}
    con = connect_rw(Path(db_path), timeout=30); con.row_factory = sqlite3.Row
    try:
        existing = con.execute("SELECT payload_checksum FROM calibration_protocols WHERE protocol_id=?", (protocol.protocol_id,)).fetchone()
        if existing:
            if existing["payload_checksum"] != protocol.payload_checksum: raise CalibrationProtocolError("protocol_replay_mismatch")
            return {"written": False, "existing": True, "protocol_id": protocol.protocol_id}
        if con.execute("SELECT COUNT(*) FROM calibration_arms").fetchone()[0]: raise CalibrationProtocolError("late_protocol_registration")
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO calibration_protocols VALUES (?,?,?,?,?,?)",
                    (protocol.protocol_id, REGISTRY_ID, "frozen", canonical_json(protocol.payload), protocol.payload_checksum, protocol.payload["frozen_at"]))
        if fault_at == "after_protocol": raise RuntimeError("injected calibration failure")
        for member_id, payload, digest in members:
            con.execute("INSERT INTO calibration_cohort_members VALUES (?,?,?,?,?,?,?,?,?)",
                        (member_id, protocol.protocol_id, payload["ordinal"], payload["case_id"], payload["case_checksum"],
                         payload["outcome_event_checksum"], canonical_json(payload), digest, protocol.payload["frozen_at"]))
        event = {"protocol_id": protocol.protocol_id, "event_type": "protocol_frozen", "protocol_checksum": protocol.payload_checksum,
                 "member_checksums": [item[2] for item in members]}
        con.execute("INSERT INTO calibration_events VALUES (?,?,?,?,?,?,?,?)",
                    (stable_id("cale", event), protocol.protocol_id, 1, "protocol_frozen", "GENESIS", canonical_json(event), checksum(event), protocol.payload["frozen_at"]))
        con.commit(); return {"written": True, "existing": False, "protocol_id": protocol.protocol_id}
    except Exception: con.rollback(); raise
    finally: con.close()


__all__ = ["CalibrationProtocolError", "FrozenProtocol", "REQUIRED_METRICS", "build_protocol", "freeze_protocol"]
