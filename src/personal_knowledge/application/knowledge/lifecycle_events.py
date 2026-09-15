"""Governed, append-only lifecycle manifests and event ledger.

No operation deletes a knowledge unit.  Applying a manifest requires a stored,
human-reviewed checksum and exact optimistic-lock versions for every unit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping
import uuid

from personal_knowledge.core.lifecycle import (  # noqa: F401
    LIFECYCLE_SCHEMA_SQL,
    ensure_lifecycle_schema,
    lifecycle_status,
)
from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.retrieval.evidence import EvidenceResolver
from personal_knowledge.application.serving.snapshots import get_active_snapshot


ALLOWED_ACTIONS = {"supersede", "conflict", "correct", "restore", "deprecate"}
ALLOWED_CORRECTION_FIELDS = {"question", "answer"}
MAX_ACTIONS = 50
_NON_HUMAN_RE = re.compile(r"agent|codex|gpt|claude|gemini|synthetic|auto", re.I)


class LifecycleError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reviewer_hash(reviewer_id: str) -> str:
    return hashlib.sha256(reviewer_id.encode("utf-8")).hexdigest()


def _validate_reviewer(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    reviewer = str(payload.get("reviewer_id") or "").strip()
    reviewed_at = str(payload.get("reviewed_at") or "").strip()
    reviewer_type = str(payload.get("reviewer_type") or "human").strip().lower()
    if reviewer_type not in {"human", "llm"} or len(reviewer) < 3:
        raise LifecycleError("reviewer identity invalid")
    if reviewer_type == "human" and _NON_HUMAN_RE.search(reviewer):
        raise LifecycleError("human reviewer_id cannot identify an agent or model")
    if reviewer_type == "llm" and not all(
        str(payload.get(key) or "").strip()
        for key in ("model_id", "review_run_id", "prompt_version")
    ):
        raise LifecycleError("llm review provenance incomplete")
    try:
        datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleError("reviewed_at must be ISO-8601") from exc
    return reviewer, reviewed_at, reviewer_type


def build_manifest(
    actions: Iterable[Mapping[str, Any]],
    *,
    source_snapshot_id: str,
    reviewer_id: str = "",
    reviewed_at: str = "",
    reviewer_type: str = "human",
    model_id: str = "",
    review_run_id: str = "",
    prompt_version: str = "",
    review_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(actions, 1):
        normalized.append(
            {
                "ordinal": ordinal,
                "unit_id": str(raw.get("unit_id") or raw.get("canonical_unit_id") or ""),
                "action": str(raw.get("action") or ""),
                "expected_version": int(raw.get("expected_version") or 0),
                "expected_lifecycle": str(raw.get("expected_lifecycle") or raw.get("lifecycle_before") or ""),
                "target_unit_id": str(raw.get("target_unit_id") or raw.get("supersedes_id") or ""),
                "reason": str(raw.get("reason") or ""),
                "evidence_refs": sorted({str(x) for x in raw.get("evidence_refs") or [] if str(x)}),
                "changes": dict(raw.get("changes") or {}),
                "decision": str(raw.get("decision") or "pending"),
            }
        )
    body = {
        "schema_version": "knowledge_lifecycle_manifest_v1",
        "source_snapshot_id": source_snapshot_id,
        "reviewer_id": reviewer_id,
        "reviewed_at": reviewed_at,
        "reviewer_type": reviewer_type,
        "model_id": model_id,
        "review_run_id": review_run_id,
        "prompt_version": prompt_version,
        "review_receipt": dict(review_receipt or {}),
        "actions": normalized,
    }
    digest = _checksum(body)
    return {**body, "manifest_id": f"klm_{digest[:24]}", "manifest_checksum": digest}


def validate_manifest(manifest: Mapping[str, Any], *, require_review: bool = True) -> None:
    provenance_keys = ("reviewer_type", "model_id", "review_run_id", "prompt_version")
    body_keys = ("schema_version", "source_snapshot_id", "reviewer_id", "reviewed_at")
    if any(key in manifest for key in provenance_keys):
        body_keys += provenance_keys
    body_keys += ("review_receipt", "actions")
    body = {key: manifest.get(key) for key in body_keys}
    if manifest.get("manifest_checksum") != _checksum(body):
        raise LifecycleError("manifest checksum mismatch")
    if manifest.get("manifest_id") != f"klm_{_checksum(body)[:24]}":
        raise LifecycleError("manifest id mismatch")
    actions = list(manifest.get("actions") or [])
    if not actions or len(actions) > MAX_ACTIONS:
        raise LifecycleError(f"manifest action count must be 1..{MAX_ACTIONS}")
    unit_ids: set[str] = set()
    for action in actions:
        unit_id = str(action.get("unit_id") or "")
        if not unit_id or unit_id in unit_ids:
            raise LifecycleError("manifest contains missing or duplicate unit_id")
        unit_ids.add(unit_id)
        if action.get("action") not in ALLOWED_ACTIONS:
            raise LifecycleError("unsupported lifecycle action")
        if int(action.get("expected_version") or 0) < 1 or not action.get("expected_lifecycle"):
            raise LifecycleError("expected version/lifecycle required")
        if not str(action.get("reason") or "").strip():
            raise LifecycleError("reason required")
        if not list(action.get("evidence_refs") or []):
            raise LifecycleError("eligible evidence refs required")
        if action.get("action") == "supersede" and not action.get("target_unit_id"):
            raise LifecycleError("supersede target required")
        if action.get("action") == "correct":
            changes = dict(action.get("changes") or {})
            if not changes or not set(changes).issubset(ALLOWED_CORRECTION_FIELDS):
                raise LifecycleError("correction changes invalid")
        if require_review and action.get("decision") != "approve":
            raise LifecycleError("every applied action requires approve decision")
    if require_review:
        _validate_reviewer(manifest)


def register_manifest(db_path: Path, manifest: Mapping[str, Any], *, write: bool = False) -> dict[str, Any]:
    validate_manifest(manifest, require_review=True)
    result = {"manifest_id": manifest["manifest_id"], "manifest_checksum": manifest["manifest_checksum"], "written": False}
    if not write:
        return result
    con = connect_rw(db_path)
    try:
        ensure_lifecycle_schema(con)
        con.execute("BEGIN IMMEDIATE")
        reviewer_hash = _reviewer_hash(str(manifest["reviewer_id"]))
        con.execute(
            "INSERT INTO knowledge_lifecycle_manifests VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (manifest["manifest_id"], _canonical(manifest), manifest["manifest_checksum"], "reviewed", reviewer_hash, manifest["reviewed_at"], None, manifest["source_snapshot_id"], _now()),
        )
        for action in manifest["actions"]:
            action_id = f"kla_{_checksum([manifest['manifest_id'], action])[:24]}"
            con.execute(
                "INSERT INTO knowledge_lifecycle_actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, manifest["manifest_id"], action["ordinal"], action["unit_id"], action["action"], action["expected_version"], action["expected_lifecycle"], action["target_unit_id"] or None, action["reason"], _canonical(action["evidence_refs"]), _canonical(action["changes"])),
            )
        con.commit()
        result["written"] = True
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def finalize_review(proposal_path: Path, review_path: Path, artifact: Path) -> dict[str, Any]:
    proposal = load_manifest(proposal_path)
    validate_manifest(proposal, require_review=False)
    review = load_manifest(review_path)
    if review.get("proposal_manifest_id") != proposal.get("manifest_id") or review.get("proposal_checksum") != proposal.get("manifest_checksum"):
        raise LifecycleError("review does not bind exact proposal")
    reviewer = str(review.get("reviewer_id") or "").strip()
    reviewed_at = str(review.get("reviewed_at") or "")
    _, _, reviewer_type = _validate_reviewer(review)
    decisions = {str(x.get("unit_id")): str(x.get("decision")) for x in review.get("decisions") or []}
    expected = {str(x["unit_id"]) for x in proposal["actions"]}
    if set(decisions) != expected or any(value not in {"approve", "reject"} for value in decisions.values()):
        raise LifecycleError("review decisions must cover every proposal exactly once")
    approved = [{**action, "decision": "approve"} for action in proposal["actions"] if decisions[action["unit_id"]] == "approve"]
    if reviewer_type == "llm":
        for item in review.get("decisions") or []:
            confidence = item.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
                raise LifecycleError("llm lifecycle decision confidence must be between 0 and 1")
    if not approved:
        receipt = {
            "schema_version": "knowledge_lifecycle_review_receipt_v1",
            "review_status": "no_actions_approved",
            "proposal_manifest_id": proposal["manifest_id"],
            "proposal_checksum": proposal["manifest_checksum"],
            "reviewer_id_hash": _reviewer_hash(reviewer),
            "reviewed_at": reviewed_at,
            "reviewer_type": reviewer_type,
            "model_id": review.get("model_id"),
            "review_run_id": review.get("review_run_id"),
            "prompt_version": review.get("prompt_version"),
            "rejected_unit_ids": sorted(expected),
            "review_checksum": _checksum(review),
        }
        receipt["receipt_checksum"] = _checksum(receipt)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt
    reviewed = build_manifest(
        approved,
        source_snapshot_id=str(proposal.get("source_snapshot_id") or ""),
        reviewer_id=reviewer,
        reviewed_at=reviewed_at,
        reviewer_type=str(review.get("reviewer_type") or "human"),
        model_id=str(review.get("model_id") or ""),
        review_run_id=str(review.get("review_run_id") or ""),
        prompt_version=str(review.get("prompt_version") or ""),
        review_receipt={
            "proposal_manifest_id": proposal["manifest_id"],
            "proposal_checksum": proposal["manifest_checksum"],
            "review_checksum": _checksum(review),
            "rejected_unit_ids": sorted(expected - {str(x["unit_id"]) for x in approved}),
        },
    )
    validate_manifest(reviewed, require_review=True)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return reviewed


def _default_evidence_validator(refs: list[str]) -> bool:
    # 自动识别 ref 类型（cm| 消息 / cu| 知识单元 / g| / turn）：
    # deprecate 等动作的证据可能不是消息本体（例如证据已随 canonical 重建丢失，
    # 只能引用 canonical unit 记录自身定位被审对象）。
    resolver = EvidenceResolver()
    return all(resolver.resolve(ref).get("status") == "ok" for ref in refs)


def apply_manifest(
    db_path: Path,
    manifest: Mapping[str, Any],
    *,
    actor_id: str,
    evidence_validator: Callable[[list[str]], bool] | None = None,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest, require_review=True)
    if len(actor_id.strip()) < 3:
        raise LifecycleError("actor_id required")
    evidence_validator = evidence_validator or _default_evidence_validator
    con = connect_rw(db_path)
    con.row_factory = sqlite3.Row
    try:
        ensure_lifecycle_schema(con)
        stored = con.execute(
            "SELECT status,manifest_checksum,reviewer_id_hash FROM knowledge_lifecycle_manifests WHERE manifest_id=?",
            (manifest["manifest_id"],),
        ).fetchone()
        if not stored or stored["status"] != "reviewed" or stored["manifest_checksum"] != manifest["manifest_checksum"]:
            raise LifecycleError("manifest is not registered reviewed authority")
        con.execute("BEGIN IMMEDIATE")
        events: list[str] = []
        for index, action in enumerate(manifest["actions"], 1):
            row = con.execute(
                "SELECT canonical_unit_id,lifecycle,version,supersedes_id,question,answer FROM canonical_knowledge_units WHERE canonical_unit_id=?",
                (action["unit_id"],),
            ).fetchone()
            if row is None:
                raise LifecycleError("manifest unit missing")
            if int(row["version"]) != int(action["expected_version"]) or str(row["lifecycle"]) != str(action["expected_lifecycle"]):
                raise LifecycleError("manifest stale unit version or lifecycle")
            refs = list(action.get("evidence_refs") or [])
            if not evidence_validator(refs):
                raise LifecycleError("manifest evidence unresolved or ineligible")
            before_lifecycle = str(row["lifecycle"])
            after_lifecycle = before_lifecycle
            before_supersedes = row["supersedes_id"]
            after_supersedes = before_supersedes
            if action["action"] == "supersede":
                target = con.execute("SELECT 1 FROM canonical_knowledge_units WHERE canonical_unit_id=?", (action["target_unit_id"],)).fetchone()
                if target is None:
                    raise LifecycleError("supersede target missing")
                after_lifecycle, after_supersedes = "superseded", action["target_unit_id"]
            elif action["action"] == "conflict":
                after_lifecycle = "conflict"
            elif action["action"] == "deprecate":
                after_lifecycle, after_supersedes = "deprecated", None
            elif action["action"] == "restore":
                after_lifecycle, after_supersedes = "current", None
            changes = dict(action.get("changes") or {}) if action["action"] == "correct" else {}
            assignments = ["lifecycle=?", "supersedes_id=?", "version=version+1"]
            values: list[Any] = [after_lifecycle, after_supersedes]
            for field, value in changes.items():
                assignments.append(f"{field}=?")
                values.append(str(value))
            values.append(action["unit_id"])
            con.execute(f"UPDATE canonical_knowledge_units SET {','.join(assignments)} WHERE canonical_unit_id=?", values)
            action_row = con.execute("SELECT action_id FROM knowledge_lifecycle_actions WHERE manifest_id=? AND ordinal=?", (manifest["manifest_id"], action["ordinal"])).fetchone()
            event_id = f"kle_{uuid.uuid4().hex}"
            con.execute(
                "INSERT INTO knowledge_lifecycle_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, manifest["manifest_id"], action_row[0], action["unit_id"], action["action"], before_lifecycle, after_lifecycle, row["version"], int(row["version"]) + 1, before_supersedes, after_supersedes, action["reason"], stored["reviewer_id_hash"], actor_id, None, _now()),
            )
            for field, after_value in changes.items():
                before_value = row[field]
                con.execute(
                    "INSERT INTO knowledge_unit_corrections VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"kuc_{uuid.uuid4().hex}", event_id, action["unit_id"], field, _checksum(before_value), _checksum(after_value), _canonical(before_value), _canonical(after_value), _now()),
                )
            events.append(event_id)
            if inject_failure_after is not None and index >= inject_failure_after:
                raise RuntimeError("injected lifecycle apply failure")
        con.execute("UPDATE knowledge_lifecycle_manifests SET status='applied',actor_id=?,applied_at=? WHERE manifest_id=?", (actor_id, _now(), manifest["manifest_id"]))
        con.commit()
        return {"ok": True, "manifest_id": manifest["manifest_id"], "events": events, "applied": len(events)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def rollback_manifest(db_path: Path, manifest_id: str, *, actor_id: str) -> dict[str, Any]:
    """Reverse one applied manifest with linked append-only rollback events."""
    if len(actor_id.strip()) < 3:
        raise LifecycleError("actor_id required")
    con = connect_rw(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT status,reviewer_id_hash FROM knowledge_lifecycle_manifests WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if row is None or row["status"] != "applied":
            raise LifecycleError("only applied manifest can be rolled back")
        source_events = con.execute(
            "SELECT * FROM knowledge_lifecycle_events WHERE manifest_id=? AND rollback_of_event_id IS NULL ORDER BY created_at DESC,event_id DESC",
            (manifest_id,),
        ).fetchall()
        if not source_events:
            raise LifecycleError("manifest has no events")
        con.execute("BEGIN IMMEDIATE")
        rollback_events: list[str] = []
        for source in source_events:
            current = con.execute(
                "SELECT lifecycle,version,supersedes_id,question,answer FROM canonical_knowledge_units WHERE canonical_unit_id=?",
                (source["unit_id"],),
            ).fetchone()
            if current is None or int(current["version"]) != int(source["version_after"]):
                raise LifecycleError("rollback refused: unit advanced after manifest")
            corrections = con.execute(
                "SELECT field_name,before_value_json FROM knowledge_unit_corrections WHERE event_id=?",
                (source["event_id"],),
            ).fetchall()
            assignments = ["lifecycle=?", "supersedes_id=?", "version=version+1"]
            values: list[Any] = [source["lifecycle_before"], source["supersedes_before"]]
            for correction in corrections:
                assignments.append(f"{correction['field_name']}=?")
                values.append(json.loads(correction["before_value_json"]))
            values.append(source["unit_id"])
            con.execute(
                f"UPDATE canonical_knowledge_units SET {','.join(assignments)} WHERE canonical_unit_id=?",
                values,
            )
            event_id = f"kle_{uuid.uuid4().hex}"
            con.execute(
                "INSERT INTO knowledge_lifecycle_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, manifest_id, source["action_id"], source["unit_id"], "rollback", current["lifecycle"], source["lifecycle_before"], current["version"], int(current["version"]) + 1, current["supersedes_id"], source["supersedes_before"], f"rollback:{source['event_id']}", row["reviewer_id_hash"], actor_id, source["event_id"], _now()),
            )
            rollback_events.append(event_id)
        con.execute(
            "UPDATE knowledge_lifecycle_manifests SET status='rolled_back',actor_id=? WHERE manifest_id=?",
            (actor_id, manifest_id),
        )
        con.commit()
        return {"ok": True, "manifest_id": manifest_id, "rollback_events": rollback_events}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def event_history(db_path: Path, unit_id: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_lifecycle_events'").fetchone() is None:
            return []
        rows = con.execute(
            "SELECT event_id,manifest_id,event_type,lifecycle_before,lifecycle_after,version_before,version_after,supersedes_before,supersedes_after,reason,reviewer_id_hash,actor_id,rollback_of_event_id,created_at FROM knowledge_lifecycle_events WHERE unit_id=? ORDER BY created_at,CASE WHEN rollback_of_event_id IS NULL THEN 0 ELSE 1 END,event_id",
            (unit_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LifecycleError("manifest must be a JSON object")
    return value


def propose_reconcile_manifest(
    db_path: Path,
    *,
    subject: str | None = None,
    max_subjects: int | None = 20,
    artifact: Path | None = None,
) -> dict[str, Any]:
    """Convert heuristic reconcile output into a metadata-only review manifest."""
    from personal_knowledge.application.knowledge.reconcile_knowledge_lifecycle import (
        ACTION_MARK_CONFLICT,
        ACTION_MARK_SUPERSEDED,
        reconcile_knowledge_lifecycle,
    )

    report = reconcile_knowledge_lifecycle(
        db_path, subject=subject, max_subjects=max_subjects, write=False, dry_run=True
    )
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    proposed: list[dict[str, Any]] = []
    try:
        for action in report.actions:
            if action["action"] not in {ACTION_MARK_SUPERSEDED, ACTION_MARK_CONFLICT}:
                continue
            row = con.execute(
                "SELECT version,lifecycle FROM canonical_knowledge_units WHERE canonical_unit_id=?",
                (action["canonical_unit_id"],),
            ).fetchone()
            refs = [
                str(x[0])
                for x in con.execute(
                    "SELECT DISTINCT u.source_message_ref FROM knowledge_units u "
                    "JOIN canonical_unit_members m ON m.member_unit_id=u.unit_id "
                    "WHERE m.canonical_unit_id=? AND COALESCE(u.source_message_ref,'')<>'' ORDER BY u.source_message_ref",
                    (action["canonical_unit_id"],),
                ).fetchall()
            ]
            if row is None or not refs:
                continue
            proposed.append(
                {
                    "unit_id": action["canonical_unit_id"],
                    "action": "supersede" if action["action"] == ACTION_MARK_SUPERSEDED else "conflict",
                    "expected_version": int(row["version"]),
                    "expected_lifecycle": str(row["lifecycle"]),
                    "target_unit_id": action.get("supersedes_id") or "",
                    "reason": action["reason"],
                    "evidence_refs": refs,
                    "decision": "pending",
                }
            )
            if len(proposed) >= MAX_ACTIONS:
                break
    finally:
        con.close()
    if not proposed:
        raise LifecycleError("no evidence-backed lifecycle proposals in bounded cohort")
    snapshot = get_active_snapshot(db_path) or {}
    manifest = build_manifest(
        proposed,
        source_snapshot_id=str(snapshot.get("snapshot_id") or ""),
    )
    if artifact is not None:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        review_path = artifact.with_name(artifact.stem + ".review" + artifact.suffix)
        review_template = {
            "proposal_manifest_id": manifest["manifest_id"],
            "proposal_checksum": manifest["manifest_checksum"],
            "reviewer_id": "",
            "reviewed_at": "",
            "decisions": [
                {"unit_id": action["unit_id"], "decision": "pending", "reviewer_notes": ""}
                for action in manifest["actions"]
            ],
        }
        review_path.write_text(json.dumps(review_template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
