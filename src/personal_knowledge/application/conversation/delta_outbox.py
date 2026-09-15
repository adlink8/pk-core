"""Durable, metadata-only delivery queue for committed conversation deltas."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import os
import re
import time
from uuid import uuid4
from typing import Callable

from personal_knowledge.core.project_paths import VAR_DB


OUTBOX_DB = VAR_DB / "conversation_delta_outbox.sqlite"
_FIELDS = frozenset(
    {
        "producer", "scope", "source_checksum", "canonical_checksum",
        "watermark", "publication_version", "occurred_at", "idempotency_key",
        "committed",
    }
)
_FORBIDDEN = frozenset({"body", "content", "prompt", "completion", "credential", "secret", "sql", "statement", "token", "password", "path"})


class DeltaBodyConflict(ValueError):
    """An idempotency key was reused with different metadata."""


def _digest(body: dict) -> str:
    # occurred_at/publication_version are observation metadata. A repeated
    # sync for the same canonical checksum must retain the first body.
    stable = {k: v for k, v in body.items() if k not in {"occurred_at", "publication_version"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _resolve_path(path: Path | None) -> Path:
    return Path(path) if path is not None else Path(os.environ.get("PK_CONVERSATION_DELTA_OUTBOX", str(OUTBOX_DB)))


def _validate(body: dict) -> None:
    if not isinstance(body, dict) or body.keys() != _FIELDS:
        raise ValueError("delta body must contain only the fixed metadata fields")
    if body.get("committed") is not True:
        raise ValueError("only committed deltas may enter the outbox")
    for key in ("idempotency_key", "canonical_checksum", "watermark", "source_checksum"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise ValueError(f"missing delta metadata: {key}")
    for key in ("canonical_checksum", "watermark", "source_checksum"):
        if not re.fullmatch(r"[a-f0-9]{64}", body[key]):
            raise ValueError(f"invalid delta checksum: {key}")
    if body["watermark"] != body["canonical_checksum"]:
        raise DeltaBodyConflict("watermark must bind the committed canonical checksum")
    for key in _FIELDS - {"committed"}:
        if not isinstance(body[key], str) or not body[key] or len(body[key]) > 2048:
            raise ValueError(f"invalid delta metadata: {key}")
    if any(key.lower() in _FORBIDDEN for key in body):
        raise ValueError("forbidden sensitive field in delta metadata")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS conversation_delta_outbox ("
        "idempotency_key TEXT PRIMARY KEY, body_json TEXT NOT NULL, body_digest TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('pending','sent')), attempts INTEGER NOT NULL DEFAULT 0, "
        "event_id TEXT, sequence INTEGER, last_error TEXT, delivery_target TEXT, "
        "lease_owner TEXT, lease_until REAL)"
    )
    # Keep compatibility with a ledger created by an earlier process version.
    columns = {row[1] for row in con.execute("PRAGMA table_info(conversation_delta_outbox)")}
    if "delivery_target" not in columns:
        con.execute("ALTER TABLE conversation_delta_outbox ADD COLUMN delivery_target TEXT")
    for name, kind in (("lease_owner", "TEXT"), ("lease_until", "REAL")):
        if name not in columns:
            con.execute(f"ALTER TABLE conversation_delta_outbox ADD COLUMN {name} {kind}")
    con.commit()
    return con


def enqueue_conversation_delta(body: dict, *, path: Path | None = None, delivery_target: str | None = None) -> dict:
    """Persist one fixed metadata event, idempotently, before transport."""
    _validate(body)
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = _digest(body)
    con = _connect(_resolve_path(path))
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT body_json, body_digest, status, attempts, event_id, sequence, delivery_target FROM conversation_delta_outbox WHERE idempotency_key=?",
            (body["idempotency_key"],),
        ).fetchone()
        if row:
            if row[1] != digest:
                raise DeltaBodyConflict("idempotency key already binds a different delta body")
            return {"status": row[2], "idempotent": True, "body": json.loads(row[0]), "event_id": row[4], "sequence": row[5]}
        con.execute(
            "INSERT INTO conversation_delta_outbox(idempotency_key,body_json,body_digest,status) VALUES(?,?,?,'pending')",
            (body["idempotency_key"], encoded, digest),
        )
        if delivery_target:
            con.execute("UPDATE conversation_delta_outbox SET delivery_target=? WHERE idempotency_key=?", (delivery_target, body["idempotency_key"]))
        con.commit()
        return {"status": "pending", "idempotent": False, "body": dict(body)}
    finally:
        con.close()


def drain_conversation_delta_outbox(*, path: Path | None = None, sender: Callable[[dict], dict], limit: int = 100) -> dict:
    """Claim bounded deliveries; expired leases recover after a process crash.

    The 60-second lease exceeds the product transport's 10-second timeout.
    Crash-after-send still requires receiver-side idempotency; this is at-least-once
    delivery, not an impossible transaction spanning HTTP and local SQLite.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 100:
        raise ValueError("delivery limit must be between 0 and 100")
    resolved = _resolve_path(path)
    con = _connect(resolved)
    sent = failed = 0
    try:
        rows = con.execute(
            "SELECT idempotency_key, body_json FROM conversation_delta_outbox "
            "WHERE status='pending' AND COALESCE(lease_until,0)<=? ORDER BY rowid LIMIT ?",
            (time.time(), limit),
        ).fetchall()
        for key, encoded in rows:
            owner = uuid4().hex
            now = time.time()
            claimed = con.execute(
                "UPDATE conversation_delta_outbox SET attempts=attempts+1, lease_owner=?, lease_until=? "
                "WHERE idempotency_key=? AND status='pending' AND COALESCE(lease_until,0)<=?",
                (owner, now + 60, key, now),
            ).rowcount
            con.commit()
            if not claimed:
                continue
            try:
                result = sender(json.loads(encoded))
                if not isinstance(result, dict) or result.get("published") is not True:
                    raise RuntimeError(str((result or {}).get("reason", "transport_error")))
                con.execute(
                    "UPDATE conversation_delta_outbox SET status='sent', event_id=?, sequence=?, "
                    "last_error=NULL, lease_owner=NULL, lease_until=NULL WHERE idempotency_key=? AND lease_owner=?",
                    (result.get("event_id"), result.get("sequence"), key, owner),
                )
                sent += 1
            except Exception as exc:  # transport remains retryable
                con.execute(
                    "UPDATE conversation_delta_outbox SET last_error=?, lease_owner=NULL, lease_until=NULL "
                    "WHERE idempotency_key=? AND lease_owner=?",
                    (type(exc).__name__, key, owner),
                )
                failed += 1
            con.commit()
    finally:
        con.close()
    return {"sent": sent, "failed": failed, "pending": outbox_status(path=resolved)["pending"]}


def outbox_status(*, path: Path | None = None) -> dict[str, int]:
    resolved = _resolve_path(path)
    if not resolved.exists():
        return {"pending": 0, "sent": 0, "total": 0}
    con = sqlite3.connect(resolved.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        values = {row[0]: int(row[1]) for row in con.execute("SELECT status, COUNT(*) FROM conversation_delta_outbox GROUP BY status")}
        return {"pending": values.get("pending", 0), "sent": values.get("sent", 0), "total": sum(values.values())}
    finally:
        con.close()
