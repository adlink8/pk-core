"""Deterministic planning and fault-atomic publication of analysis candidates."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

import yaml

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw
from personal_knowledge.intelligence.decision.context_binding import DecisionContextBinding

from .migrate import inspect_schema
from .schema import (
    REGISTRY_ID, SCHEMA_VERSION, AnalysisCandidate, AnalysisClaim, AnalysisRun,
    AnalysisSchemaError, CandidateDraft, ProviderReceipt, canonical_json, checksum,
    reject_forbidden, stable_id,
)


class AnalysisRunError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_policy(path: Path | str) -> tuple[dict[str, Any], str]:
    policy_path = Path(path)
    try:
        value = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AnalysisRunError("policy_unreadable", str(policy_path)) from exc
    if not isinstance(value, dict):
        raise AnalysisRunError("policy_invalid")
    reject_forbidden(value, "policy")
    authority = value.get("authority")
    if not isinstance(authority, Mapping) or authority.get("registry_id") != REGISTRY_ID:
        raise AnalysisRunError("policy_authority_mismatch")
    if authority.get("evidence_parents") != ["a.personal_change", "s.external_fact"]:
        raise AnalysisRunError("policy_evidence_parent_mismatch")
    if not str(value.get("version") or "").strip():
        raise AnalysisRunError("policy_version_missing")
    return value, checksum(value)


def _binding(value: DecisionContextBinding | Mapping[str, Any]) -> dict[str, Any]:
    try:
        typed = value if isinstance(value, DecisionContextBinding) else DecisionContextBinding.from_dict(value)
    except Exception as exc:
        raise AnalysisRunError("binding_invalid") from exc
    payload = typed.to_dict()
    if checksum(typed.core()) != typed.binding_hash:
        raise AnalysisRunError("binding_hash_mismatch")
    return payload


def _claim_core(claim: AnalysisClaim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id, "claim_type": claim.claim_type,
        "statement": claim.statement, "evidence": [asdict(item) for item in claim.evidence],
    }


def _validate_claims(claims: tuple[AnalysisClaim, ...]) -> None:
    if len({item.claim_id for item in claims}) != len(claims):
        raise AnalysisRunError("duplicate_claim_id")
    for claim in claims:
        if checksum(_claim_core(claim)) != claim.claim_checksum:
            raise AnalysisRunError("claim_checksum_mismatch", claim.claim_id)


def _run_core(run: AnalysisRun) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "registry_id": run.registry_id,
        "binding": run.binding, "binding_hash": run.binding_hash,
        "policy_version": run.policy_version, "policy_checksum": run.policy_checksum,
        "request_checksum": run.request_checksum, "response_checksum": run.response_checksum,
        "candidate_id": run.candidate.candidate_id,
        "candidate_checksum": run.candidate.candidate_checksum,
        "claim_checksums": [item.claim_checksum for item in run.claims],
        "receipt_checksum": checksum(asdict(run.receipt)),
    }


def plan_run(
    *,
    binding: DecisionContextBinding | Mapping[str, Any],
    policy_path: Path | str,
    request_manifest: Mapping[str, Any],
    response_manifest: Mapping[str, Any],
    candidate: CandidateDraft,
    claims: Iterable[AnalysisClaim],
    receipt: ProviderReceipt,
) -> AnalysisRun:
    policy, policy_checksum = load_policy(policy_path)
    bound = _binding(binding)
    claim_items = tuple(claims)
    _validate_claims(claim_items)
    reject_forbidden(request_manifest, "request")
    reject_forbidden(response_manifest, "response")
    request_checksum, response_checksum = checksum(request_manifest), checksum(response_manifest)
    if receipt.request_checksum != request_checksum or receipt.response_checksum != response_checksum:
        raise AnalysisRunError("provider_receipt_checksum_mismatch")
    if receipt.schema_version != SCHEMA_VERSION or receipt.policy_version != str(policy["version"]):
        raise AnalysisRunError("provider_receipt_version_mismatch")
    candidate_payload = {
        "schema_version": SCHEMA_VERSION, "binding_hash": bound["binding_hash"],
        "draft": asdict(candidate),
        "claims": [{"claim_id": item.claim_id, "claim_checksum": item.claim_checksum} for item in claim_items],
    }
    candidate_checksum = checksum(candidate_payload)
    candidate_id = stable_id("dac", candidate_payload)
    provisional_core = {
        "schema_version": SCHEMA_VERSION, "registry_id": REGISTRY_ID,
        "binding": bound, "binding_hash": bound["binding_hash"],
        "policy_version": str(policy["version"]), "policy_checksum": policy_checksum,
        "request_checksum": request_checksum, "response_checksum": response_checksum,
        "candidate_id": candidate_id, "candidate_checksum": candidate_checksum,
        "claim_checksums": [item.claim_checksum for item in claim_items],
        "receipt_checksum": checksum(asdict(receipt)),
    }
    run_checksum = checksum(provisional_core)
    run_id = stable_id("dar", provisional_core)
    materialized = AnalysisCandidate(
        candidate_id=candidate_id, run_id=run_id, binding_hash=bound["binding_hash"],
        draft=candidate, candidate_payload=candidate_payload,
        candidate_checksum=candidate_checksum,
    )
    run = AnalysisRun(
        run_id=run_id, registry_id=REGISTRY_ID, binding=bound,
        binding_hash=bound["binding_hash"], policy_version=str(policy["version"]),
        policy_checksum=policy_checksum, request_manifest=dict(request_manifest),
        request_checksum=request_checksum, response_manifest=dict(response_manifest),
        response_checksum=response_checksum, candidate=materialized, claims=claim_items,
        receipt=receipt, run_checksum=run_checksum,
    )
    return validate_run(run, policy_path=policy_path)


def validate_run(run: AnalysisRun, *, policy_path: Path | str) -> AnalysisRun:
    _, current_policy_checksum = load_policy(policy_path)
    if current_policy_checksum != run.policy_checksum:
        raise AnalysisRunError("policy_checksum_drift")
    bound = _binding(run.binding)
    if bound["binding_hash"] != run.binding_hash:
        raise AnalysisRunError("binding_hash_mismatch")
    if checksum(run.request_manifest) != run.request_checksum:
        raise AnalysisRunError("request_checksum_mismatch")
    if checksum(run.response_manifest) != run.response_checksum:
        raise AnalysisRunError("response_checksum_mismatch")
    if run.receipt.request_checksum != run.request_checksum or run.receipt.response_checksum != run.response_checksum:
        raise AnalysisRunError("provider_receipt_checksum_mismatch")
    _validate_claims(run.claims)
    expected_candidate_payload = {
        "schema_version": SCHEMA_VERSION, "binding_hash": run.binding_hash,
        "draft": asdict(run.candidate.draft),
        "claims": [{"claim_id": item.claim_id, "claim_checksum": item.claim_checksum} for item in run.claims],
    }
    if (run.candidate.candidate_payload != expected_candidate_payload
            or checksum(expected_candidate_payload) != run.candidate.candidate_checksum
            or stable_id("dac", expected_candidate_payload) != run.candidate.candidate_id
            or run.candidate.run_id != run.run_id):
        raise AnalysisRunError("candidate_checksum_mismatch")
    if checksum(_run_core(run)) != run.run_checksum or stable_id("dar", _run_core(run)) != run.run_id:
        raise AnalysisRunError("run_checksum_mismatch")
    return run


def _validate_existing(con: sqlite3.Connection, run: AnalysisRun) -> None:
    row = con.execute("SELECT * FROM analysis_runs WHERE run_id=?", (run.run_id,)).fetchone()
    if row is None:
        raise AnalysisRunError("existing_run_missing", run.run_id)
    pairs = (
        ("binding_json", "binding_hash", run.binding, run.binding_hash),
        ("request_manifest_json", "request_checksum", run.request_manifest, run.request_checksum),
        ("response_manifest_json", "response_checksum", run.response_manifest, run.response_checksum),
    )
    for column, digest_column, value, digest in pairs:
        if (canonical_json(json.loads(str(row[column]))) != canonical_json(value)
                or str(row[digest_column]) != digest):
            raise AnalysisRunError("existing_run_checksum_mismatch", column)
    if str(row["run_checksum"]) != run.run_checksum or str(row["policy_checksum"]) != run.policy_checksum:
        raise AnalysisRunError("existing_run_checksum_mismatch", run.run_id)
    candidate = con.execute(
        "SELECT * FROM analysis_candidates WHERE run_id=?", (run.run_id,),
    ).fetchall()
    if len(candidate) != 1:
        raise AnalysisRunError("existing_run_row_count_mismatch", "candidate")
    candidate_row = candidate[0]
    expected_candidate = {
        "candidate_id": run.candidate.candidate_id,
        "binding_hash": run.binding_hash,
        "domain": run.candidate.draft.domain,
        "candidate_status": run.candidate.draft.status,
        "payload_json": canonical_json(run.candidate.candidate_payload),
        "payload_checksum": run.candidate.candidate_checksum,
    }
    if any(str(candidate_row[key]) != value for key, value in expected_candidate.items()):
        raise AnalysisRunError("existing_run_checksum_mismatch", "candidate")

    claim_rows = con.execute(
        "SELECT * FROM analysis_claims WHERE candidate_id=? ORDER BY claim_ordinal",
        (run.candidate.candidate_id,),
    ).fetchall()
    expected_claims = tuple(run.claims)
    if len(claim_rows) != len(expected_claims):
        raise AnalysisRunError("existing_run_row_count_mismatch", "claims")
    for claim_ordinal, (claim_row, claim) in enumerate(zip(claim_rows, expected_claims, strict=True)):
        if any(str(claim_row[key]) != value for key, value in {
            "claim_ordinal": str(claim_ordinal),
            "claim_id": claim.claim_id, "claim_type": claim.claim_type,
            "statement": claim.statement, "claim_checksum": claim.claim_checksum,
        }.items()):
            raise AnalysisRunError("existing_run_checksum_mismatch", f"claim:{claim.claim_id}")
        ref_rows = con.execute(
            "SELECT * FROM analysis_evidence_refs WHERE claim_id=? ORDER BY evidence_ordinal",
            (claim.claim_id,),
        ).fetchall()
        expected_refs = tuple(claim.evidence)
        if len(ref_rows) != len(expected_refs):
            raise AnalysisRunError("existing_run_row_count_mismatch", f"evidence:{claim.claim_id}")
        for ordinal, (ref_row, ref) in enumerate(zip(ref_rows, expected_refs, strict=True)):
            payload = asdict(ref)
            expected_ref = {
                "evidence_ordinal": str(ordinal),
                "authority_id": ref.authority_id, "record_type": ref.record_type,
                "record_id": ref.record_id, "record_checksum": ref.record_checksum,
                "snapshot_id": ref.snapshot_id, "snapshot_hash": ref.snapshot_hash,
                "payload_json": canonical_json(payload), "payload_checksum": checksum(payload),
            }
            if any(str(ref_row[key]) != value for key, value in expected_ref.items()):
                raise AnalysisRunError("existing_run_checksum_mismatch", f"evidence:{claim.claim_id}:{ref.record_id}")

    receipt_rows = con.execute(
        "SELECT * FROM analysis_provider_receipts WHERE run_id=?", (run.run_id,),
    ).fetchall()
    receipt_payload = asdict(run.receipt)
    if (len(receipt_rows) != 1
            or str(receipt_rows[0]["payload_json"]) != canonical_json(receipt_payload)
            or str(receipt_rows[0]["payload_checksum"]) != checksum(receipt_payload)):
        raise AnalysisRunError("existing_run_checksum_mismatch", "receipt")

    event_rows = con.execute(
        "SELECT * FROM analysis_events WHERE run_id=? ORDER BY sequence", (run.run_id,),
    ).fetchall()
    event_type = "abstained" if run.candidate.draft.status == "abstain" else "candidate_published"
    event_payload = {"run_id": run.run_id, "candidate_id": run.candidate.candidate_id,
                     "candidate_checksum": run.candidate.candidate_checksum,
                     "run_checksum": run.run_checksum}
    event_checksum = checksum({"sequence": 1, "event_type": event_type,
                               "previous_event_checksum": "GENESIS", "payload": event_payload})
    if (len(event_rows) != 1 or int(event_rows[0]["sequence"]) != 1
            or str(event_rows[0]["event_type"]) != event_type
            or str(event_rows[0]["previous_event_checksum"]) != "GENESIS"
            or str(event_rows[0]["payload_json"]) != canonical_json(event_payload)
            or str(event_rows[0]["payload_checksum"]) != event_checksum):
        raise AnalysisRunError("existing_run_checksum_mismatch", "event")


def publish_run(
    db_path: Path | str,
    run: AnalysisRun,
    *,
    policy_path: Path | str,
    write: bool = False,
    fault_at: str | None = None,
) -> dict[str, Any]:
    validate_run(run, policy_path=policy_path)
    path = Path(db_path)
    result = {"ok": True, "run_id": run.run_id, "run_checksum": run.run_checksum,
              "candidate_id": run.candidate.candidate_id, "written": False, "existing": False}
    if not write:
        return {**result, "dry_run": True}
    if inspect_schema(path)["schema_state"] != "applied":
        raise AnalysisRunError("analysis_authority_not_ready")
    con = connect_rw(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM analysis_runs WHERE run_id=?", (run.run_id,)).fetchone():
            _validate_existing(con, run)
            con.commit()
            return {**result, "existing": True, "dry_run": False}
        timestamp = _now()
        con.execute(
            "INSERT INTO analysis_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.registry_id, canonical_json(run.binding), run.binding_hash,
             run.policy_version, run.policy_checksum, canonical_json(run.request_manifest),
             run.request_checksum, canonical_json(run.response_manifest), run.response_checksum,
             run.run_checksum, "committed", timestamp),
        )
        if fault_at == "after_run": raise RuntimeError("injected analysis failure after_run")
        con.execute(
            "INSERT INTO analysis_candidates VALUES (?,?,?,?,?,?,?,?)",
            (run.candidate.candidate_id, run.run_id, run.binding_hash, run.candidate.draft.domain,
             run.candidate.draft.status, canonical_json(run.candidate.candidate_payload),
             run.candidate.candidate_checksum, timestamp),
        )
        if fault_at == "after_candidate": raise RuntimeError("injected analysis failure after_candidate")
        for claim_ordinal, claim in enumerate(run.claims):
            con.execute("INSERT INTO analysis_claims VALUES (?,?,?,?,?,?,?)",
                        (claim.claim_id, run.candidate.candidate_id, claim_ordinal, claim.claim_type,
                         claim.statement, claim.claim_checksum, timestamp))
            for ordinal, ref in enumerate(claim.evidence):
                payload = asdict(ref)
                con.execute("INSERT INTO analysis_evidence_refs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (stable_id("aer", {"claim_id": claim.claim_id, "ordinal": ordinal, **payload}), claim.claim_id,
                             ordinal,
                             ref.authority_id, ref.record_type, ref.record_id, ref.record_checksum,
                             ref.snapshot_id, ref.snapshot_hash, canonical_json(payload), checksum(payload), timestamp))
        if fault_at == "after_claims": raise RuntimeError("injected analysis failure after_claims")
        receipt_payload = asdict(run.receipt)
        con.execute("INSERT INTO analysis_provider_receipts VALUES (?,?,?,?,?)",
                    (stable_id("apr", {"run_id": run.run_id, **receipt_payload}), run.run_id,
                     canonical_json(receipt_payload), checksum(receipt_payload), timestamp))
        if fault_at == "after_receipt": raise RuntimeError("injected analysis failure after_receipt")
        event_payload = {"run_id": run.run_id, "candidate_id": run.candidate.candidate_id,
                         "candidate_checksum": run.candidate.candidate_checksum,
                         "run_checksum": run.run_checksum}
        event_checksum = checksum({"sequence": 1, "event_type": "abstained" if run.candidate.draft.status == "abstain" else "candidate_published",
                                   "previous_event_checksum": "GENESIS", "payload": event_payload})
        con.execute("INSERT INTO analysis_events VALUES (?,?,?,?,?,?,?,?)",
                    (stable_id("dae", event_checksum), run.run_id, 1,
                     "abstained" if run.candidate.draft.status == "abstain" else "candidate_published",
                     "GENESIS", canonical_json(event_payload), event_checksum, timestamp))
        if fault_at == "after_event": raise RuntimeError("injected analysis failure after_event")
        assert_foreign_key_integrity(con)
        con.commit()
        return {**result, "written": True, "dry_run": False}
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


__all__ = ["AnalysisRunError", "load_policy", "plan_run", "publish_run", "validate_run"]
