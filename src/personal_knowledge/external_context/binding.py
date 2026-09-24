"""Strongly typed cross-database Personal/External decision-context binding (OC-10).

Canonical implementation, relocated from
``intelligence/decision/context_binding.py``. This binding contract reads both
the personal serving authority (read-only SQL) and the external snapshot
authority, so it lives in the ``external_context`` package where the external
read primitives it depends on already exist.  The intelligence package imports
the same symbols through a thin re-export facade at
``intelligence/decision/context_binding.py``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .schema import checksum
from .service import (
    ExternalContextServiceError,
    validate_active_snapshot_policy,
)
from .snapshots import ExternalSnapshotError, get_active_snapshot


class DecisionContextBindingError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class DecisionContextPolicy:
    region: str
    max_external_age_seconds: int
    conflict_policy: str = "reject"

    def __post_init__(self) -> None:
        if not self.region:
            raise DecisionContextBindingError("region_required")
        if (isinstance(self.max_external_age_seconds, bool)
                or not isinstance(self.max_external_age_seconds, int)
                or self.max_external_age_seconds < 0):
            raise DecisionContextBindingError("invalid_freshness_policy")
        if self.conflict_policy != "reject":
            raise DecisionContextBindingError("unsupported_conflict_policy", self.conflict_policy)


@dataclass(frozen=True)
class DecisionContextBinding:
    personal_snapshot_id: str
    personal_snapshot_hash: str
    external_snapshot_id: str
    external_snapshot_hash: str
    policy: DecisionContextPolicy
    bound_at: str
    binding_hash: str

    def core(self) -> dict[str, Any]:
        return {
            "schema_version": "decision_context_binding_v1",
            "personal_snapshot_id": self.personal_snapshot_id,
            "personal_snapshot_hash": self.personal_snapshot_hash,
            "external_snapshot_id": self.external_snapshot_id,
            "external_snapshot_hash": self.external_snapshot_hash,
            "policy": asdict(self.policy),
            "bound_at": self.bound_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.core(), "binding_hash": self.binding_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DecisionContextBinding":
        try:
            policy = DecisionContextPolicy(**dict(value["policy"]))
            return cls(
                personal_snapshot_id=str(value["personal_snapshot_id"]),
                personal_snapshot_hash=str(value["personal_snapshot_hash"]),
                external_snapshot_id=str(value["external_snapshot_id"]),
                external_snapshot_hash=str(value["external_snapshot_hash"]),
                policy=policy, bound_at=str(value["bound_at"]),
                binding_hash=str(value["binding_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionContextBindingError("binding_payload_invalid") from exc


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _personal_active(db_path: Path | str) -> dict[str, str]:
    path = Path(db_path)
    if not path.exists():
        raise DecisionContextBindingError("personal_database_missing", str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        row = con.execute(
            "SELECT s.snapshot_id,s.manifest_json,s.manifest_hash,s.status "
            "FROM serving_authority a JOIN serving_snapshots s ON s.snapshot_id=a.active_snapshot_id "
            "WHERE a.singleton_id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise DecisionContextBindingError("personal_authority_invalid", str(exc)) from exc
    finally:
        con.close()
    if row is None:
        raise DecisionContextBindingError("personal_authority_missing")
    if str(row["status"]) != "validated":
        raise DecisionContextBindingError("personal_snapshot_not_validated", str(row["snapshot_id"]))
    try:
        manifest = json.loads(str(row["manifest_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise DecisionContextBindingError("personal_manifest_invalid") from exc
    if checksum(manifest) != str(row["manifest_hash"]):
        raise DecisionContextBindingError("personal_snapshot_hash_mismatch", str(row["snapshot_id"]))
    return {"snapshot_id": str(row["snapshot_id"]), "snapshot_hash": str(row["manifest_hash"])}


def _validate(
    binding: DecisionContextBinding,
    personal_db_path: Path | str,
    external_db_path: Path | str,
    *,
    now: str,
) -> dict[str, Any]:
    if checksum(binding.core()) != binding.binding_hash:
        raise DecisionContextBindingError("binding_hash_mismatch")
    personal = _personal_active(personal_db_path)
    if (personal["snapshot_id"] != binding.personal_snapshot_id
            or personal["snapshot_hash"] != binding.personal_snapshot_hash):
        raise DecisionContextBindingError("personal_authority_drift", binding.personal_snapshot_id)
    try:
        external = validate_active_snapshot_policy(
            external_db_path,
            snapshot_id=binding.external_snapshot_id,
            snapshot_hash=binding.external_snapshot_hash,
            region=binding.policy.region,
            now=now,
            max_age_seconds=binding.policy.max_external_age_seconds,
            conflict_policy=binding.policy.conflict_policy,
        )
    except ExternalContextServiceError as exc:
        raise DecisionContextBindingError(exc.code, exc.detail) from exc
    return {"personal": personal, "external": external, "binding": binding.to_dict()}


def create_decision_context_binding(
    personal_db_path: Path | str,
    external_db_path: Path | str,
    *,
    region: str,
    max_external_age_seconds: int,
    now: str | None = None,
    conflict_policy: str = "reject",
) -> DecisionContextBinding:
    """Create only after both currently active authorities pass exact read-only checks."""
    timestamp = now or _now()
    policy = DecisionContextPolicy(region, max_external_age_seconds, conflict_policy)
    personal = _personal_active(personal_db_path)
    try:
        external = get_active_snapshot(external_db_path)
    except ExternalSnapshotError as exc:
        raise DecisionContextBindingError(exc.code, exc.detail) from exc
    if external is None:
        raise DecisionContextBindingError("external_authority_missing")
    draft = DecisionContextBinding(
        personal_snapshot_id=personal["snapshot_id"], personal_snapshot_hash=personal["snapshot_hash"],
        external_snapshot_id=str(external["snapshot_id"]), external_snapshot_hash=str(external["snapshot_hash"]),
        policy=policy, bound_at=timestamp, binding_hash="",
    )
    binding = replace(draft, binding_hash=checksum(draft.core()))
    _validate(binding, personal_db_path, external_db_path, now=timestamp)
    return binding


def validate_decision_context_binding(
    binding: DecisionContextBinding | Mapping[str, Any],
    personal_db_path: Path | str,
    external_db_path: Path | str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Read/consume gate: revalidate both active authorities every time."""
    typed = binding if isinstance(binding, DecisionContextBinding) else DecisionContextBinding.from_dict(binding)
    return _validate(typed, personal_db_path, external_db_path, now=now or _now())


read_decision_context_binding = validate_decision_context_binding


__all__ = [
    "DecisionContextBinding", "DecisionContextBindingError", "DecisionContextPolicy",
    "create_decision_context_binding", "read_decision_context_binding",
    "validate_decision_context_binding",
]
