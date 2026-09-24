"""Auditable Pi runtime mode authority; fresh state is always legacy."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODES = ("legacy", "shadow", "canary", "primary")
UPGRADES = {"legacy": {"shadow"}, "shadow": {"canary"}, "canary": {"primary"}}
DOWNGRADES = {"primary": {"canary", "shadow", "legacy"}, "canary": {"shadow", "legacy"}, "shadow": {"legacy"}, "legacy": set()}
REQUIRED_READINESS_SCHEMA = "pi-primary-readiness-v1"

def _now() -> str: return datetime.now(timezone.utc).isoformat()
def _checksum(value: Any) -> str: return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def _as_int(value: Any, default: int = 0) -> int:
    try: return int(value)
    except (TypeError, ValueError): return default

def _load_json(path: Path | str, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ActivationError(error_code) from exc
    if not isinstance(value, dict):
        raise ActivationError(error_code)
    return value

def validate_primary_readiness(
    readiness_path: Path | str,
    *,
    inventory_path: Path | str | None = None,
    baseline_path: Path | str | None = None,
    fault_matrix_path: Path | str | None = None,
    browser_uat_path: Path | str | None = None,
) -> dict[str, Any]:
    """Validate the evidence bundle required before a primary activation."""
    root = Path(__file__).resolve().parents[3]
    readiness = _load_json(readiness_path, "primary_readiness_evidence_invalid")
    inventory = _load_json(inventory_path or root / "governance/manifests/ai/pi-ai-entrypoints.json", "primary_inventory_invalid")
    baseline = _load_json(baseline_path or root / "ops/reports/evidence/pi-phase53-real-paired-baseline.json", "primary_baseline_invalid")
    fault_matrix = _load_json(fault_matrix_path or root / "ops/reports/evidence/pi-kernel-fault-matrix.json", "primary_fault_matrix_invalid")
    browser_uat = _load_json(browser_uat_path or root / "ops/reports/evidence/pi-browser-uat.json", "primary_browser_uat_invalid")
    reason_codes: list[str] = []

    if readiness.get("schema") != REQUIRED_READINESS_SCHEMA:
        reason_codes.append("readiness_schema_invalid")
    if readiness.get("status") != "READY":
        reason_codes.append("readiness_status_not_ready")

    entrypoints = inventory.get("entrypoints")
    if not isinstance(entrypoints, list):
        reason_codes.append("entrypoint_inventory_invalid")
        production_ids: list[str] = []
    else:
        production_ids = [
            str(row["id"])
            for row in entrypoints
            if isinstance(row, dict)
            and row.get("status") == "migrated"
            and row.get("target_route")
            and row.get("id") != "analysis.providers.ReplayProvider"
        ]

    if baseline.get("status") != "PASS":
        reason_codes.append("baseline_not_pass")
    if baseline.get("evidence_class") != "real_authorized_paired_baseline":
        reason_codes.append("baseline_evidence_class_invalid")
    member_count = _as_int(baseline.get("member_count", baseline.get("sample_size", 0)))
    if member_count < 2:
        reason_codes.append("baseline_sample_below_minimum")
    if baseline.get("provider_calls") != member_count * 2:
        reason_codes.append("baseline_provider_call_count_invalid")
    if baseline.get("raw_bodies_committed") is True or baseline.get("authority_mutations", 0) != 0:
        reason_codes.append("baseline_privacy_or_authority_violation")
    parity = baseline.get("paired_parity")
    if not isinstance(parity, dict) or not all(parity.get(key) is True for key in ("same_frozen_protocol", "same_model", "same_purpose", "one_call_per_arm")) or parity.get("silent_retry") is not False:
        reason_codes.append("baseline_pairing_invalid")
    arms = baseline.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"personalized", "generic"}:
        reason_codes.append("baseline_arms_invalid")
    else:
        for arm in arms.values():
            if not isinstance(arm, dict) or arm.get("schema_valid") is not True or arm.get("task_state") not in {"succeeded", "completed", "direct_completed"}:
                reason_codes.append("baseline_arm_receipt_invalid")
                break

    if fault_matrix.get("evidence_class") != "synthetic_replay" or fault_matrix.get("provider_calls") != 0:
        reason_codes.append("fault_matrix_not_synthetic")
    cases = fault_matrix.get("cases")
    if not isinstance(cases, list) or not cases or any(not isinstance(case, dict) or case.get("status") != "PASS" for case in cases):
        reason_codes.append("fault_matrix_not_pass")

    if browser_uat.get("human_acceptance_signed") is not True or browser_uat.get("privacy_boundary") != "PASS_NO_PROMPT_OR_PROVIDER_BODY" or browser_uat.get("authority_mutations", 0) != 0:
        reason_codes.append("browser_uat_not_signed_or_safe")
    if browser_uat.get("real_personal_cohort_accessed") is not True:
        reason_codes.append("browser_uat_personal_cohort_missing")

    receipts = readiness.get("entrypoint_receipts")
    if not isinstance(receipts, dict):
        reason_codes.append("entrypoint_receipts_missing")
    else:
        if set(receipts) != set(production_ids):
            reason_codes.append("entrypoint_receipt_inventory_mismatch")
        for entrypoint_id in production_ids:
            receipt = receipts.get(entrypoint_id)
            if not isinstance(receipt, dict) or receipt.get("provider") != "pi-kernel" or _as_int(receipt.get("receipt_count")) < 1 or _as_int(receipt.get("legacy_receipt_count")) != 0:
                reason_codes.append("entrypoint_receipt_invalid")
                break
    if readiness.get("authority_mutations", 0) != 0 or readiness.get("raw_bodies_committed") is True:
        reason_codes.append("readiness_privacy_or_authority_violation")

    unique_reasons = list(dict.fromkeys(reason_codes))
    return {
        "ready": not unique_reasons,
        "schema": REQUIRED_READINESS_SCHEMA,
        "reason_codes": unique_reasons,
        "production_entrypoint_count": len(production_ids),
        "readiness_checksum": _checksum(readiness),
    }

class ActivationError(Exception):
    def __init__(self, code: str): super().__init__(code); self.code = code

class RuntimeActivation:
    def __init__(self, database_path: Path | str = "var/db/pi_runtime_activation.sqlite", pointer_path: Path | str | None = None, policy_path: Path | str | None = None) -> None:
        self.database_path = Path(database_path); self.database_path.parent.mkdir(parents=True, exist_ok=True); self.pointer_path = Path(pointer_path or self.database_path.with_suffix(".pointer.json"))
        self.policy_path = Path(policy_path or Path(__file__).resolve().parents[3] / "governance/manifests/ai/pi-runtime-policy.json")
        try: self.policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): self.policy = {}
        self.db = sqlite3.connect(self.database_path); self.db.execute("PRAGMA foreign_keys=ON"); self.db.execute("CREATE TABLE IF NOT EXISTS activation_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, previous_mode TEXT NOT NULL, event_checksum TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, reason TEXT NOT NULL, evidence_checksum TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"); self.db.commit()
    def current(self) -> dict[str, Any]:
        row = self.db.execute("SELECT sequence,mode,previous_mode,event_checksum,actor,reason,evidence_checksum,created_at FROM activation_events ORDER BY sequence DESC LIMIT 1").fetchone()
        if not row: return {"mode": "legacy", "sequence": 0, "event_checksum": None}
        return {"sequence": row[0], "mode": row[1], "previous_mode": row[2], "event_checksum": row[3], "actor": row[4], "reason": row[5], "evidence_checksum": row[6], "created_at": row[7]}
    def prepare(self, target: str, *, evidence_checksum: str, actor: str = "user", reason: str = "", cohort: str = "", budget: int = 0, window: str = "", stop_conditions: tuple[str, ...] | list[str] = (), rollback_target: str = "legacy", readiness: bool = False, readiness_evidence_path: Path | str | None = None) -> dict[str, Any]:
        current = self.current(); if_invalid = target not in MODES
        if if_invalid: raise ActivationError("mode_invalid")
        if target not in UPGRADES.get(current["mode"], set()) and target not in DOWNGRADES.get(current["mode"], set()) and target != current["mode"]: raise ActivationError("transition_illegal")
        if target in {"canary", "primary"} and self.policy.get("phase53_decision") != "proceed": raise ActivationError("phase53_decision_not_proceed")
        readiness_evidence = None
        if target == "primary" and not readiness: raise ActivationError("primary_readiness_required")
        if target == "primary":
            if not readiness_evidence_path: raise ActivationError("primary_readiness_evidence_required")
            readiness_evidence = validate_primary_readiness(readiness_evidence_path)
            if not readiness_evidence["ready"]: raise ActivationError("primary_readiness_evidence_invalid")
        if rollback_target != "legacy": raise ActivationError("rollback_target_invalid")
        if target in UPGRADES.get(current["mode"], set()) and not evidence_checksum: raise ActivationError("evidence_required")
        preview = {"from": current["mode"], "to": target, "actor": actor, "reason": reason, "evidence_checksum": evidence_checksum, "cohort": cohort, "window": window, "budget": int(budget), "stop_conditions": list(stop_conditions), "rollback_target": rollback_target, "readiness": bool(readiness), "readiness_checksum": readiness_evidence.get("readiness_checksum") if readiness_evidence else None, "readiness_evidence_path": str(readiness_evidence_path) if readiness_evidence_path else None}
        return {"preview": preview, "preview_checksum": _checksum(preview), "confirmation_phrase": f"CONFIRM {target.upper()} {_checksum(preview)[:12]}"}
    def confirm(self, prepared: dict[str, Any], *, confirmation_phrase: str, idempotency_key: str, actor: str = "user") -> dict[str, Any]:
        preview = prepared.get("preview") or {}; checksum = prepared.get("preview_checksum")
        if checksum != _checksum(preview) or confirmation_phrase != f"CONFIRM {preview.get('to','').upper()} {checksum[:12]}": raise ActivationError("confirmation_mismatch")
        current = self.current(); target = preview.get("to")
        if target in UPGRADES.get(current["mode"], set()) and preview.get("evidence_checksum") in {"", None, "blocked"}: raise ActivationError("evidence_required")
        if target == "primary":
            readiness_path = preview.get("readiness_evidence_path")
            if not preview.get("readiness") or not readiness_path: raise ActivationError("primary_readiness_evidence_required")
            readiness = validate_primary_readiness(readiness_path)
            if not readiness["ready"] or readiness["readiness_checksum"] != preview.get("readiness_checksum"): raise ActivationError("primary_readiness_evidence_invalid")
        event_checksum = _checksum({"preview_checksum": checksum, "idempotency_key": idempotency_key, "sequence": current["sequence"] + 1})
        old_pointer = self.pointer_path.read_bytes() if self.pointer_path.exists() else None
        temp_pointer = self.pointer_path.with_name(self.pointer_path.name + ".tmp")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO activation_events(mode,previous_mode,event_checksum,actor,reason,evidence_checksum,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (target,current["mode"],event_checksum,actor,preview.get("reason", ""),preview.get("evidence_checksum", ""),idempotency_key,_now()))
            state = self.current()
            temp_pointer.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            os.replace(temp_pointer, self.pointer_path)
            self.db.commit()
            return state
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise ActivationError("idempotency_conflict")
        except Exception as exc:
            self.db.rollback()
            try:
                if old_pointer is None: self.pointer_path.unlink(missing_ok=True)
                else: self.pointer_path.write_bytes(old_pointer)
                temp_pointer.unlink(missing_ok=True)
            except OSError: pass
            raise ActivationError("activation_atomicity_failed") from exc
    def downgrade(self, reason: str, *, actor: str = "automatic-stop", evidence_checksum: str = "stop-condition") -> dict[str, Any]:
        current = self.current(); target = "legacy"
        if current["mode"] == target: return current
        preview = self.prepare(target, evidence_checksum=evidence_checksum, actor=actor, reason=reason)
        return self.confirm(preview, confirmation_phrase=preview["confirmation_phrase"], idempotency_key=f"downgrade:{current['sequence']+1}", actor=actor)
    def close(self) -> None: self.db.close()

__all__ = ["ActivationError", "RuntimeActivation", "validate_primary_readiness"]
