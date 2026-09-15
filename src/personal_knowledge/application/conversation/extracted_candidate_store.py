"""Immutable, evidence-bound candidates derived from the conversation queue."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository
from personal_knowledge.application.knowledge.candidate_manifest import manifest_payload
from personal_knowledge.application.knowledge.eligibility import strip_system_injections
from personal_knowledge.application.conversation.build_agentsview_normalized import local_secret_scan
from personal_knowledge.core.conversation_events import EventKind, FieldDisposition

_TABLE = "ce_extracted_candidates"
_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
 candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, queue_candidate_id TEXT NOT NULL,
 candidate_checksum TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, proof_json TEXT NOT NULL,
 created_at TEXT NOT NULL
)
"""
_VALID_TO = "9999-12-31T23:59:59Z"
_RULE_VERSION = "conversation-candidate-v1"
_LIMITS = {"subject": 200, "conclusion": 2048, "scope": 200, "quote": 4000}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _LIMITS[name]:
        raise ValueError(f"invalid {name}")
    labeled_secret = re.search(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]", value)
    if local_secret_scan(value) or labeled_secret or strip_system_injections(value) != value.strip():
        raise ValueError(f"unsafe {name}: secret or injection")
    return value


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _active_generation(con: sqlite3.Connection, generation_id: str) -> bool:
    row = con.execute(
        "SELECT active FROM ce_generation_authority WHERE generation_id=?", (generation_id,)
    ).fetchone()
    return bool(row and row["active"] == 1)


def _event_rows(con: sqlite3.Connection, generation_id: str, ids: list[str]) -> dict[str, sqlite3.Row]:
    marks = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT event_id, kind, content, fidelity_json FROM ce_events "
        f"WHERE generation_id=? AND event_id IN ({marks})", (generation_id, *ids),
    ).fetchall()
    return {row["event_id"]: row for row in rows}


def _kept_content(con: sqlite3.Connection, generation_id: str, event_id: str) -> bool:
    rows = con.execute(
        "SELECT disposition FROM ce_field_dispositions WHERE generation_id=? AND event_id=?",
        (generation_id, event_id),
    ).fetchall()
    return all(row["disposition"] == FieldDisposition.MAPPED.value for row in rows)


def _context_signature(con: sqlite3.Connection, generation_id: str, event_id: str) -> str:
    rows = con.execute(
        "SELECT field_name, disposition, reason FROM ce_field_dispositions "
        "WHERE generation_id=? AND event_id=? ORDER BY field_name",
        (generation_id, event_id),
    )
    dispositions = [list(row) for row in rows]
    position = con.execute(
        "SELECT session_id,ordinal,occurred_at FROM ce_events WHERE generation_id=? AND event_id=?",
        (generation_id, event_id),
    ).fetchone()
    return _sha(_json({"dispositions": dispositions, "position": list(position) if position else None}))


def _validate_context_checksums(
    con: sqlite3.Connection, generation_id: str, checksums: dict[str, str], queue_refs: set[str],
) -> list[dict[str, str]]:
    if not isinstance(checksums, dict) or len(checksums) > 100:
        raise ValueError("context mapping limit")
    if not set(checksums).issubset(queue_refs) or any(
        not isinstance(key, str) or not isinstance(value, str) or
        not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in checksums.items()
    ):
        raise ValueError("context mapping outside queue or invalid checksum")
    if not checksums:
        raise ValueError("context mapping must include evidence")
    rows = _event_rows(con, generation_id, list(checksums))
    if len(rows) != len(checksums):
        raise ValueError("context event missing")
    proof = []
    for event_id, expected in sorted(checksums.items()):
        row = rows[event_id]
        if row["kind"] not in (EventKind.USER_MESSAGE.value, EventKind.ASSISTANT_MESSAGE.value) or not row["content"] or not _kept_content(con, generation_id, event_id):
            raise ValueError("context event is not eligible")
        actual = _sha(row["content"])
        if actual != expected:
            raise ValueError("context checksum mismatch")
        proof.append({"event_id": event_id, "content_checksum": actual, "kind": row["kind"], "disposition_checksum": _context_signature(con, generation_id, event_id)})
    return proof


