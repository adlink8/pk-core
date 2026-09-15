"""Plan 61-07 dispatcher-bound reflection staging adapter (HARNESS-05).

Reflection Candidate staging starts ONLY through the Plan 61-06 dispatcher
binding: the ``conversation.reflection.stage`` PiDomainGateway provider hands
this adapter the authenticated event_id / canonical_checksum / watermark /
source / snapshot / two-freshness / rule_version / task / idempotency / binding
metadata. A direct Python/public/model call, a foreign/stale/mixed binding or a
divergent replay is rejected before any inference.

``reflection_key`` binds event_id + canonical_checksum + watermark +
rule_version and is recomputed deterministically before any Candidate work.
Exact replay returns ``duplicate`` and can never overwrite; a divergent identity
(one event_id staged under a different reflection identity) fails closed. The
staged Candidate keeps immutable Evidence refs, a reproducible Observation and an
inference Candidate (``provenance_class: inference``, ``status: candidate``)
separate -- never a canonical fact, never a body or a secret, and never an
authority mutation. The reflection ledger is metadata-only and lives only in the
adapter-owned ``db_path``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from personal_knowledge.core.project_paths import ROOT

STAGE_OUTCOMES = frozenset({"staged", "duplicate", "rejected", "failed"})
REFLECTION_KEY_FIELDS = ("event_id", "canonical_checksum", "watermark", "rule_version")

# Mirrors the 61-06 dispatcher authority: only these producers may trigger
# reflection. renderer/model.wake/schedule.learned/agent.action never stage.
ALLOWED_DISPATCHER_SOURCES = frozenset({"pk-sync", "conversation.close"})

_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_SNAPSHOT_PATTERN = re.compile(r"^agentsview@[a-f0-9]{64}$")
_REQUIRED_BINDING_FIELDS = (
    "event_id", "canonical_checksum", "watermark", "rule_version",
    "task_id", "idempotency_key",
)
_FRESHNESS_LEGS = ("source_to_agentsview", "agentsview_to_canonical")

# Deterministic Candidate metadata (fixed reviewable baseline, never learned).
CANDIDATE_CONFIDENCE = 0.4
CANDIDATE_UNCERTAINTY = (
    "reflection from a single committed conversation delta; review before acceptance"
)
VALID_TO_HORIZON = "9999-12-31T23:59:59.000Z"

# A fixed default ledger location for the gateway-provider path. The adapter
# itself never discovers or writes canonical/promotion/watermark/permission/value
# state; the ledger holds only stable reflection identities and candidate
# receipts.
DEFAULT_REFLECTION_DB = ROOT / "var" / "db" / "conversation_reflection.sqlite"


class ReflectionStageError(Exception):
    """Fail-closed validation error with a stable machine code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reflection_key(binding: Mapping[str, Any]) -> str:
    """Deterministic stable-key contract (exactly the Task 1 fixture formula).

    Binds event_id + canonical_checksum + watermark + rule_version and is
    recomputed before any validation/inference work.
    """
    return hashlib.sha256(json.dumps(
        {
            "event_id": "" if binding.get("event_id") is None else str(binding["event_id"]),
            "canonical_checksum": "" if binding.get("canonical_checksum") is None else str(binding["canonical_checksum"]),
            "watermark": "" if binding.get("watermark") is None else str(binding["watermark"]),
            "rule_version": "" if binding.get("rule_version") is None else str(binding["rule_version"]),
        },
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _validate_dispatcher_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the dispatcher-authenticated binding; raises ReflectionStageError."""
    if not isinstance(binding, Mapping):
        raise ReflectionStageError("binding_invalid", "dispatcher binding required")
    for field in _REQUIRED_BINDING_FIELDS:
        value = binding.get(field)
        if not isinstance(value, str) or not value:
            raise ReflectionStageError("missing_field", field)
    source = binding.get("source")
    if source not in ALLOWED_DISPATCHER_SOURCES:
        raise ReflectionStageError("source_invalid", str(source))
    canonical_checksum = binding["canonical_checksum"]
    watermark = binding["watermark"]
    if not _SHA256_HEX.fullmatch(canonical_checksum) or not _SHA256_HEX.fullmatch(watermark):
        raise ReflectionStageError("checksum_invalid", "canonical_checksum/watermark must be sha256 hex")
    if watermark != canonical_checksum:
        raise ReflectionStageError("watermark_mismatch", "committed watermark must equal the canonical checksum")
    snapshot = binding.get("snapshot")
    if not isinstance(snapshot, str) or not _SNAPSHOT_PATTERN.fullmatch(snapshot):
        raise ReflectionStageError("snapshot_invalid", "snapshot must be agentsview@<sha256>")
    scope = binding.get("scope")
    publication_version = binding.get("publication_version")
    occurred_at = binding.get("occurred_at")
    if not isinstance(scope, str) or not scope:
        raise ReflectionStageError("missing_field", "scope")
    if not isinstance(publication_version, str) or not publication_version:
        raise ReflectionStageError("missing_field", "publication_version")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise ReflectionStageError("missing_field", "occurred_at")
    binding_identity = binding.get("binding")
    if not isinstance(binding_identity, Mapping) or not binding_identity:
        raise ReflectionStageError("binding_required", "dispatcher binding identity required")

    freshness = binding.get("freshness")
    legs: dict[str, dict[str, Any]] = {}
    if not isinstance(freshness, Mapping):
        raise ReflectionStageError("freshness_invalid", "two freshness legs required")
    for name in _FRESHNESS_LEGS:
        leg = freshness.get(name)
        if not isinstance(leg, Mapping):
            raise ReflectionStageError("freshness_invalid", f"missing leg {name}")
        if leg.get("status") != "current":
            raise ReflectionStageError("freshness_stale", f"{name}:{leg.get('status')}")
        if not leg.get("watermark") or not leg.get("observed_at"):
            raise ReflectionStageError("freshness_incomplete", name)
        legs[name] = dict(leg)

    return {
        "event_id": binding["event_id"],
        "canonical_checksum": canonical_checksum,
        "watermark": watermark,
        "rule_version": binding["rule_version"],
        "source": source,
        "snapshot": snapshot,
        "scope": scope,
        "publication_version": publication_version,
        "occurred_at": occurred_at,
        "freshness": legs,
        "task_id": binding["task_id"],
        "idempotency_key": binding["idempotency_key"],
        "binding": dict(binding_identity),
    }


def _build_candidate(valid: Mapping[str, Any], reflection_key: str) -> dict[str, Any]:
    """Immutable Evidence refs + reproducible Observation + inference Candidate."""
    source_checksum = str(valid["snapshot"]).split("@", 1)[1]
    evidence = (
        {
            "ref": f"agentsview.snapshot@{source_checksum}",
            "checksum": source_checksum,
            "privacy_class": "R1",
            "serving_role": "source.agentsview",
            "artifact_version_id": str(valid["publication_version"]).split("#", 1)[0],
        },
        {
            "ref": f"canonical.conversation@{valid['watermark']}#{valid['publication_version']}",
            "checksum": valid["canonical_checksum"],
            "privacy_class": "R2",
            "serving_role": "agent.conversation.canonical",
            "artifact_version_id": valid["publication_version"],
        },
    )
    support_refs = tuple(ref["ref"] for ref in evidence)

    observation_core = {
        "provenance_class": "observation",
        "observed_at": valid["occurred_at"],
        "event_id": valid["event_id"],
        "canonical_checksum": valid["canonical_checksum"],
        "watermark": valid["watermark"],
        "rule_version": valid["rule_version"],
        "source": valid["source"],
        "scope": valid["scope"],
        "snapshot": valid["snapshot"],
        "freshness": valid["freshness"],
    }
    observation = {
        **observation_core,
        "observation_checksum": _checksum(observation_core),
    }

    candidate_core = {
        "provenance_class": "inference",
        "status": "candidate",
        "subject": f"conversation:{valid['scope']}",
        "scope": valid["scope"],
        "observed_at": valid["occurred_at"],
        "valid_from": valid["occurred_at"],
        "valid_to": VALID_TO_HORIZON,
        "confidence": CANDIDATE_CONFIDENCE,
        "uncertainty": CANDIDATE_UNCERTAINTY,
        "support_refs": list(support_refs),
        "conflict_refs": [],
        "evidence": tuple(dict(ref) for ref in evidence),
        "observation": observation,
        "freshness": valid["freshness"],
        "event_id": valid["event_id"],
        "reflection_key": reflection_key,
        "rule_version": valid["rule_version"],
        "task_id": valid["task_id"],
    }
    candidate_checksum = _checksum(candidate_core)
    candidate_id = "cand_" + hashlib.sha256(
        f"candidate:{reflection_key}:{candidate_checksum}".encode("utf-8")
    ).hexdigest()[:24]

    receipt_core = {
        "operation": "conversation.reflection.stage",
        "outcome": "candidate_staged",
        "reflection_key": reflection_key,
        "candidate_id": candidate_id,
        "candidate_checksum": candidate_checksum,
        "event_id": valid["event_id"],
        "rule_version": valid["rule_version"],
    }
    receipt = {
        "receipt_id": "reflection_receipt_" + _checksum(receipt_core)[:24],
        "receipt_checksum": _checksum(receipt_core),
        "operation": "conversation.reflection.stage",
        "outcome": "candidate_staged",
        "reflection_key": reflection_key,
        "candidate_id": candidate_id,
        "candidate_checksum": candidate_checksum,
        "event_id": valid["event_id"],
        "rule_version": valid["rule_version"],
        "metadata_only": True,
    }

    return {
        **candidate_core,
        "candidate_id": candidate_id,
        "candidate_checksum": candidate_checksum,
        "receipt": receipt,
    }


def _rejected(code: str, detail: str, reflection_key: str) -> dict[str, Any]:
    return {
        "status": "rejected",
        "reflection_key": reflection_key,
        "reason": f"{code}:{detail}",
    }


class HarnessReflectionAdapter:
    """Metadata-only reflection ledger guarded by the dispatcher binding.

    The ledger stores only the stable reflection identity plus candidate
    receipts. It never holds a body, prompt, credential, SQL statement or any
    canonical/promotion/permission/value state.
    """

    def __init__(self, *, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS reflection_stage ("
                "reflection_key TEXT PRIMARY KEY,"
                "event_id TEXT NOT NULL UNIQUE,"
                "canonical_checksum TEXT NOT NULL,"
                "watermark TEXT NOT NULL,"
                "rule_version TEXT NOT NULL,"
                "candidate_id TEXT NOT NULL,"
                "candidate_checksum TEXT NOT NULL,"
                "staged_at TEXT NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS reflection_candidate_binding ("
                "reflection_key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE, event_id TEXT NOT NULL, "
                "canonical_checksum TEXT NOT NULL, watermark TEXT NOT NULL, rule_version TEXT NOT NULL, "
                "source TEXT NOT NULL, snapshot TEXT NOT NULL, scope TEXT NOT NULL, "
                "publication_version TEXT NOT NULL, occurred_at TEXT NOT NULL, freshness_json TEXT NOT NULL, "
                "binding_json TEXT NOT NULL, task_id TEXT NOT NULL)"
            )
            con.commit()
        finally:
            con.close()

    def stage(self, **binding: Any) -> dict[str, Any]:
        """Stage one dispatcher-authenticated delta into a review Candidate.

        Returns one of ``staged`` / ``duplicate`` / ``rejected`` / ``failed``.
        Never raises for a rejected or duplicate binding and never writes
        canonical/promotion/pointer/permission/value state.
        """
        reflection_key = _reflection_key(binding)
        try:
            valid = _validate_dispatcher_binding(binding)
        except ReflectionStageError as exc:
            return _rejected(exc.code, exc.detail, reflection_key)

        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT candidate_id, candidate_checksum FROM reflection_stage "
                "WHERE reflection_key=?", (reflection_key,),
            ).fetchone()
            if row is not None:
                return {
                    "status": "duplicate",
                    "reflection_key": reflection_key,
                    "candidate_id": str(row[0]),
                    "candidate_checksum": str(row[1]),
                    "reason": "duplicate: reflection key already staged",
                }
            existing_event = con.execute(
                "SELECT reflection_key FROM reflection_stage WHERE event_id=?",
                (valid["event_id"],),
            ).fetchone()
            if existing_event is not None and str(existing_event[0]) != reflection_key:
                return _rejected(
                    "divergent_replay",
                    "same event id staged under a different reflection identity",
                    reflection_key,
                )
            candidate = _build_candidate(valid, reflection_key)
            con.execute(
                "INSERT INTO reflection_stage (reflection_key, event_id, canonical_checksum, "
                "watermark, rule_version, candidate_id, candidate_checksum, staged_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    reflection_key, valid["event_id"], valid["canonical_checksum"],
                    valid["watermark"], valid["rule_version"], candidate["candidate_id"],
                    candidate["candidate_checksum"], _now_utc(),
                ),
            )
            con.execute(
                "INSERT INTO reflection_candidate_binding VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (reflection_key, candidate["candidate_id"], valid["event_id"], valid["canonical_checksum"],
                 valid["watermark"], valid["rule_version"], valid["source"], valid["snapshot"],
                 valid["scope"], valid["publication_version"], valid["occurred_at"],
                 _canonical_json(valid["freshness"]), _canonical_json(valid["binding"]), valid["task_id"]),
            )
            con.commit()
            return {
                "status": "staged",
                "reflection_key": reflection_key,
                "candidate_id": candidate["candidate_id"],
                "candidate_checksum": candidate["candidate_checksum"],
                "candidate": candidate,
                "receipt": candidate["receipt"],
            }
        except sqlite3.Error as exc:  # transport-safe: ledger failure is not a staged candidate
            return {"status": "failed", "reflection_key": reflection_key, "reason": f"reflection_store_unavailable:{type(exc).__name__}"}
        except Exception as exc:  # noqa: BLE001 - stage must never leak an internal trace
            return {"status": "failed", "reflection_key": reflection_key, "reason": f"unexpected:{type(exc).__name__}"}
        finally:
            con.close()

    @classmethod
    def load_candidates(cls, db_path: Path | str) -> dict[str, dict[str, Any]]:
        """Read only verified candidates persisted by :meth:`stage`."""
        path = Path(db_path)
        if not path.exists():
            return {}
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            try:
                rows = con.execute(
                    "SELECT b.reflection_key,b.candidate_id,b.event_id,b.canonical_checksum,b.watermark,"
                    "b.rule_version,b.source,b.snapshot,b.scope,b.publication_version,b.occurred_at,"
                    "b.freshness_json,b.binding_json,b.task_id,s.candidate_checksum "
                    "FROM reflection_candidate_binding b JOIN reflection_stage s ON s.reflection_key=b.reflection_key"
                ).fetchall()
            except sqlite3.Error:
                return {}
        finally:
            con.close()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                (key, candidate_id, event_id, checksum, watermark, rule_version, source, snapshot,
                 scope, publication, occurred, freshness_json, binding_json, task_id, stored_checksum) = row
                valid = _validate_dispatcher_binding({
                    "event_id": event_id, "canonical_checksum": checksum, "watermark": watermark,
                    "rule_version": rule_version, "source": source, "snapshot": snapshot, "scope": scope,
                    "publication_version": publication, "occurred_at": occurred,
                    "freshness": json.loads(freshness_json), "task_id": task_id,
                    "idempotency_key": "recovered", "binding": json.loads(binding_json),
                })
                candidate = _build_candidate(valid, str(key))
                if candidate["candidate_id"] != str(candidate_id) or candidate["candidate_checksum"] != str(stored_checksum):
                    continue
                result[str(candidate_id)] = candidate
            except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError, ReflectionStageError):
                continue
        return result


__all__ = [
    "ALLOWED_DISPATCHER_SOURCES",
    "CANDIDATE_CONFIDENCE",
    "DEFAULT_REFLECTION_DB",
    "HarnessReflectionAdapter",
    "REFLECTION_KEY_FIELDS",
    "ReflectionStageError",
    "STAGE_OUTCOMES",
]
