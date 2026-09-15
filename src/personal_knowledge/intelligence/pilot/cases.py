"""Deterministic bridge from an admitted analysis option to a pilot case."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum, stable_id
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding,
    DecisionContextBindingError,
    validate_decision_context_binding,
)

from .schema import (
    REGISTRY_ID, SCHEMA_VERSION, PilotSchemaError, ProjectCase,
    RecommendationCandidate, inspect_schema,
)


class PilotAdmissionError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class AdmissionResult:
    status: str
    reason_codes: tuple[str, ...]
    case: ProjectCase | None = None
    recommendation: RecommendationCandidate | None = None
    written: bool = False
    existing: bool = False
    authority_fingerprints_before: Mapping[str, str] | None = None
    authority_fingerprints_after: Mapping[str, str] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fingerprint(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ro(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path)
    if not resolved.is_file():
        raise PilotAdmissionError("analysis_authority_missing")
    con = sqlite3.connect(f"file:{resolved.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _json(row: sqlite3.Row, name: str) -> dict[str, Any]:
    try:
        value = json.loads(str(row[name]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise PilotAdmissionError("analysis_payload_invalid", name) from exc
    if not isinstance(value, dict):
        raise PilotAdmissionError("analysis_payload_invalid", name)
    return value


def _validate_analysis_candidate(
    analysis_db_path: Path | str,
    *,
    run_id: str,
    candidate_id: str,
    selected_option_id: str,
) -> dict[str, Any]:
    con = _ro(analysis_db_path)
    try:
        run = con.execute("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
        candidate = con.execute(
            "SELECT * FROM analysis_candidates WHERE candidate_id=? AND run_id=?",
            (candidate_id, run_id),
        ).fetchone()
        if run is None or candidate is None:
            raise PilotAdmissionError("analysis_candidate_missing")
        if str(run["registry_id"]) != "a.decision_analysis" or str(run["status"]) != "committed":
            raise PilotAdmissionError("analysis_run_not_admitted")
        if str(candidate["domain"]) != "project" or str(candidate["candidate_status"]) != "candidate":
            raise PilotAdmissionError("analysis_candidate_not_admitted")
        event = con.execute(
            "SELECT * FROM analysis_events WHERE run_id=? AND sequence=1 AND event_type='candidate_published'",
            (run_id,),
        ).fetchone()
        if event is None:
            raise PilotAdmissionError("analysis_admission_event_missing")

        binding = _json(run, "binding_json")
        request = _json(run, "request_manifest_json")
        response = _json(run, "response_manifest_json")
        candidate_payload = _json(candidate, "payload_json")
        event_payload = _json(event, "payload_json")
        if checksum(request) != str(run["request_checksum"]):
            raise PilotAdmissionError("analysis_request_checksum_mismatch")
        if checksum(response) != str(run["response_checksum"]):
            raise PilotAdmissionError("analysis_response_checksum_mismatch")
        if checksum(candidate_payload) != str(candidate["payload_checksum"]):
            raise PilotAdmissionError("analysis_candidate_checksum_mismatch")
        expected_event_checksum = checksum({
            "sequence": 1, "event_type": "candidate_published",
            "previous_event_checksum": "GENESIS", "payload": event_payload,
        })
        if (stable_id("dac", candidate_payload) != candidate_id
                or expected_event_checksum != str(event["payload_checksum"])
                or stable_id("dae", expected_event_checksum) != str(event["event_id"])
                or str(event["previous_event_checksum"]) != "GENESIS"):
            raise PilotAdmissionError("analysis_admission_event_checksum_mismatch")
        try:
            typed_binding = DecisionContextBinding.from_dict(binding)
        except Exception as exc:
            raise PilotAdmissionError("analysis_binding_invalid") from exc
        if (checksum(typed_binding.core()) != typed_binding.binding_hash
                or typed_binding.binding_hash != str(run["binding_hash"])
                or typed_binding.binding_hash != str(candidate["binding_hash"])
                or candidate_payload.get("binding_hash") != typed_binding.binding_hash
                or request.get("binding_hash") != typed_binding.binding_hash):
            raise PilotAdmissionError("analysis_binding_checksum_mismatch")

        draft = candidate_payload.get("draft")
        if not isinstance(draft, dict) or draft.get("domain") != "project" or draft.get("status") != "candidate":
            raise PilotAdmissionError("analysis_candidate_payload_invalid")
        if draft != {key: response.get(key) for key in draft}:
            raise PilotAdmissionError("analysis_response_candidate_mismatch")
        options = draft.get("options")
        if not isinstance(options, list):
            raise PilotAdmissionError("analysis_options_invalid")
        option = next(
            (item for item in options if isinstance(item, dict) and item.get("option_id") == selected_option_id),
            None,
        )
        if option is None:
            raise PilotAdmissionError("analysis_option_missing", selected_option_id)

        claim_links = candidate_payload.get("claims")
        if not isinstance(claim_links, list):
            raise PilotAdmissionError("analysis_claim_links_invalid")
        claim_rows = con.execute(
            "SELECT * FROM analysis_claims WHERE candidate_id=? ORDER BY claim_ordinal",
            (candidate_id,),
        ).fetchall()
        if len(claim_links) != len(claim_rows):
            raise PilotAdmissionError("analysis_claim_count_mismatch")
        verified_claim_checksums: list[str] = []
        for ordinal, (link, claim) in enumerate(zip(claim_links, claim_rows, strict=True)):
            if (claim["claim_ordinal"] != ordinal or not isinstance(link, dict)
                    or link.get("claim_id") != claim["claim_id"]
                    or link.get("claim_checksum") != claim["claim_checksum"]):
                raise PilotAdmissionError("analysis_claim_lineage_mismatch")
            refs = con.execute(
                "SELECT * FROM analysis_evidence_refs WHERE claim_id=? ORDER BY evidence_ordinal",
                (claim["claim_id"],),
            ).fetchall()
            if str(claim["claim_type"]) == "factual" and not refs:
                raise PilotAdmissionError("analysis_factual_evidence_missing")
            for ref_ordinal, ref in enumerate(refs):
                payload = _json(ref, "payload_json")
                if ref["evidence_ordinal"] != ref_ordinal or checksum(payload) != str(ref["payload_checksum"]):
                    raise PilotAdmissionError("analysis_evidence_checksum_mismatch")
                for key in (
                    "authority_id", "record_type", "record_id", "record_checksum",
                    "snapshot_id", "snapshot_hash",
                ):
                    if payload.get(key) != ref[key]:
                        raise PilotAdmissionError("analysis_evidence_lineage_mismatch")
            claim_core = {
                "claim_id": str(claim["claim_id"]), "claim_type": str(claim["claim_type"]),
                "statement": str(claim["statement"]),
                "evidence": [_json(ref, "payload_json") for ref in refs],
            }
            if checksum(claim_core) != str(claim["claim_checksum"]):
                raise PilotAdmissionError("analysis_claim_checksum_mismatch")
            verified_claim_checksums.append(str(claim["claim_checksum"]))

        receipt = con.execute("SELECT * FROM analysis_provider_receipts WHERE run_id=?", (run_id,)).fetchone()
        if receipt is None:
            raise PilotAdmissionError("analysis_receipt_missing")
        receipt_payload = _json(receipt, "payload_json")
        receipt_checksum = checksum(receipt_payload)
        if receipt_checksum != str(receipt["payload_checksum"]):
            raise PilotAdmissionError("analysis_receipt_checksum_mismatch")
        run_core = {
            "schema_version": "decision_analysis_candidate_v1", "registry_id": str(run["registry_id"]),
            "binding": binding, "binding_hash": str(run["binding_hash"]),
            "policy_version": str(run["policy_version"]), "policy_checksum": str(run["policy_checksum"]),
            "request_checksum": str(run["request_checksum"]), "response_checksum": str(run["response_checksum"]),
            "candidate_id": candidate_id, "candidate_checksum": str(candidate["payload_checksum"]),
            "claim_checksums": verified_claim_checksums, "receipt_checksum": receipt_checksum,
        }
        if checksum(run_core) != str(run["run_checksum"]) or stable_id("dar", run_core) != run_id:
            raise PilotAdmissionError("analysis_run_checksum_mismatch")

        if request.get("domain") != "project" or request.get("risk_budget") != "low":
            raise PilotAdmissionError("analysis_request_policy_invalid")
        confirmation = request.get("confirmation")
        if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True or confirmation.get("actor") != "user":
            raise PilotAdmissionError("analysis_user_confirmation_missing")
        return {
            "run": dict(run), "candidate": dict(candidate), "binding": binding,
            "request": request, "response": response, "draft": draft, "option": option,
        }
    except sqlite3.Error as exc:
        raise PilotAdmissionError("analysis_authority_invalid", str(exc)) from exc
    finally:
        con.close()


def _materialize(
    source: Mapping[str, Any], *, case_confirmation_event_id: str,
) -> tuple[ProjectCase, RecommendationCandidate, dict[str, Any], dict[str, Any], dict[str, Any]]:
    run, candidate = source["run"], source["candidate"]
    binding, request, draft, option = source["binding"], source["request"], source["draft"], source["option"]
    case_payload = {
        "schema_version": SCHEMA_VERSION, "registry_id": REGISTRY_ID,
        "source": {
            "run_id": run["run_id"], "candidate_id": candidate["candidate_id"],
            "run_checksum": run["run_checksum"], "candidate_checksum": candidate["payload_checksum"],
            "binding_hash": run["binding_hash"], "request_checksum": run["request_checksum"],
            "response_checksum": run["response_checksum"],
        },
        "snapshots": {
            "personal": {"snapshot_id": binding["personal_snapshot_id"], "snapshot_hash": binding["personal_snapshot_hash"]},
            "external": {"snapshot_id": binding["external_snapshot_id"], "snapshot_hash": binding["external_snapshot_hash"]},
        },
        "confirmed_input": {
            "goal": request["goal"], "constraints": request["constraints"],
            "weights": request["weights"], "risk_budget": request["risk_budget"],
            "source_confirmation": request["confirmation"],
            "case_confirmation_event_id": case_confirmation_event_id,
        },
        "no_action_baseline": draft["no_action_baseline"],
        "alternatives": draft["options"], "stop_conditions": draft["stop_conditions"],
    }
    case_checksum = checksum(case_payload)
    case_id = stable_id("ppc", case_payload)
    case = ProjectCase(
        case_id=case_id, source_run_id=str(run["run_id"]), source_candidate_id=str(candidate["candidate_id"]),
        source_run_checksum=str(run["run_checksum"]), source_candidate_checksum=str(candidate["payload_checksum"]),
        binding_hash=str(run["binding_hash"]), personal_snapshot_id=str(binding["personal_snapshot_id"]),
        personal_snapshot_hash=str(binding["personal_snapshot_hash"]), external_snapshot_id=str(binding["external_snapshot_id"]),
        external_snapshot_hash=str(binding["external_snapshot_hash"]), request_checksum=str(run["request_checksum"]),
        response_checksum=str(run["response_checksum"]), goal=str(request["goal"]),
        constraints=tuple(str(item) for item in request["constraints"]),
        weights={str(key): float(value) for key, value in request["weights"].items()}, risk_budget="low",
        no_action_baseline=dict(draft["no_action_baseline"]), alternatives=tuple(dict(item) for item in draft["options"]),
        stop_conditions=tuple(str(item) for item in draft["stop_conditions"]),
        confirmation_event_id=case_confirmation_event_id, payload_checksum=case_checksum,
    )
    recommendation_payload = {
        "schema_version": SCHEMA_VERSION, "case_id": case_id,
        "status": "candidate", "option": option,
        "non_authoritative": True, "requires_user_decision": True,
        "actions_executed": 0, "source_candidate_checksum": candidate["payload_checksum"],
    }
    recommendation_checksum = checksum(recommendation_payload)
    recommendation = RecommendationCandidate(
        recommendation_id=stable_id("ppr", recommendation_payload), case_id=case_id,
        option_id=str(option["option_id"]), option=dict(option), status="candidate",
        reason_codes=(), payload_checksum=recommendation_checksum,
    )
    protocol_payload = {
        "schema_version": SCHEMA_VERSION, "case_id": case_id, "status": "pending",
        "requires_preregistration_before_action": True, "external_actions_allowed": False,
    }
    return case, recommendation, case_payload, recommendation_payload, protocol_payload


def _publish(
    pilot_db_path: Path | str, case: ProjectCase, recommendation: RecommendationCandidate,
    case_payload: Mapping[str, Any], recommendation_payload: Mapping[str, Any],
    protocol_payload: Mapping[str, Any], *, occurred_at: str, fault_at: str | None,
) -> tuple[bool, bool]:
    if inspect_schema(pilot_db_path).get("schema_state") != "applied":
        raise PilotAdmissionError("pilot_schema_not_applied")
    con = connect_rw(Path(pilot_db_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        existing = con.execute("SELECT payload_checksum FROM pilot_cases WHERE case_id=?", (case.case_id,)).fetchone()
        if existing is not None:
            if str(existing["payload_checksum"]) != case.payload_checksum:
                raise PilotAdmissionError("existing_case_checksum_mismatch")
            child = con.execute(
                "SELECT payload_checksum FROM pilot_recommendations WHERE case_id=?", (case.case_id,),
            ).fetchone()
            if child is None or str(child["payload_checksum"]) != recommendation.payload_checksum:
                raise PilotAdmissionError("existing_recommendation_checksum_mismatch")
            protocol = con.execute("SELECT payload_json,payload_checksum FROM pilot_protocols WHERE case_id=?", (case.case_id,)).fetchone()
            event = con.execute("SELECT * FROM pilot_events WHERE case_id=? AND sequence=1", (case.case_id,)).fetchone()
            if protocol is None or event is None:
                raise PilotAdmissionError("existing_case_children_missing")
            if (checksum(json.loads(str(protocol["payload_json"]))) != str(protocol["payload_checksum"])
                    or checksum(json.loads(str(event["payload_json"]))) != str(event["payload_checksum"])
                    or str(event["previous_event_checksum"]) != "GENESIS"):
                raise PilotAdmissionError("existing_case_child_checksum_mismatch")
            return False, True
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO pilot_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case.case_id, REGISTRY_ID, case.source_run_id, case.source_candidate_id,
             case.source_run_checksum, case.source_candidate_checksum, case.binding_hash,
             case.personal_snapshot_id, case.personal_snapshot_hash, case.external_snapshot_id,
             case.external_snapshot_hash, case.request_checksum, case.response_checksum,
             case.confirmation_event_id, canonical_json(case_payload), case.payload_checksum, occurred_at),
        )
        if fault_at == "after_case":
            raise RuntimeError("injected pilot failure after_case")
        con.execute(
            "INSERT INTO pilot_recommendations VALUES (?,?,?,?,?,?,?)",
            (recommendation.recommendation_id, case.case_id, recommendation.option_id,
             recommendation.status, canonical_json(recommendation_payload),
             recommendation.payload_checksum, occurred_at),
        )
        protocol_checksum = checksum(protocol_payload)
        con.execute(
            "INSERT INTO pilot_protocols VALUES (?,?,?,?,?,?)",
            (stable_id("ppp", protocol_payload), case.case_id, "pending",
             canonical_json(protocol_payload), protocol_checksum, occurred_at),
        )
        event_payload = {
            "schema_version": SCHEMA_VERSION, "case_id": case.case_id,
            "event_type": "case_frozen", "case_checksum": case.payload_checksum,
            "recommendation_checksum": recommendation.payload_checksum,
            "protocol_checksum": protocol_checksum, "actions_executed": 0,
        }
        con.execute(
            "INSERT INTO pilot_events VALUES (?,?,?,?,?,?,?,?)",
            (stable_id("ppe", event_payload), case.case_id, 1, "case_frozen", "GENESIS",
             canonical_json(event_payload), checksum(event_payload), occurred_at),
        )
        if fault_at == "after_event":
            raise RuntimeError("injected pilot failure after_event")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise PilotAdmissionError("pilot_foreign_key_failure")
        con.commit()
        return True, False
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def admit_project_case(
    *,
    pilot_db_path: Path | str,
    analysis_db_path: Path | str,
    personal_db_path: Path | str,
    external_db_path: Path | str,
    run_id: str,
    candidate_id: str,
    selected_option_id: str,
    case_confirmation_event_id: str,
    write: bool = False,
    now: str | None = None,
    fault_at: str | None = None,
) -> AdmissionResult:
    """Freeze one exact admitted option; all failures abstain without source writes."""
    source_paths = {
        "personal": personal_db_path, "external": external_db_path, "analysis": analysis_db_path,
    }
    try:
        before = {name: _fingerprint(path) for name, path in source_paths.items()}
        source = _validate_analysis_candidate(
            analysis_db_path, run_id=run_id, candidate_id=candidate_id,
            selected_option_id=selected_option_id,
        )
        validate_decision_context_binding(
            source["binding"], personal_db_path, external_db_path, now=now,
        )
        if not case_confirmation_event_id.strip() or len(case_confirmation_event_id) > 256:
            raise PilotAdmissionError("case_confirmation_invalid")
        case, recommendation, case_payload, recommendation_payload, protocol_payload = _materialize(
            source, case_confirmation_event_id=case_confirmation_event_id,
        )
        if write:
            written, existing = _publish(
                pilot_db_path, case, recommendation, case_payload, recommendation_payload,
                protocol_payload, occurred_at=now or _now(), fault_at=fault_at,
            )
        else:
            written, existing = False, False
        after = {name: _fingerprint(path) for name, path in source_paths.items()}
        if before != after:
            raise PilotAdmissionError("source_authority_drift")
        return AdmissionResult(
            status="candidate", reason_codes=(), case=case, recommendation=recommendation,
            written=written, existing=existing, authority_fingerprints_before=before,
            authority_fingerprints_after=after,
        )
    except (PilotAdmissionError, PilotSchemaError, DecisionContextBindingError, sqlite3.Error, OSError) as exc:
        code = str(getattr(exc, "code", "pilot_admission_failed"))
        after = None
        try:
            after = {name: _fingerprint(path) for name, path in source_paths.items()}
        except OSError:
            pass
        return AdmissionResult(
            status="abstain", reason_codes=(code,),
            authority_fingerprints_before=locals().get("before"),
            authority_fingerprints_after=after,
        )


__all__ = ["AdmissionResult", "PilotAdmissionError", "admit_project_case"]
