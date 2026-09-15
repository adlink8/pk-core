"""Durable, body-free manifest for view-policy candidate queues.

The manifest is deliberately separate from the estimate ledger.  It stores
the exact candidate mapping and blocked set used for a run, plus digests for
the queue and view build, so a replay can be compared without trusting a
coarse union-of-evidence digest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from personal_knowledge.application.conversation.extraction_policy import SchedulingOutput
from personal_knowledge.application.conversation.extraction_views import ViewBuildResult

MANIFEST_TABLES = (
    "ce_candidate_manifests",
    "ce_candidate_manifest_candidates",
    "ce_candidate_manifest_blocked",
)

MANIFEST_DDL = (
    """CREATE TABLE IF NOT EXISTS ce_candidate_manifests (
        run_id TEXT PRIMARY KEY, queue_digest TEXT NOT NULL,
        scheduled_digest TEXT NOT NULL, view_digest TEXT NOT NULL,
        manifest_digest TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES ce_candidate_runs(run_id))""",
    """CREATE TABLE IF NOT EXISTS ce_candidate_manifest_candidates (
        run_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
        derived_from_view TEXT NOT NULL, view_type TEXT NOT NULL,
        evidence_event_refs_json TEXT NOT NULL, rank INTEGER NOT NULL,
        position INTEGER NOT NULL, PRIMARY KEY (run_id, candidate_id),
        FOREIGN KEY (run_id) REFERENCES ce_candidate_manifests(run_id))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ce_candidate_manifest_position
        ON ce_candidate_manifest_candidates(run_id, position)""",
    """CREATE TABLE IF NOT EXISTS ce_candidate_manifest_blocked (
        run_id TEXT NOT NULL, view_id TEXT NOT NULL, view_type TEXT NOT NULL,
        reason TEXT NOT NULL, PRIMARY KEY (run_id, view_id, reason),
        FOREIGN KEY (run_id) REFERENCES ce_candidate_manifests(run_id))""",
)

class ManifestSchemaError(ValueError):
    """The manifest header exists but its child tables are incomplete."""

class ManifestIntegrityError(ValueError):
    """Persisted manifest content or digest is inconsistent."""

class ManifestMissingError(ValueError):
    """The requested run has no manifest header."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_manifest(scheduled: SchedulingOutput, view_result: ViewBuildResult) -> dict[str, object]:
    """Validate and serialize the public queue mapping without body content."""
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    views = {v.view_id: v for v in view_result.views}
    for candidate in scheduled.candidates:
        candidate_id = str(candidate.candidate_id)
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        seen.add(candidate_id)
        view = views.get(candidate.derived_from_view)
        if view is None:
            raise ValueError(f"candidate view not found: {candidate.derived_from_view}")
        if view.generation_id != view_result.generation_id or view.builder_version != view_result.builder_version:
            raise ValueError(f"candidate view identity mismatch: {candidate_id}")
        view_type = candidate.view_type.value if hasattr(candidate.view_type, "value") else str(candidate.view_type)
        actual_type = view.view_type.value if hasattr(view.view_type, "value") else str(view.view_type)
        if view_type != actual_type:
            raise ValueError(f"candidate view type mismatch: {candidate_id}")
        expected_refs = set(view.evidence_event_refs)
        actual_refs = set(candidate.evidence_event_refs)
        if expected_refs != actual_refs:
            raise ValueError(f"candidate evidence mapping mismatch: {candidate_id}")
        if candidate.policy_digest != scheduled.policy_digest:
            raise ValueError(f"candidate policy mismatch: {candidate_id}")
        candidates.append({
            "candidate_id": candidate_id,
            "derived_from_view": str(candidate.derived_from_view),
            "view_type": view_type,
            "evidence_event_refs": sorted(actual_refs),
            "rank": int(candidate.rank),
        })
    blocked = []
    for item in scheduled.blocked:
        view_type = item.view_type.value if hasattr(item.view_type, "value") else str(item.view_type)
        blocked.append({"view_id": str(item.view_id), "view_type": view_type, "reason": str(item.reason)})
    blocked.sort(key=lambda item: (item["view_id"], item["reason"], item["view_type"]))
    queue = {"policy_digest": str(scheduled.policy_digest), "candidates": candidates, "blocked": blocked}
    return {
        "candidates": candidates,
        "blocked": blocked,
        "queue_digest": digest(queue),
        "scheduled_digest": str(scheduled.digest),
        "view_digest": str(view_result.digest),
    }


