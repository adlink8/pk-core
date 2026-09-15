"""Deterministic preview, confirmation and replay core for decision sessions."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding, create_decision_context_binding,
    validate_decision_context_binding,
)

from .models import (
    OrchestrationError, OperationResult, Preview, REGISTRY_ID, SCHEMA_VERSION,
    canonical_json, checksum, event_id, stable_id,
)
from .schema import inspect_schema


TRANSITIONS = {
    "generate": ("confirmed", "generated"),
    "publish": ("generated", "published"),
    "decide": ("published", "decided"),
    "preregister": ("decided", "preregistered"),
    "action_start": ("preregistered", "action_started"),
    "action_complete": ("action_started", "action_completed"),
    "observe": ("action_completed", "observed"),
    "calibrate": ("observed", "calibrated"),
}
FORBIDDEN_RISK_TERMS = frozenset({
    "medical", "diagnosis", "legal", "lawsuit", "investment", "trading",
    "purchase", "payment", "deploy", "send message", "safety critical",
})


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OrchestrationError("timestamp_invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OrchestrationError("timestamp_invalid") from exc


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_core(
    *, session_id: str, sequence: int, operation: str, from_state: str,
    to_state: str, previous_event_checksum: str, payload_checksum: str,
    idempotency_key: str, actor_identity_hash: str,
    confirmation_digest: str, occurred_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "session_id": session_id,
        "sequence": sequence, "operation": operation, "from_state": from_state,
        "to_state": to_state, "previous_event_checksum": previous_event_checksum,
        "payload_checksum": payload_checksum, "idempotency_key": idempotency_key,
        "actor_identity_hash": actor_identity_hash,
        "confirmation_digest": confirmation_digest, "occurred_at": occurred_at,
    }


class OrchestrationService:
    def __init__(
        self, *, db_path: Path | str, personal_db: Path | str,
        external_db: Path | str, confirmation_secret: bytes,
        binding_factory: Callable[..., DecisionContextBinding] = create_decision_context_binding,
        binding_validator: Callable[..., Mapping[str, Any]] = validate_decision_context_binding,
    ) -> None:
        if len(confirmation_secret) < 32:
            raise OrchestrationError("confirmation_secret_weak")
        self.db_path = Path(db_path)
        self.personal_db = Path(personal_db)
        self.external_db = Path(external_db)
        self._secret = confirmation_secret
        self._binding_factory = binding_factory
        self._binding_validator = binding_validator

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if inspect_schema(self.db_path)["schema_state"] != "applied":
            raise OrchestrationError("orchestration_schema_not_applied")
        if readonly:
            con = sqlite3.connect(
                f"file:{self.db_path.resolve().as_posix()}?mode=ro", uri=True,
            )
            con.execute("PRAGMA query_only=ON")
        else:
            con = connect_rw(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @staticmethod
    def _validate_actor(value: str) -> str:
        actor = str(value or "").strip()
        if len(actor) != 64 or any(char not in "0123456789abcdef" for char in actor):
            raise OrchestrationError("actor_identity_invalid")
        return actor

    @staticmethod
    def _validate_idempotency(value: str) -> str:
        key = str(value or "").strip()
        if not key or len(key) > 256:
            raise OrchestrationError("idempotency_key_invalid")
        return key

    def prepare(
        self, *, goal: str, constraints: tuple[str, ...] | list[str],
        weights: Mapping[str, float], actor_identity_hash: str,
        domain: str = "project", risk_budget: str = "low", region: str = "global",
        max_external_age_seconds: int = 86_400, now: str | None = None,
    ) -> Preview:
        timestamp = now or _now()
        _utc(timestamp)
        actor = self._validate_actor(actor_identity_hash)
        clean_goal = str(goal or "").strip()
        clean_constraints = tuple(str(item).strip() for item in constraints if str(item).strip())
        clean_weights = {
            str(key): (int(parsed) if parsed.is_integer() else parsed)
            for key, value in sorted(weights.items())
            for parsed in (float(value),)
        }
        searchable = " ".join((clean_goal, *clean_constraints)).lower()
        if domain != "project":
            raise OrchestrationError("domain_not_allowed")
        if risk_budget != "low":
            raise OrchestrationError("risk_budget_not_allowed")
        if not clean_goal or not clean_constraints or not clean_weights:
            raise OrchestrationError("decision_input_incomplete")
        if any(term in searchable for term in FORBIDDEN_RISK_TERMS):
            raise OrchestrationError("high_risk_or_external_action_forbidden")
        if any(not 0 <= value <= 1 for value in clean_weights.values()):
            raise OrchestrationError("weight_invalid")
        binding = self._binding_factory(
            self.personal_db, self.external_db, region=region,
            max_external_age_seconds=max_external_age_seconds, now=timestamp,
        )
        manifest = {
            "schema_version": SCHEMA_VERSION, "registry_id": REGISTRY_ID,
            "domain": domain, "risk_budget": risk_budget, "goal": clean_goal,
            "constraints": list(clean_constraints), "weights": clean_weights,
            "actor_identity_hash": actor, "binding": binding.to_dict(),
            "binding_hash": binding.binding_hash,
        }
        session_id = stable_id("ors", manifest)
        return Preview.build(
            session_id=session_id, operation="confirm", actor_identity_hash=actor,
            expected_sequence=0, payload=manifest, issued_at=timestamp,
        )

    def issue_confirmation(
        self, preview: Preview | Mapping[str, Any], *, expires_at: str | None = None,
    ) -> str:
        item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
        Preview.from_dict(item.to_dict())
        expiry = expires_at or (
            _utc(item.issued_at) + timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if _utc(expiry) <= _utc(item.issued_at) or _utc(expiry) > _utc(item.issued_at) + timedelta(minutes=10):
            raise OrchestrationError("confirmation_expiry_invalid")
        claims = {
            "schema_version": SCHEMA_VERSION, "session_id": item.session_id,
            "operation": item.operation, "preview_checksum": item.preview_checksum,
            "actor_identity_hash": item.actor_identity_hash,
            "expected_sequence": item.expected_sequence, "expires_at": expiry,
        }
        body = canonical_json(claims).encode("utf-8")
        signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
        return f"{encoded}.{signature}"

    def _confirmation_claims(
        self, preview: Preview, token: str, *, now: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            encoded, signature = token.split(".", 1)
            padded = encoded + "=" * (-len(encoded) % 4)
            body = base64.urlsafe_b64decode(padded.encode("ascii"))
            claims = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise OrchestrationError("confirmation_invalid") from exc
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise OrchestrationError("confirmation_signature_invalid")
        wanted = {
            "schema_version": SCHEMA_VERSION, "session_id": preview.session_id,
            "operation": preview.operation, "preview_checksum": preview.preview_checksum,
            "actor_identity_hash": preview.actor_identity_hash,
            "expected_sequence": preview.expected_sequence,
        }
        if any(claims.get(key) != value for key, value in wanted.items()):
            raise OrchestrationError("confirmation_binding_mismatch")
        if _utc(now) > _utc(str(claims.get("expires_at") or "")):
            raise OrchestrationError("confirmation_expired")
        return claims, hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _result(row: sqlite3.Row, *, replayed: bool) -> OperationResult:
        payload = json.loads(str(row["payload_json"]))
        return OperationResult(
            session_id=str(row["session_id"]), operation=str(row["operation"]),
            state=str(row["to_state"]), sequence=int(row["sequence"]),
            event_id=str(row["event_id"]), event_checksum=str(row["event_checksum"]),
            replayed=replayed, references=dict(payload.get("effect") or {}),
        )

    def confirm(
        self, preview: Preview | Mapping[str, Any], *, confirmation_token: str,
        idempotency_key: str, now: str | None = None,
    ) -> OperationResult:
        item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
        Preview.from_dict(item.to_dict())
        timestamp = now or _now()
        claims, confirmation_digest = self._confirmation_claims(item, confirmation_token, now=timestamp)
        key = self._validate_idempotency(idempotency_key)
        if item.operation != "confirm" or item.expected_sequence != 0:
            raise OrchestrationError("confirmation_operation_invalid")
        manifest = dict(item.payload)
        if stable_id("ors", manifest) != item.session_id:
            raise OrchestrationError("session_identity_mismatch")
        binding = dict(manifest.get("binding") or {})
        self._binding_validator(binding, self.personal_db, self.external_db, now=timestamp)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM orchestration_events WHERE session_id=? AND idempotency_key=?",
                (item.session_id, key),
            ).fetchone()
            if existing is not None:
                payload = json.loads(str(existing["payload_json"]))
                if existing["operation"] != "confirm" or payload.get("preview_checksum") != item.preview_checksum:
                    raise OrchestrationError("idempotency_conflict")
                con.rollback()
                return self._result(existing, replayed=True)
            if con.execute("SELECT 1 FROM orchestration_sessions WHERE session_id=?", (item.session_id,)).fetchone():
                raise OrchestrationError("session_already_confirmed")
            manifest_checksum = checksum(manifest)
            con.execute(
                "INSERT INTO orchestration_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item.session_id, REGISTRY_ID, SCHEMA_VERSION, "project", "low",
                 item.actor_identity_hash, canonical_json(binding), manifest["binding_hash"],
                 canonical_json(manifest), manifest_checksum, timestamp),
            )
            con.execute(
                "INSERT INTO orchestration_confirmations VALUES (?,?,?,?,?,?,?,?)",
                (confirmation_digest, item.session_id, "confirm", item.preview_checksum,
                 item.actor_identity_hash, 0, claims["expires_at"], timestamp),
            )
            payload = {"preview_checksum": item.preview_checksum, "manifest_checksum": manifest_checksum, "effect": {}}
            payload_checksum = checksum(payload)
            core = _event_core(
                session_id=item.session_id, sequence=1, operation="confirm",
                from_state="none", to_state="confirmed", previous_event_checksum="GENESIS",
                payload_checksum=payload_checksum, idempotency_key=key,
                actor_identity_hash=item.actor_identity_hash,
                confirmation_digest=confirmation_digest, occurred_at=timestamp,
            )
            digest = checksum(core)
            identifier = event_id(digest)
            con.execute(
                "INSERT INTO orchestration_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, item.session_id, 1, "confirm", "none", "confirmed", "GENESIS",
                 canonical_json(payload), payload_checksum, digest, key, item.actor_identity_hash,
                 confirmation_digest, timestamp),
            )
            con.commit()
            row = con.execute("SELECT * FROM orchestration_events WHERE event_id=?", (identifier,)).fetchone()
            return self._result(row, replayed=False)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _resume_with(self, con: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        session = con.execute("SELECT * FROM orchestration_sessions WHERE session_id=?", (session_id,)).fetchone()
        if session is None:
            raise OrchestrationError("session_missing")
        try:
            manifest = json.loads(str(session["manifest_json"]))
            binding = json.loads(str(session["binding_json"]))
        except json.JSONDecodeError as exc:
            raise OrchestrationError("session_payload_invalid") from exc
        if (checksum(manifest) != session["manifest_checksum"]
                or checksum(DecisionContextBinding.from_dict(binding).core()) != session["binding_hash"]
                or manifest.get("binding_hash") != session["binding_hash"]):
            raise OrchestrationError("session_checksum_mismatch")
        rows = con.execute(
            "SELECT * FROM orchestration_events WHERE session_id=? ORDER BY sequence", (session_id,),
        ).fetchall()
        previous = "GENESIS"
        state = "none"
        for index, row in enumerate(rows, start=1):
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as exc:
                raise OrchestrationError("event_payload_invalid") from exc
            core = _event_core(
                session_id=session_id, sequence=index, operation=str(row["operation"]),
                from_state=str(row["from_state"]), to_state=str(row["to_state"]),
                previous_event_checksum=str(row["previous_event_checksum"]),
                payload_checksum=str(row["payload_checksum"]),
                idempotency_key=str(row["idempotency_key"]),
                actor_identity_hash=str(row["actor_identity_hash"]),
                confirmation_digest=str(row["confirmation_digest"]), occurred_at=str(row["occurred_at"]),
            )
            if (int(row["sequence"]) != index or row["previous_event_checksum"] != previous
                    or row["from_state"] != state or checksum(payload) != row["payload_checksum"]
                    or checksum(core) != row["event_checksum"] or event_id(str(row["event_checksum"])) != row["event_id"]):
                raise OrchestrationError("event_chain_invalid", str(row["event_id"]))
            previous = str(row["event_checksum"])
            state = str(row["to_state"])
        return {
            "session_id": session_id, "state": state, "sequence": len(rows),
            "last_event_checksum": previous, "manifest": manifest, "binding": binding,
            "events": [dict(row) for row in rows],
        }

    def resume(self, session_id: str, *, now: str | None = None, revalidate_binding: bool = True) -> dict[str, Any]:
        con = self._connect(readonly=True)
        try:
            view = self._resume_with(con, session_id)
        finally:
            con.close()
        if revalidate_binding:
            self._binding_validator(view["binding"], self.personal_db, self.external_db, now=now or _now())
        return view

    def preview_transition(
        self, session_id: str, operation: str, payload: Mapping[str, Any], *,
        actor_identity_hash: str, expected_sequence: int, now: str | None = None,
    ) -> Preview:
        if operation not in TRANSITIONS:
            raise OrchestrationError("operation_unknown")
        timestamp = now or _now()
        view = self.resume(session_id, now=timestamp)
        actor = self._validate_actor(actor_identity_hash)
        if actor != view["manifest"]["actor_identity_hash"]:
            raise OrchestrationError("actor_identity_mismatch")
        if expected_sequence != view["sequence"]:
            raise OrchestrationError("stale_expected_sequence")
        if view["state"] != TRANSITIONS[operation][0]:
            raise OrchestrationError("illegal_transition")
        body = {"input": dict(payload), "binding_hash": view["manifest"]["binding_hash"]}
        return Preview.build(
            session_id=session_id, operation=operation, actor_identity_hash=actor,
            expected_sequence=expected_sequence, payload=body, issued_at=timestamp,
        )

    def commit_transition(
        self, preview: Preview | Mapping[str, Any], *, confirmation_token: str,
        idempotency_key: str, references: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> OperationResult:
        item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
        Preview.from_dict(item.to_dict())
        timestamp = now or _now()
        claims, confirmation_digest = self._confirmation_claims(item, confirmation_token, now=timestamp)
        key = self._validate_idempotency(idempotency_key)
        if item.operation not in TRANSITIONS:
            raise OrchestrationError("operation_unknown")
        request_checksum = checksum(dict(item.payload))
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM orchestration_events WHERE session_id=? AND idempotency_key=?",
                (item.session_id, key),
            ).fetchone()
            if existing is not None:
                payload = json.loads(str(existing["payload_json"]))
                if existing["operation"] != item.operation or payload.get("request_checksum") != request_checksum:
                    raise OrchestrationError("idempotency_conflict")
                con.rollback()
                return self._result(existing, replayed=True)
            view = self._resume_with(con, item.session_id)
            source, target = TRANSITIONS[item.operation]
            if item.expected_sequence != view["sequence"]:
                raise OrchestrationError("stale_expected_sequence")
            if view["state"] != source:
                raise OrchestrationError("illegal_transition")
            if item.actor_identity_hash != view["manifest"]["actor_identity_hash"]:
                raise OrchestrationError("actor_identity_mismatch")
            if item.payload.get("binding_hash") != view["manifest"]["binding_hash"]:
                raise OrchestrationError("preview_binding_drift")
            self._binding_validator(view["binding"], self.personal_db, self.external_db, now=timestamp)
            sequence = view["sequence"] + 1
            con.execute(
                "INSERT INTO orchestration_confirmations VALUES (?,?,?,?,?,?,?,?)",
                (confirmation_digest, item.session_id, item.operation, item.preview_checksum,
                 item.actor_identity_hash, item.expected_sequence, claims["expires_at"], timestamp),
            )
            payload = {
                "preview_checksum": item.preview_checksum, "request_checksum": request_checksum,
                "request": dict(item.payload), "effect": dict(references or {}),
            }
            payload_checksum = checksum(payload)
            core = _event_core(
                session_id=item.session_id, sequence=sequence, operation=item.operation,
                from_state=source, to_state=target,
                previous_event_checksum=view["last_event_checksum"], payload_checksum=payload_checksum,
                idempotency_key=key, actor_identity_hash=item.actor_identity_hash,
                confirmation_digest=confirmation_digest, occurred_at=timestamp,
            )
            digest = checksum(core)
            identifier = event_id(digest)
            con.execute(
                "INSERT INTO orchestration_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, item.session_id, sequence, item.operation, source, target,
                 view["last_event_checksum"], canonical_json(payload), payload_checksum, digest,
                 key, item.actor_identity_hash, confirmation_digest, timestamp),
            )
            con.commit()
            row = con.execute("SELECT * FROM orchestration_events WHERE event_id=?", (identifier,)).fetchone()
            return self._result(row, replayed=False)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def authorize_transition(
        self, preview: Preview | Mapping[str, Any], *, confirmation_token: str,
        idempotency_key: str, now: str | None = None,
    ) -> dict[str, Any]:
        """Validate every gate before a downstream idempotent authority write."""
        item = preview if isinstance(preview, Preview) else Preview.from_dict(preview)
        Preview.from_dict(item.to_dict())
        timestamp = now or _now()
        _, confirmation_digest = self._confirmation_claims(item, confirmation_token, now=timestamp)
        key = self._validate_idempotency(idempotency_key)
        if item.operation not in TRANSITIONS:
            raise OrchestrationError("operation_unknown")
        con = self._connect(readonly=True)
        try:
            consumed = con.execute(
                "SELECT * FROM orchestration_confirmations WHERE confirmation_digest=?",
                (confirmation_digest,),
            ).fetchone()
            event = con.execute(
                "SELECT * FROM orchestration_events WHERE session_id=? AND idempotency_key=?",
                (item.session_id, key),
            ).fetchone()
        finally:
            con.close()
        if consumed is not None:
            event_payload = {} if event is None else json.loads(str(event["payload_json"]))
            if (event is None or event["operation"] != item.operation
                    or event["confirmation_digest"] != confirmation_digest
                    or consumed["preview_checksum"] != item.preview_checksum
                    or event_payload.get("request_checksum") != checksum(dict(item.payload))):
                raise OrchestrationError("confirmation_consumed")
            view = self.resume(item.session_id, now=timestamp)
            return {"replay": True, "view": view, "event_id": str(event["event_id"])}
        view = self.resume(item.session_id, now=timestamp)
        source, _ = TRANSITIONS[item.operation]
        if item.expected_sequence != view["sequence"]:
            raise OrchestrationError("stale_expected_sequence")
        if view["state"] != source:
            raise OrchestrationError("illegal_transition")
        if item.actor_identity_hash != view["manifest"]["actor_identity_hash"]:
            raise OrchestrationError("actor_identity_mismatch")
        if item.payload.get("binding_hash") != view["manifest"]["binding_hash"]:
            raise OrchestrationError("preview_binding_drift")
        return {"replay": False, "view": view}

    def get(self, session_id: str, *, now: str | None = None) -> dict[str, Any]:
        view = self.resume(session_id, now=now)
        return {key: value for key, value in view.items() if key != "events"}

    def explain(self, session_id: str, *, now: str | None = None) -> dict[str, Any]:
        view = self.resume(session_id, now=now)
        next_operation = next((op for op, (source, _) in TRANSITIONS.items() if source == view["state"]), None)
        return {
            **view, "next_operation": next_operation,
            "limitations": [
                "project domain and low risk only", "explicit confirmation required",
                "no automated external action", "no automatic calibration promotion",
            ],
        }


__all__ = ["FORBIDDEN_RISK_TERMS", "OrchestrationService", "TRANSITIONS"]
