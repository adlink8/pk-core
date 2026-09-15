"""Crash-aware at-most-once generation over the Analysis authority."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from personal_knowledge.intelligence.analysis.executor import execute_analysis
from personal_knowledge.intelligence.analysis.inputs import ConfirmationEvent, DEFAULT_POLICY_PATH
from personal_knowledge.intelligence.analysis.service import AnalysisReadService

from .models import OrchestrationError, OperationResult, Preview, canonical_json, checksum, event_id, stable_id
from .service import OrchestrationService, _event_core


GenerationRunner = Callable[[Mapping[str, Any], Mapping[str, Any], str, str], Mapping[str, Any]]


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invocation_rows(
    con: sqlite3.Connection, session_id: str, idempotency_key: str,
) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM orchestration_invocations WHERE session_id=? AND operation='generate' "
        "AND idempotency_key=? ORDER BY CASE stage WHEN 'reserved' THEN 0 ELSE 1 END",
        (session_id, idempotency_key),
    ).fetchall()


def reserve_generation(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, now: str,
) -> dict[str, Any]:
    item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
    Preview.from_dict(item.to_dict())
    if item.operation != "generate":
        raise OrchestrationError("generation_operation_invalid")
    claims, confirmation_digest = service._confirmation_claims(item, confirmation_token, now=now)
    key = service._validate_idempotency(idempotency_key)
    request_checksum = checksum(dict(item.payload))
    reservation_id = stable_id("ori", {
        "session_id": item.session_id, "operation": "generate",
        "idempotency_key": key, "request_checksum": request_checksum,
    })
    con = service._connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = _invocation_rows(con, item.session_id, key)
        if existing:
            if any(str(row["request_checksum"]) != request_checksum for row in existing):
                raise OrchestrationError("idempotency_conflict")
            terminal = next((row for row in existing if row["stage"] != "reserved"), None)
            if terminal is not None:
                con.rollback()
                result = json.loads(str(terminal["result_json"]))
                return {"new": False, "stage": str(terminal["stage"]), "result": result}
            con.rollback()
            raise OrchestrationError("provider_outcome_unknown")
        view = service._resume_with(con, item.session_id)
        if view["state"] != "confirmed":
            raise OrchestrationError("illegal_transition")
        if item.expected_sequence != view["sequence"]:
            raise OrchestrationError("stale_expected_sequence")
        if item.actor_identity_hash != view["manifest"]["actor_identity_hash"]:
            raise OrchestrationError("actor_identity_mismatch")
        if item.payload.get("binding_hash") != view["manifest"]["binding_hash"]:
            raise OrchestrationError("preview_binding_drift")
        service._binding_validator(view["binding"], service.personal_db, service.external_db, now=now)
        con.execute(
            "INSERT INTO orchestration_confirmations VALUES (?,?,?,?,?,?,?,?)",
            (confirmation_digest, item.session_id, "generate", item.preview_checksum,
             item.actor_identity_hash, item.expected_sequence, claims["expires_at"], now),
        )
        con.execute(
            "INSERT INTO orchestration_invocations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (reservation_id, reservation_id, item.session_id, "generate", key,
             request_checksum, "reserved", None, None, now),
        )
        con.commit()
        return {
            "new": True, "stage": "reserved", "reservation_id": reservation_id,
            "request_checksum": request_checksum, "confirmation_digest": confirmation_digest,
        }
    except sqlite3.IntegrityError as exc:
        con.rollback()
        if "confirmation" in str(exc).lower() or "unique" in str(exc).lower():
            raise OrchestrationError("confirmation_consumed") from exc
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def finalize_generation(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    idempotency_key: str, runner_result: Mapping[str, Any], now: str,
) -> OperationResult | dict[str, Any]:
    item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
    key = service._validate_idempotency(idempotency_key)
    request_checksum = checksum(dict(item.payload))
    con = service._connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = _invocation_rows(con, item.session_id, key)
        reservation = next((row for row in rows if row["stage"] == "reserved"), None)
        terminal = next((row for row in rows if row["stage"] != "reserved"), None)
        if reservation is None or str(reservation["request_checksum"]) != request_checksum:
            raise OrchestrationError("generation_reservation_missing")
        if terminal is not None:
            stored = json.loads(str(terminal["result_json"]))
            if checksum(stored) != terminal["result_checksum"]:
                raise OrchestrationError("generation_result_checksum_mismatch")
            if terminal["stage"] == "abstained":
                con.rollback()
                return stored
            event = con.execute(
                "SELECT * FROM orchestration_events WHERE session_id=? AND idempotency_key=?",
                (item.session_id, key),
            ).fetchone()
            con.rollback()
            if event is None:
                raise OrchestrationError("generation_event_missing")
            return service._result(event, replayed=True)
        status = str(runner_result.get("status") or "")
        if status not in {"success", "abstain"}:
            raise OrchestrationError("generation_runner_result_invalid")
        result = dict(runner_result)
        result_checksum = checksum(result)
        stage = "completed" if status == "success" else "abstained"
        terminal_id = stable_id("ori", {
            "reservation_id": reservation["reservation_id"], "stage": stage,
            "result_checksum": result_checksum,
        })
        con.execute(
            "INSERT INTO orchestration_invocations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (terminal_id, reservation["reservation_id"], item.session_id, "generate", key,
             request_checksum, stage, canonical_json(result), result_checksum, now),
        )
        if stage == "abstained":
            con.commit()
            return {**result, "replayed": False}
        view = service._resume_with(con, item.session_id)
        if view["state"] != "confirmed" or item.expected_sequence != view["sequence"]:
            raise OrchestrationError("generation_state_drift")
        references = dict(result.get("references") or {})
        required = {"run_id", "candidate_id", "run_checksum", "candidate_checksum"}
        if not required.issubset(references):
            raise OrchestrationError("generation_references_incomplete")
        confirmation_digest = _token_digest(str(runner_result.get("confirmation_token") or ""))
        confirmation = con.execute(
            "SELECT confirmation_digest FROM orchestration_confirmations "
            "WHERE session_id=? AND operation='generate' AND preview_checksum=?",
            (item.session_id, item.preview_checksum),
        ).fetchone()
        if confirmation is None:
            raise OrchestrationError("generation_confirmation_missing")
        confirmation_digest = str(confirmation["confirmation_digest"])
        payload = {
            "preview_checksum": item.preview_checksum, "request_checksum": request_checksum,
            "request": dict(item.payload), "effect": references,
        }
        payload_checksum = checksum(payload)
        sequence = view["sequence"] + 1
        core = _event_core(
            session_id=item.session_id, sequence=sequence, operation="generate",
            from_state="confirmed", to_state="generated",
            previous_event_checksum=view["last_event_checksum"], payload_checksum=payload_checksum,
            idempotency_key=key, actor_identity_hash=item.actor_identity_hash,
            confirmation_digest=confirmation_digest, occurred_at=now,
        )
        event_checksum = checksum(core)
        identifier = event_id(event_checksum)
        con.execute(
            "INSERT INTO orchestration_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, item.session_id, sequence, "generate", "confirmed", "generated",
             view["last_event_checksum"], canonical_json(payload), payload_checksum,
             event_checksum, key, item.actor_identity_hash, confirmation_digest, now),
        )
        con.commit()
        event = con.execute("SELECT * FROM orchestration_events WHERE event_id=?", (identifier,)).fetchone()
        return service._result(event, replayed=False)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def execute_confirmed_generation(
    service: OrchestrationService, preview: Preview | Mapping[str, Any], *,
    confirmation_token: str, idempotency_key: str, runner: GenerationRunner,
    now: str,
) -> OperationResult | dict[str, Any]:
    reservation = reserve_generation(
        service, preview, confirmation_token=confirmation_token,
        idempotency_key=idempotency_key, now=now,
    )
    if not reservation.get("new"):
        result = dict(reservation["result"])
        if reservation["stage"] == "abstained":
            return {**result, "replayed": True}
        con = service._connect(readonly=True)
        try:
            event = con.execute(
                "SELECT * FROM orchestration_events WHERE session_id=? AND idempotency_key=?",
                ((preview.session_id if isinstance(preview, Preview) else preview["session_id"]), idempotency_key),
            ).fetchone()
        finally:
            con.close()
        if event is None:
            raise OrchestrationError("generation_event_missing")
        return service._result(event, replayed=True)
    item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
    view = service.resume(item.session_id, now=now)
    try:
        result = runner(view["manifest"], dict(item.payload.get("input") or {}), reservation["reservation_id"], now)
    except Exception as exc:
        raise OrchestrationError("provider_outcome_unknown") from exc
    return finalize_generation(
        service, item, idempotency_key=idempotency_key,
        runner_result=result, now=now,
    )


class ExistingAnalysisAdapter:
    """Convert one confirmed session into the existing Analysis executor contract."""
    def __init__(
        self, *, provider: Any, personal_db: Path | str, external_db: Path | str,
        analysis_db: Path | str, policy_path: Path | str = DEFAULT_POLICY_PATH,
        max_output_tokens: int = 4096, max_total_tokens: int = 8192,
        timeout_seconds: float = 120,
    ) -> None:
        self.provider = provider
        self.personal_db = Path(personal_db)
        self.external_db = Path(external_db)
        self.analysis_db = Path(analysis_db)
        self.policy_path = Path(policy_path)
        self.max_output_tokens = max_output_tokens
        self.max_total_tokens = max_total_tokens
        self.timeout_seconds = timeout_seconds

    def __call__(
        self, manifest: Mapping[str, Any], input_payload: Mapping[str, Any],
        confirmation_event_id: str, now: str,
    ) -> Mapping[str, Any]:
        receipt = execute_analysis(
            provider=self.provider, binding=manifest["binding"],
            personal_db_path=self.personal_db, external_db_path=self.external_db,
            analysis_db_path=self.analysis_db, policy_path=self.policy_path,
            goal=str(manifest["goal"]), constraints=tuple(manifest["constraints"]),
            weights=dict(manifest["weights"]), risk_budget="low",
            confirmation=ConfirmationEvent(confirmation_event_id, now, True),
            personal_evidence=tuple(input_payload.get("personal_evidence") or ()),
            external_evidence=tuple(input_payload.get("external_evidence") or ()),
            temperature=0, max_output_tokens=self.max_output_tokens,
            max_total_tokens=self.max_total_tokens, timeout_seconds=self.timeout_seconds,
            max_attempts=1, write=True, now=now,
        )
        if receipt.stage != "publish" or not receipt.run_id or not receipt.candidate_id:
            return {
                "status": "abstain", "stage": receipt.stage,
                "reason_codes": list(receipt.reason_codes), "attempts": receipt.attempts,
            }
        detail = AnalysisReadService(self.analysis_db).get_run(receipt.run_id)
        stage_candidate = getattr(self.provider, "stage_candidate", None)
        if callable(stage_candidate):
            evidence_refs = []
            for claim in detail.get("claims") or ():
                for evidence in claim.get("evidence") or ():
                    evidence_refs.append({
                        "ref": str(evidence.get("evidence_ref_id") or evidence.get("record_id") or "evidence"),
                        "checksum": str(evidence.get("record_checksum") or evidence.get("payload_checksum") or ""),
                    })
            try:
                stage_candidate(
                    candidate_id=str(receipt.candidate_id),
                    proposal={
                        "status": str(detail.get("candidate_status") or "candidate"),
                        "claim_count": int(detail.get("claim_count") or 0),
                        "run_id": str(receipt.run_id),
                    },
                    evidence_refs=evidence_refs,
                    candidate_checksum=str(detail["candidate_checksum"]),
                    run_checksum=str(detail["run_checksum"]),
                )
            except Exception:
                # The authority write above is retained, but the Pi receipt
                # contract must not report a completed generation when its
                # durable candidate staging failed.
                return {
                    "status": "abstain", "stage": "pi_candidate_stage",
                    "reason_codes": ["pi_candidate_stage_failed"], "attempts": receipt.attempts,
                }
        return {
            "status": "success",
            "references": {
                "run_id": receipt.run_id, "candidate_id": receipt.candidate_id,
                "run_checksum": detail["run"]["run_checksum"],
                "candidate_checksum": detail["candidate"]["payload_checksum"],
                "request_checksum": receipt.request_checksum,
                "response_checksum": receipt.response_checksum,
            },
            "receipt": {key: value for key, value in asdict(receipt).items() if key != "telemetry"},
        }


__all__ = [
    "ExistingAnalysisAdapter", "GenerationRunner", "execute_confirmed_generation",
    "finalize_generation", "reserve_generation",
]