class ExtractedCandidateStore:
    """Stage and verify immutable extracted candidates without an LLM/provider."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def stage(
        self, conversation_db: Path, run_id: str, queue_candidate_id: str, *,
        subject: str, conclusion: str, scope: str, confidence: float,
        evidence: list[dict[str, str]],
        expected_context_checksums: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        subject = _check_text("subject", subject)
        conclusion = _check_text("conclusion", conclusion)
        scope = _check_text("scope", scope)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite in [0,1]")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            raise ValueError("evidence must contain 1..8 items")
        repo = CandidateRunRepository(Path(conversation_db))
        run = repo.get_run(run_id)
        if run is None:
            raise ValueError("candidate run not found")
        if any(set(item) != {"event_id", "quote"} for item in evidence if isinstance(item, dict)) or any(not isinstance(item, dict) for item in evidence):
            raise ValueError("evidence items must contain only event_id and quote")
        ids = [item["event_id"] for item in evidence]
        if any(not isinstance(event_id, str) or not event_id for event_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid evidence event IDs")
        source = _connect(Path(conversation_db), readonly=True)
        try:
            source.execute("BEGIN")
            manifest = manifest_payload(source, run_id)
            if manifest is None:
                raise ValueError("candidate manifest missing")
            queue = next((row for row in manifest["candidates"] if row["candidate_id"] == queue_candidate_id), None)
            if queue is None or not set(ids).issubset(set(queue["evidence_event_refs"])):
                raise ValueError("evidence is outside queue")
            context_proof = _validate_context_checksums(
                source, run.key.active_generation_id,
                expected_context_checksums, set(queue["evidence_event_refs"]),
            ) if expected_context_checksums is not None else []
            if expected_context_checksums is not None and not set(ids).issubset(expected_context_checksums):
                raise ValueError("context mapping must cover evidence")
            if not _active_generation(source, run.key.active_generation_id):
                raise ValueError("stale generation")
            rows = _event_rows(source, run.key.active_generation_id, ids)
            if len(rows) != len(ids):
                raise ValueError("missing evidence event")
            proof_events = []
            public_events = []
            for item in evidence:
                event_id, quote = item["event_id"], _check_text("quote", item["quote"])
                row = rows[event_id]
                if row["kind"] not in (EventKind.USER_MESSAGE.value, EventKind.ASSISTANT_MESSAGE.value) or not row["content"] or not _kept_content(source, run.key.active_generation_id, event_id) or quote not in row["content"]:
                    raise ValueError("invalid evidence source")
                checksum = _sha(row["content"])
                ref = f"ce.event@{run.key.active_generation_id}#{event_id}"
                public_events.append({"ref": ref, "checksum": checksum, "privacy_class": "R2", "serving_role": "agent.conversation.canonical", "artifact_version_id": run.key.active_generation_id})
                proof_events.append({"event_id": event_id, "quote": quote, "content_checksum": checksum})
            created = run.created_at
            proof = {"generation_id": run.key.active_generation_id, "events": proof_events}
            identity_input = f"{run.key.active_generation_id}|{queue_candidate_id}|{subject}|{conclusion}|{scope}|{confidence}|{_json(proof_events)}"
            if expected_context_checksums is not None:
                proof["context"] = context_proof
                identity_input += "|" + _json(context_proof)
            proof_json = _json(proof)
            identity = _sha(identity_input)
            payload = {
                "candidate_id": "cand_" + identity[:24], "candidate_checksum": "", "subject": subject,
                "conclusion": conclusion, "scope": scope, "confidence": float(confidence),
                "uncertainty": "inference requires review", "provenance_class": "inference", "status": "candidate",
                "support_refs": [item["ref"] for item in public_events], "evidence": public_events,
                "observation": {"snapshot": "conversation-generation@" + _sha(run.key.active_generation_id)},
                "observed_at": created, "valid_from": created, "valid_to": _VALID_TO,
                "freshness": {"event_authority": {"status": "current", "generation_id": run.key.active_generation_id}},
                "event_id": ids[0], "reflection_key": identity, "rule_version": _RULE_VERSION, "task_id": queue_candidate_id,
                "proof_digest": _sha(proof_json),
            }
            payload["candidate_checksum"] = _sha(_json(payload))
            payload_json = _json(payload)
        finally:
            source.close()
        return self._persist_candidate(run_id, queue_candidate_id, payload, payload_json, proof_json, created)

    def _persist_candidate(self, run_id, queue_candidate_id, payload, payload_json, proof_json, created):
        con = _connect(self.db_path, readonly=False)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(_DDL)
            existing = con.execute(f"SELECT payload_json, candidate_checksum FROM {_TABLE} WHERE candidate_id=?", (payload["candidate_id"],)).fetchone()
            if existing:
                if existing["candidate_checksum"] != payload["candidate_checksum"]:
                    raise ValueError("immutable candidate conflict")
                return json.loads(existing["payload_json"])
            con.execute(f"INSERT INTO {_TABLE} VALUES (?,?,?,?,?,?,?)", (payload["candidate_id"], run_id, queue_candidate_id, payload["candidate_checksum"], payload_json, proof_json, created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
            con.commit()
            return payload
        finally:
            con.close()

    def load_candidates(self, conversation_db: Path) -> dict[str, dict[str, Any]]:
        try:
            ledger = _connect(self.db_path, readonly=True)
            ledger.execute("BEGIN")
            table = ledger.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
            ).fetchone()
            if table is None:
                return {}
            source = _connect(Path(conversation_db), readonly=True)
            source.execute("BEGIN")
            rows = ledger.execute(f"SELECT * FROM {_TABLE} ORDER BY candidate_id").fetchall()
            result = {}
            for row in rows:
                payload = json.loads(row["payload_json"])
                if row["candidate_checksum"] != payload.get("candidate_checksum") or _sha(_json({**payload, "candidate_checksum": ""})) != payload.get("candidate_checksum"):
                    continue
                proof = json.loads(row["proof_json"])
                if _sha(_json(proof)) != payload.get("proof_digest"):
                    continue
                active = source.execute("SELECT generation_id FROM ce_generation_authority WHERE active=1").fetchone()
                if not active:
                    continue
                current_generation = active["generation_id"]
                ids = [item["event_id"] for item in proof["events"]]
                events = _event_rows(source, current_generation, ids)
                if len(events) != len(ids):
                    continue
                for item in proof["events"]:
                    source_row = events[item["event_id"]]
                    if source_row["kind"] not in (EventKind.USER_MESSAGE.value, EventKind.ASSISTANT_MESSAGE.value) or not source_row["content"] or not _kept_content(source, current_generation, item["event_id"]) or item["quote"] not in source_row["content"] or _sha(source_row["content"]) != item["content_checksum"]:
                        break
                else:
                    context = proof.get("context") or []
                    if context:
                        current = _event_rows(source, current_generation, [item["event_id"] for item in context])
                        if len(current) != len(context):
                            continue
                        if any(
                            row["kind"] != item["kind"] or
                            not row["content"] or
                            _sha(row["content"]) != item["content_checksum"] or
                            not _kept_content(source, current_generation, item["event_id"]) or
                            _context_signature(source, current_generation, item["event_id"]) != item["disposition_checksum"]
                            for item in context
                            for row in [current[item["event_id"]]]
                        ):
                            continue
                    result[payload["candidate_id"]] = payload
            return result
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        finally:
            try:
                ledger.close()
                source.close()
            except UnboundLocalError:
                pass


__all__ = ["ExtractedCandidateStore"]
