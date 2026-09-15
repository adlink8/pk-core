"""Read-only readiness checks for structured decision analysis."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .inputs import DEFAULT_POLICY_PATH, DEFAULT_PROMPT_PATH, DEFAULT_SCHEMA_PATH
from .migrate import inspect_schema
from .providers import codex_cli_preflight
from .runs import load_policy
from .schema import SCHEMA_VERSION, canonical_json, checksum, stable_id


def _fingerprint(path: Path | str) -> str:
    target = Path(path)
    if not target.exists():
        return "missing"
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authority_findings(
    db_path: Path | str, *, prompt_checksum: str, schema_checksum: str,
    policy_checksum: str, policy_version: str | None,
) -> list[str]:
    target = Path(db_path)
    con = sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON"); con.execute("PRAGMA foreign_keys=ON")
    errors: list[str] = []
    try:
        if str(con.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            errors.append("analysis_integrity_failed")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            errors.append("analysis_foreign_key_failed")
        for run in con.execute("SELECT * FROM analysis_runs ORDER BY run_id"):
            run_id = str(run["run_id"])
            try:
                binding = json.loads(str(run["binding_json"]))
                request = json.loads(str(run["request_manifest_json"]))
                response = json.loads(str(run["response_manifest_json"]))
            except json.JSONDecodeError:
                errors.append(f"analysis_run_json_invalid:{run_id}")
                continue
            if (checksum(request) != str(run["request_checksum"])
                    or checksum(response) != str(run["response_checksum"])):
                errors.append(f"analysis_run_manifest_drift:{run_id}")
            if (request.get("prompt_checksum") != prompt_checksum
                    or request.get("schema_checksum") != schema_checksum
                    or request.get("policy_checksum") != policy_checksum
                    or request.get("policy_version") != policy_version
                    or str(run["policy_checksum"]) != policy_checksum
                    or str(run["policy_version"]) != policy_version):
                errors.append(f"analysis_lineage_drift:{run_id}")
            candidates = con.execute(
                "SELECT * FROM analysis_candidates WHERE run_id=?", (run_id,),
            ).fetchall()
            receipts = con.execute(
                "SELECT * FROM analysis_provider_receipts WHERE run_id=?", (run_id,),
            ).fetchall()
            events = con.execute(
                "SELECT * FROM analysis_events WHERE run_id=? ORDER BY sequence", (run_id,),
            ).fetchall()
            if len(candidates) != 1 or len(receipts) != 1 or len(events) != 1:
                errors.append(f"analysis_child_count_drift:{run_id}")
                continue
            candidate = candidates[0]
            try:
                candidate_payload = json.loads(str(candidate["payload_json"]))
                receipt_payload = json.loads(str(receipts[0]["payload_json"]))
                event_payload = json.loads(str(events[0]["payload_json"]))
            except json.JSONDecodeError:
                errors.append(f"analysis_child_json_invalid:{run_id}")
                continue
            candidate_checksum = checksum(candidate_payload)
            receipt_checksum = checksum(receipt_payload)
            if (candidate_checksum != str(candidate["payload_checksum"])
                    or stable_id("dac", candidate_payload) != str(candidate["candidate_id"])
                    or receipt_checksum != str(receipts[0]["payload_checksum"])):
                errors.append(f"analysis_child_payload_drift:{run_id}")
            claim_checksums: list[str] = []
            for claim in con.execute(
                "SELECT * FROM analysis_claims WHERE candidate_id=? ORDER BY claim_ordinal",
                (candidate["candidate_id"],),
            ):
                evidence_payloads: list[dict[str, Any]] = []
                refs = con.execute(
                    "SELECT * FROM analysis_evidence_refs WHERE claim_id=? ORDER BY evidence_ordinal",
                    (claim["claim_id"],),
                ).fetchall()
                for ordinal, ref in enumerate(refs):
                    try:
                        payload = json.loads(str(ref["payload_json"]))
                    except json.JSONDecodeError:
                        errors.append(f"analysis_evidence_json_invalid:{claim['claim_id']}")
                        continue
                    if int(ref["evidence_ordinal"]) != ordinal or checksum(payload) != str(ref["payload_checksum"]):
                        errors.append(f"analysis_evidence_drift:{claim['claim_id']}:{ordinal}")
                    evidence_payloads.append(payload)
                claim_core = {
                    "claim_id": str(claim["claim_id"]), "claim_type": str(claim["claim_type"]),
                    "statement": str(claim["statement"]), "evidence": evidence_payloads,
                }
                if checksum(claim_core) != str(claim["claim_checksum"]):
                    errors.append(f"analysis_claim_drift:{claim['claim_id']}")
                claim_checksums.append(str(claim["claim_checksum"]))
            declared_claims = candidate_payload.get("claims") if isinstance(candidate_payload, dict) else None
            expected_claims = [
                {"claim_id": str(row["claim_id"]), "claim_checksum": str(row["claim_checksum"])}
                for row in con.execute(
                    "SELECT claim_id,claim_checksum FROM analysis_claims WHERE candidate_id=? ORDER BY claim_ordinal",
                    (candidate["candidate_id"],),
                )
            ]
            if declared_claims != expected_claims:
                errors.append(f"analysis_candidate_claim_drift:{run_id}")
            run_core = {
                "schema_version": SCHEMA_VERSION, "registry_id": str(run["registry_id"]),
                "binding": binding, "binding_hash": str(run["binding_hash"]),
                "policy_version": str(run["policy_version"]),
                "policy_checksum": str(run["policy_checksum"]),
                "request_checksum": str(run["request_checksum"]),
                "response_checksum": str(run["response_checksum"]),
                "candidate_id": str(candidate["candidate_id"]),
                "candidate_checksum": candidate_checksum,
                "claim_checksums": claim_checksums, "receipt_checksum": receipt_checksum,
            }
            if checksum(run_core) != str(run["run_checksum"]) or stable_id("dar", run_core) != run_id:
                errors.append(f"analysis_run_checksum_drift:{run_id}")
            event_core = {
                "sequence": 1, "event_type": str(events[0]["event_type"]),
                "previous_event_checksum": "GENESIS", "payload": event_payload,
            }
            if (int(events[0]["sequence"]) != 1
                    or str(events[0]["previous_event_checksum"]) != "GENESIS"
                    or checksum(event_core) != str(events[0]["payload_checksum"])):
                errors.append(f"analysis_event_drift:{run_id}")
    finally:
        con.close()
    return errors


def doctor(
    *,
    personal_db_path: Path | str,
    external_db_path: Path | str,
    analysis_db_path: Path | str,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
    schema_path: Path | str = DEFAULT_SCHEMA_PATH,
    codex_model: str | None = None,
) -> dict[str, Any]:
    paths = {
        "personal": personal_db_path, "external": external_db_path, "analysis": analysis_db_path,
    }
    before = {name: _fingerprint(path) for name, path in paths.items()}
    findings: list[str] = []
    if any(value == "missing" for value in before.values()):
        findings.append("authority_missing")
    schema_state = inspect_schema(analysis_db_path).get("schema_state") if Path(analysis_db_path).exists() else "missing"
    if schema_state != "applied":
        findings.append("analysis_authority_not_ready")
    try:
        policy, policy_checksum = load_policy(policy_path)
    except Exception:
        policy, policy_checksum = {}, "missing"
        findings.append("policy_invalid")
    lineage = {
        "prompt_checksum": _fingerprint(prompt_path),
        "schema_checksum": _fingerprint(schema_path),
        "policy_checksum": policy_checksum,
        "policy_version": policy.get("version"),
    }
    if "missing" in lineage.values():
        findings.append("lineage_asset_missing")
    if schema_state == "applied" and not findings:
        findings.extend(_authority_findings(
            analysis_db_path, prompt_checksum=lineage["prompt_checksum"],
            schema_checksum=lineage["schema_checksum"], policy_checksum=policy_checksum,
            policy_version=policy.get("version"),
        ))
    after = {name: _fingerprint(path) for name, path in paths.items()}
    unchanged = before == after
    if not unchanged:
        findings.append("doctor_mutated_authority")
    provider_preflight = codex_cli_preflight(codex_model) if codex_model else None
    if provider_preflight and not provider_preflight["ok"]:
        findings.extend(provider_preflight["findings"])
    public_preflight = (
        {key: value for key, value in provider_preflight.items() if key != "command_path"}
        if provider_preflight else None
    )
    return {
        "ok": not findings, "status": "ready" if not findings else "blocked",
        "findings": sorted(set(findings)), "schema_state": schema_state,
        "lineage": lineage, "authority_fingerprints_before": before,
        "authority_fingerprints_after": after, "unchanged": unchanged,
        "provider_preflight": public_preflight,
        "network_calls": 1 if provider_preflight else 0, "provider_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--personal-db", required=True)
    parser.add_argument("--external-db", required=True)
    parser.add_argument("--analysis-db", required=True)
    parser.add_argument("--codex-model")
    args = parser.parse_args(argv)
    report = doctor(
        personal_db_path=args.personal_db, external_db_path=args.external_db,
        analysis_db_path=args.analysis_db, codex_model=args.codex_model,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["doctor", "main"]