def manifest_digest(manifest: dict[str, object]) -> str:
    return digest(manifest)


def manifest_payload(con: sqlite3.Connection, run_id: str) -> dict[str, object] | None:
    try:
        header = con.execute(
            "SELECT queue_digest, scheduled_digest, view_digest, manifest_digest "
            "FROM ce_candidate_manifests WHERE run_id=?",
            (run_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not header:
        return None
    try:
        candidates = [dict(r) for r in con.execute(
            "SELECT candidate_id, derived_from_view, view_type, evidence_event_refs_json, rank "
            "FROM ce_candidate_manifest_candidates WHERE run_id=? ORDER BY position", (run_id,)
        )]
        blocked = [dict(r) for r in con.execute(
            "SELECT view_id, view_type, reason FROM ce_candidate_manifest_blocked "
            "WHERE run_id=? ORDER BY view_id, reason, view_type", (run_id,)
        )]
    except sqlite3.OperationalError as exc:
        raise ManifestSchemaError("candidate manifest schema is incomplete") from exc
    for item in candidates:
        item["evidence_event_refs"] = json.loads(item.pop("evidence_event_refs_json"))
    payload = {"candidates": candidates, "blocked": blocked,
            "queue_digest": header["queue_digest"],
            "scheduled_digest": header["scheduled_digest"],
            "view_digest": header["view_digest"]}
    if manifest_digest(payload) != header["manifest_digest"]:
        raise ManifestIntegrityError("candidate manifest digest mismatch")
    return payload


def list_candidate_rows(con: sqlite3.Connection, run_id: str, *, limit: int, offset: int) -> list[dict]:
    try:
        has_manifest = con.execute(
            "SELECT 1 FROM ce_candidate_manifests WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        has_manifest = None
    if not has_manifest:
        raise ManifestMissingError("candidate run has no candidate manifest")
    try:
        rows = con.execute(
            "SELECT candidate_id, derived_from_view, view_type, evidence_event_refs_json, rank "
            "FROM ce_candidate_manifest_candidates WHERE run_id=? ORDER BY position LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ManifestSchemaError("candidate manifest schema is incomplete") from exc
    return [{"candidate_id": r["candidate_id"], "derived_from_view": r["derived_from_view"],
             "view_type": r["view_type"], "evidence_event_refs": json.loads(r["evidence_event_refs_json"]),
             "rank": r["rank"]} for r in rows]


def save_manifest(con: sqlite3.Connection, run_id: str, manifest: dict[str, object], now: str) -> None:
    con.execute(
        "INSERT INTO ce_candidate_manifests "
        "(run_id, queue_digest, scheduled_digest, view_digest, manifest_digest, created_at) VALUES (?,?,?,?,?,?)",
        (run_id, manifest["queue_digest"], manifest["scheduled_digest"], manifest["view_digest"],
         manifest_digest(manifest), now),
    )
    for position, candidate in enumerate(manifest["candidates"]):
        con.execute(
            "INSERT INTO ce_candidate_manifest_candidates "
            "(run_id, candidate_id, derived_from_view, view_type, evidence_event_refs_json, rank, position) VALUES (?,?,?,?,?,?,?)",
            (run_id, candidate["candidate_id"], candidate["derived_from_view"], candidate["view_type"],
             canonical_json(candidate["evidence_event_refs"]), candidate["rank"], position),
        )
    for blocked in manifest["blocked"]:
        con.execute(
            "INSERT INTO ce_candidate_manifest_blocked (run_id, view_id, view_type, reason) VALUES (?,?,?,?)",
            (run_id, blocked["view_id"], blocked["view_type"], blocked["reason"]),
        )


__all__ = ["MANIFEST_DDL", "MANIFEST_TABLES", "ManifestIntegrityError", "ManifestSchemaError", "ManifestMissingError",
           "build_manifest", "canonical_json", "digest", "list_candidate_rows", "manifest_digest",
           "manifest_payload", "save_manifest"]
