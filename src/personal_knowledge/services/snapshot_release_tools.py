"""Guarded serving snapshot prepare/activate/rollback tools.

The implementation is intentionally adapter-driven: automated tests provide a
temporary pointer and in-memory snapshot authority. No production pointer or
serving database is selected by default.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from personal_knowledge.services.warehouse_mutations import (
    WarehouseMutationError,
    WarehouseOperationLedger,
)


OPERATIONS = frozenset({"snapshot.prepare", "snapshot.activate", "snapshot.rollback"})
SCHEMA_VERSION = "pi_snapshot_release_v1"
_TOKEN = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")


class SnapshotReleaseError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise SnapshotReleaseError(f"{name}_invalid")
    return value


class SnapshotAuthorityFixture:
    def __init__(self, *, active_pointer: str = "pointer:active", pointer_path: Path | None = None) -> None:
        self.active_pointer = _token(active_pointer, "active_pointer")
        self.pointer_path = pointer_path
        self.pointer_fingerprint = _digest(self.active_pointer)

    def read_pointer(self) -> str:
        if self.pointer_path is None or not self.pointer_path.exists():
            return self.active_pointer
        return self.pointer_path.read_text(encoding="utf-8").strip()

    def write_pointer_atomic(self, target: str) -> None:
        target = _token(target, "target_pointer")
        if self.pointer_path is None:
            self.active_pointer = target
            self.pointer_fingerprint = _digest(target)
            return
        self.pointer_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.pointer_path.with_suffix(self.pointer_path.suffix + ".tmp")
        temporary.write_text(target, encoding="utf-8")
        temporary.replace(self.pointer_path)
        self.active_pointer = target
        self.pointer_fingerprint = _digest(target)


class SnapshotReleaseTools:
    def __init__(self, ledger: WarehouseOperationLedger, *, authority: SnapshotAuthorityFixture | None = None) -> None:
        self.ledger = ledger
        self.authority = authority or SnapshotAuthorityFixture()
        self._pending: dict[str, dict[str, Any]] = {}
        self._receipts: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _validate_prepare(*, action: str, snapshot_id: str, generation_id: str, manifest: Mapping[str, Any],
                          reconcile: Mapping[str, Any], eval_passed: bool, eval_checksum: str,
                          current_pointer: str, target_pointer: str, protected_fingerprint: str) -> str:
        if action not in {"activate", "rollback"}:
            raise SnapshotReleaseError("release_action_invalid")
        _token(snapshot_id, "snapshot_id")
        _token(generation_id, "generation_id")
        if not isinstance(manifest, Mapping) or not manifest:
            raise SnapshotReleaseError("manifest_invalid")
        if any(str(key).lower() in {"body", "content", "path", "sql", "secret"} for key in manifest):
            raise SnapshotReleaseError("manifest_private_field")
        for name in ("missing", "orphan", "duplicate"):
            value = reconcile.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SnapshotReleaseError("reconcile_invalid")
            if value != 0:
                raise SnapshotReleaseError("reconcile_not_clean")
        if eval_passed is not True:
            raise SnapshotReleaseError("eval_gate_not_passed")
        _token(eval_checksum, "eval_checksum")
        _token(protected_fingerprint, "protected_fingerprint")
        return _digest(manifest)

    def prepare(self, *, action: str, snapshot_id: str, generation_id: str,
                manifest: Mapping[str, Any], reconcile: Mapping[str, Any], eval_passed: bool,
                eval_checksum: str, current_pointer: str, target_pointer: str,
                protected_fingerprint: str, idempotency_key: str, actor: str = "user",
                profile: str = "production", now: Any = None) -> dict[str, Any]:
        current_pointer = _token(current_pointer, "current_pointer")
        target_pointer = _token(target_pointer, "target_pointer")
        if self.authority.read_pointer() != current_pointer or self.authority.active_pointer != current_pointer:
            raise SnapshotReleaseError("pointer_binding_mismatch")
        manifest_checksum = self._validate_prepare(
            action=action, snapshot_id=snapshot_id, generation_id=generation_id, manifest=manifest,
            reconcile=reconcile, eval_passed=eval_passed, eval_checksum=eval_checksum,
            current_pointer=current_pointer, target_pointer=target_pointer,
            protected_fingerprint=protected_fingerprint,
        )
        operation = "snapshot.activate" if action == "activate" else "snapshot.rollback"
        plan = {
            "mode": f"snapshot_{action}", "candidate_ids": [snapshot_id], "reason": generation_id,
            "target_pointer": target_pointer, "current_pointer": current_pointer,
            "manifest_checksum": manifest_checksum, "eval_checksum": eval_checksum,
            "protected_fingerprint": protected_fingerprint,
        }
        try:
            preview = self.ledger.preview(
                operation, authority_id="serving", source_checksum=manifest_checksum,
                snapshot_checksum=manifest_checksum, watermark_checksum=_digest(current_pointer),
                count=1, idempotency_key=_token(idempotency_key, "idempotency_key"), actor=_token(actor, "actor"),
                profile=profile, plan=plan, now=now,
            )
        except WarehouseMutationError as exc:
            raise SnapshotReleaseError(exc.code) from exc
        self._pending[preview["operation_id"]] = {
            "action": action, "snapshot_id": snapshot_id, "generation_id": generation_id,
            "target_pointer": target_pointer, "current_pointer": current_pointer,
            "protected_fingerprint": protected_fingerprint,
        }
        return {
            "schema_version": SCHEMA_VERSION, "status": "previewed", "preview": preview,
            "snapshot_id": snapshot_id, "generation_id": generation_id, "manifest_checksum": manifest_checksum,
            "eval_checksum": eval_checksum, "current_pointer": current_pointer, "target_pointer": target_pointer,
            "protected_fingerprint": protected_fingerprint,
        }

    def _release_receipt(self, preview: Mapping[str, Any], base: Mapping[str, Any], *, status: str = "committed") -> dict[str, Any]:
        plan = preview.get("plan") or {}
        return {
            "schema_version": SCHEMA_VERSION, "receipt_id": f"release:{preview['operation_id'][3:]}",
            "operation_id": preview["operation_id"], "capability_id": preview["capability_id"],
            "status": status, "snapshot_id": (plan.get("candidate_ids") or [""])[0],
            "current_pointer": plan.get("current_pointer"), "active_pointer": self.authority.active_pointer,
            "manifest_checksum": plan.get("manifest_checksum"), "eval_checksum": plan.get("eval_checksum"),
            "ledger_receipt_id": base.get("receipt_id"),
        }

    def execute(self, preview: Mapping[str, Any], *, confirmed: bool, idempotency_key: str,
                now: Any = None, fault: str | None = None) -> dict[str, Any]:
        operation_id = _token(preview.get("operation_id"), "operation_id")
        pending = self._pending.get(operation_id)
        if pending is None:
            plan = preview.get("plan") or {}
            pending = {
                "action": "activate" if plan.get("mode") == "snapshot_activate" else "rollback",
                "snapshot_id": (plan.get("candidate_ids") or [""])[0],
                "target_pointer": plan.get("target_pointer"),
                "current_pointer": plan.get("current_pointer"),
            }
        if self.authority.active_pointer != pending["current_pointer"] and operation_id not in self._receipts:
            raise SnapshotReleaseError("pointer_binding_mismatch")
        if operation_id in self._receipts:
            return dict(self._receipts[operation_id])
        if fault == "before_ledger":
            raise SnapshotReleaseError("simulated_crash")
        try:
            base = self.ledger.commit(preview, confirmed=confirmed, idempotency_key=idempotency_key, now=now)
        except WarehouseMutationError as exc:
            raise SnapshotReleaseError(exc.code) from exc
        if fault == "before_pointer":
            raise SnapshotReleaseError("provider_outcome_unknown")
        if fault == "pointer_write":
            raise SnapshotReleaseError("provider_outcome_unknown")
        try:
            self.authority.write_pointer_atomic(pending["target_pointer"])
        except Exception as exc:
            raise SnapshotReleaseError("pointer_write_failed") from exc
        receipt = self._release_receipt(preview, base)
        self._receipts[operation_id] = receipt
        if fault == "after_pointer":
            raise SnapshotReleaseError("provider_outcome_unknown")
        return dict(receipt)

    def reconcile(self, operation_id: str) -> dict[str, Any]:
        operation_id = _token(operation_id, "operation_id")
        pending = self._pending.get(operation_id)
        if pending is None:
            preview = self.ledger.get_preview(operation_id)
            plan = preview.get("plan") or {}
            pending = {
                "action": "activate" if plan.get("mode") == "snapshot_activate" else "rollback",
                "snapshot_id": (plan.get("candidate_ids") or [""])[0],
                "target_pointer": plan.get("target_pointer"),
                "current_pointer": plan.get("current_pointer"),
            }
            self._pending[operation_id] = pending
        if operation_id in self._receipts:
            return dict(self._receipts[operation_id])
        try:
            base = self.ledger.reconcile_receipt(operation_id)
        except WarehouseMutationError as exc:
            raise SnapshotReleaseError(exc.code) from exc
        if self.authority.active_pointer != pending["target_pointer"]:
            self.authority.write_pointer_atomic(pending["target_pointer"])
        preview = self.ledger.get_preview(operation_id)
        receipt = self._release_receipt(preview, base, status="reconciled")
        self._receipts[operation_id] = receipt
        return dict(receipt)

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        if operation == "snapshot.prepare":
            return self.prepare(
                action=_token(params.get("action"), "action"), snapshot_id=_token(params.get("snapshot_id"), "snapshot_id"),
                generation_id=_token(params.get("generation_id"), "generation_id"), manifest=params.get("manifest") or {},
                reconcile=params.get("reconcile") or {}, eval_passed=params.get("eval_passed") is True,
                eval_checksum=_token(params.get("eval_checksum"), "eval_checksum"),
                current_pointer=_token(params.get("current_pointer"), "current_pointer"),
                target_pointer=_token(params.get("target_pointer"), "target_pointer"),
                protected_fingerprint=_token(params.get("protected_fingerprint"), "protected_fingerprint"),
                idempotency_key=_token(params.get("idempotency_key"), "idempotency_key"),
                actor=params.get("actor", "user"), profile=params.get("profile", "production"), now=params.get("now"),
            )
        if operation in {"snapshot.activate", "snapshot.rollback"}:
            if not params.get("preview"):
                raise SnapshotReleaseError("preview_required")
            return self.execute(
                params["preview"], confirmed=params.get("confirmed") is True,
                idempotency_key=_token(params.get("idempotency_key"), "idempotency_key"),
                now=params.get("now"), fault=params.get("fault"),
            )
        raise SnapshotReleaseError("operation_unknown")


__all__ = ["OPERATIONS", "SCHEMA_VERSION", "SnapshotAuthorityFixture", "SnapshotReleaseError", "SnapshotReleaseTools"]
