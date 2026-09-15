"""Prepare, validate, activate and roll back immutable serving snapshots."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
import uuid

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw
from personal_knowledge.governance.artifact_registry import load_registry, validate_registry


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _event(con: sqlite3.Connection, action: str, snapshot_id: str | None, detail: dict, previous: str | None = None) -> str:
    event_id = f"se_{uuid.uuid4().hex}"
    con.execute(
        "INSERT INTO serving_snapshot_events VALUES (?,?,?,?,?,?)",
        (event_id, snapshot_id, action, previous, canonical_json(detail), _now()),
    )
    return event_id


def sync_registry_entries(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    doc = load_registry()
    issues = validate_registry(doc)
    if issues:
        raise ValueError(f"artifact registry invalid: {[x.code for x in issues]}")
    result: dict[str, dict[str, Any]] = {}
    for row in doc["artifacts"]:
        digest = hashlib.sha256(canonical_json(row).encode()).hexdigest()
        existing = con.execute(
            "SELECT layer, authority_role, privacy_class, definition_hash FROM artifact_registry_entries WHERE registry_id=?",
            (row["id"],),
        ).fetchone()
        values = (row["layer"], row["authority_role"], row["privacy"], digest)
        if existing and tuple(existing[:3]) != values[:3]:
            raise RuntimeError(f"runtime registry authority drift for {row['id']}")
        if existing and str(existing[3]) != digest:
            con.execute(
                "UPDATE artifact_registry_entries SET definition_hash=?, registered_at=? WHERE registry_id=?",
                (digest, _now(), row["id"]),
            )
        con.execute(
            "INSERT OR IGNORE INTO artifact_registry_entries VALUES (?,?,?,?,?,?)",
            (row["id"], *values, _now()),
        )
        result[row["authority_role"]] = row
    return result


def prepare_snapshot(
    db_path: Path,
    members: Mapping[str, Mapping[str, Any]],
    *,
    eval_gate_ref: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    definitions = {
        str(item["authority_role"]): str(item["id"])
        for item in load_registry().get("artifacts") or []
    }
    materialized: dict[str, dict[str, Any]] = {}
    for role, raw_member in sorted(members.items()):
        member = dict(raw_member)
        registry_id = definitions.get(role)
        version = str(member.get("version") or "")
        checksum = str(member.get("checksum") or "")
        if not member.get("artifact_version_id") and registry_id and version and checksum:
            member["artifact_version_id"] = _id(
                "av", f"{registry_id}|{version}|{checksum}"
            )
        materialized[role] = dict(sorted(member.items()))
    ordered = materialized
    manifest = {"schema_version": 1, "members": ordered, "eval_gate_ref": eval_gate_ref}
    digest = manifest_hash(manifest)
    snapshot_id = _id("ss", digest)
    result = {"snapshot_id": snapshot_id, "manifest_hash": digest, "manifest": manifest, "status": "draft", "written": False}
    if not write:
        return result
    con = connect_rw(db_path, timeout=60)
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        roles = sync_registry_entries(con)
        existing_snapshot = con.execute(
            "SELECT manifest_hash,status FROM serving_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if existing_snapshot is not None:
            if str(existing_snapshot[0]) != digest:
                raise RuntimeError(f"snapshot id collision: {snapshot_id}")
            con.commit()
            result.update({"status": str(existing_snapshot[1]), "written": False, "existing": True})
            return result
        con.execute(
            "INSERT OR IGNORE INTO serving_snapshots VALUES (?,?,?,?,?,?,NULL)",
            (snapshot_id, canonical_json(manifest), digest, "draft", eval_gate_ref, _now()),
        )
        for role, member in ordered.items():
            definition = roles.get(role)
            if definition is None:
                raise ValueError(f"unknown serving role: {role}")
            version = str(member.get("version") or "")
            checksum = str(member.get("checksum") or "")
            location_ref = str(member.get("location_ref") or "")
            if not version or not checksum or not location_ref:
                raise ValueError(f"incomplete member metadata: {role}")
            avid = str(member.get("artifact_version_id") or _id("av", f"{definition['id']}|{version}|{checksum}"))
            con.execute(
                "INSERT OR IGNORE INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (avid, definition["id"], version, checksum, str(member.get("location_kind") or definition["kind"]), location_ref, "validated", definition["privacy"], member.get("producer_run_id"), member.get("evidence_version_id"), canonical_json(member.get("metadata") or {}), _now()),
            )
            con.execute(
                "INSERT OR IGNORE INTO serving_snapshot_members VALUES (?,?,?,?)",
                (snapshot_id, role, avid, member.get("watermark_id")),
            )
        _event(con, "prepare", snapshot_id, {"manifest_hash": digest, "roles": sorted(ordered)})
        con.commit()
        result["written"] = True
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _snapshot_rows(con: sqlite3.Connection, snapshot_id: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    con.row_factory = sqlite3.Row
    snap = con.execute("SELECT * FROM serving_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    rows = con.execute(
        "SELECT m.serving_role, m.watermark_id, v.* FROM serving_snapshot_members m JOIN artifact_versions v ON v.artifact_version_id=m.artifact_version_id WHERE m.snapshot_id=? ORDER BY m.serving_role",
        (snapshot_id,),
    ).fetchall()
    return snap, rows


def validate_snapshot(
    db_path: Path,
    snapshot_id: str,
    *,
    collection_inspector: Callable[[str], Mapping[str, Any]] | None = None,
    required_roles: set[str] | None = None,
    require_gate: bool = False,
    gate_validator: Callable[[str], bool] | None = None,
    integrity_validator: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    con = connect_rw(db_path, timeout=60)
    try:
        assert_foreign_key_integrity(con)
        snap, members = _snapshot_rows(con, snapshot_id)
        errors: list[str] = []
        if snap is None:
            return {"ok": False, "snapshot_id": snapshot_id, "errors": ["snapshot_not_found"]}
        gate_ref = str(snap["eval_gate_ref"] or "")
        if require_gate and (not gate_ref or gate_validator is None or not gate_validator(gate_ref)):
            errors.append("eval_gate_not_passed")
        roles = {str(row["serving_role"]) for row in members}
        missing = sorted((required_roles or set()) - roles)
        if missing:
            errors.append(f"missing_roles:{','.join(missing)}")
        try:
            manifest = json.loads(str(snap["manifest_json"] or "{}"))
            declared_members = manifest.get("members") or {}
        except (json.JSONDecodeError, TypeError):
            declared_members = {}
            errors.append("manifest_json_invalid")
        if set(declared_members) != roles:
            errors.append("manifest_member_roles")
        for row in members:
            role = str(row["serving_role"])
            declared = declared_members.get(role) or {}
            for key in (
                "artifact_version_id", "version", "checksum", "location_kind",
                "location_ref", "watermark_id",
            ):
                if str(declared.get(key) or "") != str(row[key] or ""):
                    errors.append(f"manifest_member_mismatch:{role}:{key}")
            if row["location_kind"] == "chroma_collection":
                if collection_inspector is None:
                    errors.append(f"collection_unverified:{row['serving_role']}")
                    continue
                actual = collection_inspector(str(row["location_ref"]))
                if not actual.get("exists"):
                    errors.append(f"collection_missing:{row['serving_role']}")
                if str(actual.get("checksum") or "") != str(row["checksum"]):
                    errors.append(f"collection_checksum:{row['serving_role']}")
                expected_count = json.loads(row["metadata_json"] or "{}").get("unit_count")
                if expected_count is not None and int(actual.get("count", -1)) != int(expected_count):
                    errors.append(f"collection_count:{row['serving_role']}")
            if row["watermark_id"]:
                current = con.execute(
                    "SELECT w.recorded_at FROM serving_authority a "
                    "JOIN serving_snapshot_members am ON am.snapshot_id=a.active_snapshot_id AND am.serving_role=? "
                    "JOIN source_watermarks w ON w.watermark_id=am.watermark_id WHERE a.singleton_id=1",
                    (row["serving_role"],),
                ).fetchone()
                candidate = con.execute(
                    "SELECT recorded_at FROM source_watermarks WHERE watermark_id=?",
                    (row["watermark_id"],),
                ).fetchone()
                if current and candidate and str(candidate[0]) < str(current[0]):
                    errors.append(f"watermark_regression:{row['serving_role']}")
        if integrity_validator is not None:
            integrity = integrity_validator(snapshot_id)
            if not integrity.get("ok"):
                errors.extend(str(x) for x in (integrity.get("errors") or ["evidence_integrity_failed"]))
        con.execute("BEGIN IMMEDIATE")
        if errors:
            _event(con, "refuse", snapshot_id, {"errors": errors})
        else:
            con.execute("UPDATE serving_snapshots SET status='validated', validated_at=? WHERE snapshot_id=? AND status='draft'", (_now(), snapshot_id))
            _event(con, "validate", snapshot_id, {"roles": sorted(roles)})
        con.commit()
        return {"ok": not errors, "snapshot_id": snapshot_id, "errors": errors, "roles": sorted(roles)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_active_snapshot(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='serving_authority'").fetchone() is None:
            return None
        row = con.execute(
            "SELECT s.* FROM serving_authority a JOIN serving_snapshots s ON s.snapshot_id=a.active_snapshot_id WHERE a.singleton_id=1"
        ).fetchone()
        if row is None:
            return None
        _, members = _snapshot_rows(con, str(row["snapshot_id"]))
        return {**dict(row), "members": {m["serving_role"]: dict(m) for m in members}}
    finally:
        con.close()


def get_snapshot(db_path: Path, snapshot_id: str) -> dict[str, Any] | None:
    """Read any immutable snapshot without changing serving authority."""
    if not db_path.exists() or not snapshot_id:
        return None
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row, members = _snapshot_rows(con, snapshot_id)
        if row is None:
            return None
        return {**dict(row), "members": {m["serving_role"]: dict(m) for m in members}}
    finally:
        con.close()


def activate_snapshot(
    db_path: Path,
    snapshot_id: str,
    *,
    pointer_path: Path | None = None,
    pointer_role: str = "knowledge_retrieval",
    before_commit: Callable[[sqlite3.Connection], None] | None = None,
    inject_failure: str | None = None,
) -> dict[str, Any]:
    con = connect_rw(db_path, timeout=60)
    previous: str | None = None
    event_id = ""
    try:
        assert_foreign_key_integrity(con)
        snap, members = _snapshot_rows(con, snapshot_id)
        if snap is None or snap["status"] != "validated":
            return {"ok": False, "error": "snapshot_not_validated", "snapshot_id": snapshot_id}
        previous_row = con.execute("SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1").fetchone()
        previous = previous_row[0] if previous_row else None
        if inject_failure == "before_transaction":
            raise RuntimeError("injected before transaction")
        con.execute("BEGIN IMMEDIATE")
        if before_commit:
            before_commit(con)
        event_id = _event(con, "activate" if not previous else "activate", snapshot_id, {}, previous)
        con.execute(
            "UPDATE serving_authority SET active_snapshot_id=?, activated_at=?, activation_event_id=? WHERE singleton_id=1",
            (snapshot_id, _now(), event_id),
        )
        if inject_failure == "before_commit":
            raise RuntimeError("injected before commit")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    projection_ok = True
    projection_error = ""
    if pointer_path is not None:
        try:
            if inject_failure == "projection":
                raise OSError("injected projection failure")
            active = get_active_snapshot(db_path) or {}
            member = (active.get("members") or {}).get(pointer_role) or {}
            collection = str(member.get("location_ref") or "")
            if not collection:
                raise RuntimeError(f"snapshot has no {pointer_role} projection")
            pointer_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = pointer_path.with_suffix(pointer_path.suffix + ".tmp")
            tmp.write_text(collection, encoding="utf-8")
            tmp.replace(pointer_path)
            projection_ok = pointer_path.read_text(encoding="utf-8").strip() == collection
        except Exception as exc:  # authority remains the committed SQLite snapshot
            projection_ok = False
            projection_error = str(exc)
            drift = connect_rw(db_path)
            try:
                _event(drift, "projection_drift", snapshot_id, {"error": projection_error}, previous)
                drift.commit()
            finally:
                drift.close()
    return {"ok": True, "snapshot_id": snapshot_id, "previous_snapshot_id": previous, "event_id": event_id, "projection_ok": projection_ok, "projection_error": projection_error}


def rollback_snapshot(db_path: Path, snapshot_id: str, **kwargs: Any) -> dict[str, Any]:
    current = get_active_snapshot(db_path)
    result = activate_snapshot(db_path, snapshot_id, **kwargs)
    if result.get("ok"):
        con = connect_rw(db_path)
        try:
            _event(con, "rollback", snapshot_id, {}, (current or {}).get("snapshot_id"))
            con.commit()
        finally:
            con.close()
    return result


def repair_pointer_projection(
    db_path: Path,
    pointer_path: Path,
    *,
    pointer_role: str = "knowledge_retrieval",
    write: bool = False,
) -> dict[str, Any]:
    """Repair only the compatibility pointer from SQLite authority."""
    active = get_active_snapshot(db_path)
    if not active:
        return {"ok": False, "error": "no_active_snapshot", "written": False}
    member = (active.get("members") or {}).get(pointer_role) or {}
    expected = str(member.get("location_ref") or "")
    actual = pointer_path.read_text(encoding="utf-8").strip() if pointer_path.exists() else ""
    result = {"ok": bool(expected), "snapshot_id": active["snapshot_id"], "expected": expected, "actual": actual, "drift": actual != expected, "written": False}
    if not write or not expected or actual == expected:
        return result
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer_path.with_suffix(pointer_path.suffix + ".tmp")
    tmp.write_text(expected, encoding="utf-8")
    tmp.replace(pointer_path)
    con = connect_rw(db_path)
    try:
        _event(con, "projection_repair", str(active["snapshot_id"]), {"previous": actual, "expected": expected})
        con.commit()
    finally:
        con.close()
    result.update({"actual": expected, "drift": False, "written": True})
    return result


def bootstrap_current_snapshot(
    db_path: Path,
    *,
    eval_gate: Path,
    write: bool = False,
) -> dict[str, Any]:
    """Prepare a complete draft from latest published versions; never activates."""
    from personal_knowledge.application.serving.versions import file_checksum, json_checksum, publication_status, record_publication
    from personal_knowledge.core.project_paths import (
        AGENT_CONVERSATIONS_DB, AI_CONTEXT_DIR, GOOGLE_DB,
        KNOWLEDGE_ACTIVE_POINTER, ROOT,
    )

    retrieval_contract = ROOT / "src" / "personal_knowledge" / "retrieval" / "semantic_search.py"
    turn_summaries = AI_CONTEXT_DIR / "conversation_summaries.json"
    if not eval_gate.exists():
        return {"ok": False, "written": False, "error": "eval_gate_missing", "path": str(eval_gate)}
    if not _eval_gate_passes(str(eval_gate)):
        return {"ok": False, "written": False, "error": "eval_gate_not_passed", "path": str(eval_gate)}
    contract_checksum = file_checksum(retrieval_contract)
    eval_checksum = file_checksum(eval_gate)
    missing_proofs: list[str] = []
    specs: list[dict[str, Any]] = []
    if not AGENT_CONVERSATIONS_DB.exists():
        missing_proofs.append("canonical_conversation")
    else:
        conversation_checksum = file_checksum(AGENT_CONVERSATIONS_DB)
        specs.extend([
            {"role": "canonical_conversation", "registry_id": "d.canonical_conversation", "version": conversation_checksum, "checksum": conversation_checksum, "location_kind": "sqlite_store", "location_ref": str(AGENT_CONVERSATIONS_DB), "source_key": "agentsview", "watermark_value": conversation_checksum},
            {"role": "canonical_message", "registry_id": "d.canonical_message", "version": conversation_checksum, "checksum": conversation_checksum, "location_kind": "sqlite_view", "location_ref": f"{AGENT_CONVERSATIONS_DB}#canonical_messages", "source_key": "canonical_conversation", "watermark_value": conversation_checksum, "parent_role": "canonical_conversation"},
        ])
    if not turn_summaries.exists():
        missing_proofs.append("turn_summary")
    else:
        turn_checksum = file_checksum(turn_summaries)
        try:
            turn_actual = dict(_collection_inspector("conversation_turns"))
        except Exception as exc:  # noqa: BLE001
            turn_actual = {"exists": False, "error": str(exc)}
        if not turn_actual.get("exists") or not turn_actual.get("checksum"):
            missing_proofs.append("turn_retrieval")
        else:
            specs.extend([
                {"role": "turn_summary", "registry_id": "s.turn_summary", "version": turn_checksum, "checksum": turn_checksum, "location_kind": "json_artifact", "location_ref": str(turn_summaries), "source_key": "canonical_message", "watermark_value": turn_checksum, "parent_role": "canonical_message"},
                {"role": "turn_retrieval", "registry_id": "r.turn_vector", "version": str(turn_actual["checksum"]), "checksum": str(turn_actual["checksum"]), "location_kind": "chroma_collection", "location_ref": "conversation_turns", "source_key": "turn_summary", "watermark_value": turn_checksum, "parent_role": "turn_summary", "metadata": {"count": int(turn_actual.get("count", 0))}},
            ])
    if not GOOGLE_DB.exists():
        missing_proofs.extend(["google_normalized", "google_assertion"])
    else:
        con = sqlite3.connect(f"file:{GOOGLE_DB.resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            google_runs = {str(row["run_type"]): dict(row) for row in con.execute("SELECT run_id,run_type,input_hash,dataset_hash FROM google_structure_runs WHERE status='current'")}
        except sqlite3.Error:
            google_runs = {}
        con.close()
        normalized, assertion = google_runs.get("normalized_events"), google_runs.get("light_assertions")
        if not normalized:
            missing_proofs.append("google_normalized")
        else:
            specs.append({"role": "google_normalized", "registry_id": "d.google_normalized", "version": normalized["dataset_hash"], "checksum": normalized["dataset_hash"], "location_kind": "sqlite_store", "location_ref": f"{GOOGLE_DB}#normalized_events", "source_key": "google_activities", "watermark_value": normalized["input_hash"], "producer_run_id": normalized["run_id"]})
        if not assertion:
            missing_proofs.append("google_assertion")
        else:
            specs.append({"role": "google_assertion", "registry_id": "s.google_assertion", "version": assertion["dataset_hash"], "checksum": assertion["dataset_hash"], "location_kind": "sqlite_table", "location_ref": f"{GOOGLE_DB}#google_light_assertions", "source_key": "google_normalized", "watermark_value": assertion["input_hash"], "producer_run_id": assertion["run_id"], "parent_role": "google_normalized"})
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    index_row = con.execute("SELECT version_id,build_id,collection_name,canonical_build_id,unit_count,checksum FROM knowledge_index_versions WHERE status='active' ORDER BY activated_at DESC LIMIT 1").fetchone()
    canonical_ids = [str(row[0]) for row in con.execute("SELECT canonical_unit_id FROM canonical_knowledge_units WHERE status='current' ORDER BY canonical_unit_id")]
    con.close()
    if index_row is None or not canonical_ids:
        missing_proofs.extend(["canonical_knowledge", "knowledge_retrieval"])
    else:
        try:
            knowledge_actual = dict(_collection_inspector(str(index_row["collection_name"])))
        except Exception as exc:  # noqa: BLE001
            knowledge_actual = {"exists": False, "error": str(exc)}
        if not knowledge_actual.get("exists") or not knowledge_actual.get("checksum"):
            missing_proofs.append("knowledge_retrieval")
        elif index_row["checksum"] and str(index_row["checksum"]) != str(knowledge_actual["checksum"]):
            missing_proofs.append("knowledge_retrieval_checksum")
        else:
            canonical_build = str(index_row["canonical_build_id"] or index_row["build_id"])
            canonical_checksum = json_checksum(canonical_ids)
            specs.extend([
                {"role": "canonical_knowledge", "registry_id": "s.knowledge_unit", "version": canonical_build, "checksum": canonical_checksum, "location_kind": "sqlite_table", "location_ref": "canonical_knowledge_units", "source_key": "canonical_message", "watermark_value": canonical_build, "producer_run_id": canonical_build, "parent_role": "canonical_message", "metadata": {"unit_count": len(canonical_ids)}},
                {"role": "knowledge_retrieval", "registry_id": "r.knowledge_index", "version": str(index_row["version_id"]), "checksum": str(knowledge_actual["checksum"]), "location_kind": "chroma_collection", "location_ref": str(index_row["collection_name"]), "source_key": "canonical_knowledge", "watermark_value": canonical_build, "producer_run_id": str(index_row["build_id"]), "parent_role": "canonical_knowledge", "metadata": {"unit_count": int(knowledge_actual.get("count", index_row["unit_count"])), "canonical_build_id": canonical_build}},
            ])
    specs.extend([
        {"role": "product_retrieval", "registry_id": "r.layered_search", "version": contract_checksum, "checksum": contract_checksum, "location_kind": "service_contract", "location_ref": str(retrieval_contract), "source_key": "retrieval_contract", "watermark_value": contract_checksum, "parent_role": "knowledge_retrieval"},
        {"role": "knowledge_evaluation", "registry_id": "a.knowledge_evaluation", "version": eval_checksum, "checksum": eval_checksum, "location_kind": "evaluation_run", "location_ref": str(eval_gate), "source_key": "knowledge_evaluation", "watermark_value": eval_checksum, "parent_role": "knowledge_retrieval"},
    ])
    if missing_proofs:
        return {"ok": False, "written": False, "mode": "draft_only", "missing_proofs": sorted(set(missing_proofs)), "discovered_roles": [spec["role"] for spec in specs]}
    if not write:
        return {"ok": True, "written": False, "mode": "draft_only", "would_record": [spec["role"] for spec in specs], "required_roles": list(load_registry().get("required_serving_roles") or [])}
    if write:
        recorded_by_role: dict[str, dict[str, Any]] = {}
        for spec in specs:
            payload = {key: value for key, value in spec.items() if key not in {"role", "parent_role"}}
            parent = recorded_by_role.get(str(spec.get("parent_role") or ""))
            payload["evidence_version_id"] = parent.get("artifact_version_id") if parent else None
            recorded_by_role[spec["role"]] = record_publication(db_path, **payload)
    status = publication_status(db_path)
    artifacts = status.get("artifacts") or {}
    registry = load_registry()
    required = list(registry.get("required_serving_roles") or [])
    by_role = {str(item.get("authority_role")): item for item in artifacts.values() if item.get("artifact_version_id")}
    missing = sorted(role for role in required if role not in by_role)
    if missing:
        return {"ok": False, "written": False, "mode": "draft_only", "missing_proofs": missing, "would_record": [spec["role"] for spec in specs] if not write else []}
    members: dict[str, dict[str, Any]] = {}
    for role in required:
        row = by_role[role]
        members[role] = {
            "artifact_version_id": row["artifact_version_id"],
            "version": row["version"],
            "checksum": row["checksum"],
            "location_kind": row["location_kind"],
            "location_ref": row["location_ref"],
            "producer_run_id": row.get("producer_run_id"),
            "watermark_id": row.get("watermark_id"),
            "metadata": row.get("metadata") or {},
        }
    draft = prepare_snapshot(db_path, members, eval_gate_ref=str(eval_gate), write=write)
    return {"ok": True, "mode": "draft_only", "required_roles": required, **draft}


def _eval_gate_passes(reference: str) -> bool:
    path = Path(reference)
    if not path.exists():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    candidates = [doc, doc.get("gate") or {}, doc.get("result") or {}, doc.get("summary") or {}]
    return any(
        item.get("passed") is True or str(item.get("status") or item.get("verdict") or "").upper() == "PASS"
        for item in candidates if isinstance(item, dict)
    )


def _collection_inspector(name: str) -> Mapping[str, Any]:
    from personal_knowledge.application.knowledge.doctor_ku import _default_collection_inspector
    return _default_collection_inspector(name)


def _evidence_integrity(db_path: Path, snapshot_id: str) -> Mapping[str, Any]:
    from personal_knowledge.application.knowledge.doctor_ku import _check_evidence_probe
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB
    result = _check_evidence_probe(db_path, AGENT_CONVERSATIONS_DB)
    return {"ok": result.ok, "errors": [] if result.ok else ["evidence_integrity_failed"]}


def _cli() -> int:
    import argparse
    from personal_knowledge.core.project_paths import KNOWLEDGE_ACTIVE_POINTER, UNIFIED_DB

    parser = argparse.ArgumentParser(description="Composite serving snapshot operations")
    parser.add_argument("--db", type=Path, default=UNIFIED_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--pointer", type=Path, default=KNOWLEDGE_ACTIVE_POINTER)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--eval-gate", type=Path, required=True)
    bootstrap.add_argument("--write", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("--snapshot", required=True)
    activate = sub.add_parser("activate")
    activate.add_argument("--snapshot", required=True)
    activate.add_argument("--pointer", type=Path, default=KNOWLEDGE_ACTIVE_POINTER)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--snapshot", required=True)
    rollback.add_argument("--pointer", type=Path, default=KNOWLEDGE_ACTIVE_POINTER)
    repair = sub.add_parser("repair-pointer")
    repair.add_argument("--pointer", type=Path, default=KNOWLEDGE_ACTIVE_POINTER)
    repair.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.command == "status":
        active = get_active_snapshot(args.db)
        projected = args.pointer.read_text(encoding="utf-8").strip() if args.pointer.exists() else ""
        expected = str((((active or {}).get("members") or {}).get("knowledge_retrieval") or {}).get("location_ref") or "")
        result = {"ok": bool(active), "active": active, "pointer": projected, "pointer_drift": bool(active) and projected != expected}
    elif args.command == "bootstrap":
        result = bootstrap_current_snapshot(args.db, eval_gate=args.eval_gate, write=args.write)
    elif args.command == "validate":
        result = validate_snapshot(args.db, args.snapshot, collection_inspector=_collection_inspector, required_roles=set(load_registry().get("required_serving_roles") or []), require_gate=True, gate_validator=_eval_gate_passes, integrity_validator=lambda sid: _evidence_integrity(args.db, sid))
    elif args.command == "activate":
        result = activate_snapshot(args.db, args.snapshot, pointer_path=args.pointer)
    elif args.command == "rollback":
        result = rollback_snapshot(args.db, args.snapshot, pointer_path=args.pointer)
    else:
        result = repair_pointer_projection(args.db, args.pointer, write=args.write)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
