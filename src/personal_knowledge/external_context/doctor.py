"""Read-only fail-closed Doctor for the independent External Context authority."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, UNIFIED_DB
from .binding import (
    DecisionContextBinding,
    DecisionContextBindingError,
    create_decision_context_binding,
    validate_decision_context_binding,
)

from .ingest import BODY_KEYS as INGEST_BODY_KEYS
from .migrate import ALL_TABLES
from .registry import BODY_KEYS as REGISTRY_BODY_KEYS, DEFAULT_REGISTRY, source_definitions
from .schema import canonical_json, checksum
from .service import ExternalContextServiceError, validate_active_snapshot_policy
from .snapshots import ExternalSnapshotError, get_active_snapshot


DOCTOR_SCHEMA_VERSION = "external_context_doctor_v1"
PERSONAL_ONLY_TABLES = frozenset({
    "serving_authority", "serving_snapshots", "serving_snapshot_members",
    "canonical_knowledge_units", "personal_state_runs", "decision_runs", "proactive_runs",
})
EXTERNAL_ONLY_TABLES = frozenset(ALL_TABLES)
BODY_KEYS = INGEST_BODY_KEYS | REGISTRY_BODY_KEYS


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _check(check_id: str, ok: bool, detail: Mapping[str, Any], *, error: str = "") -> dict[str, Any]:
    return {
        "check_id": check_id, "critical": True, "ok": bool(ok),
        "status": "PASS" if ok else "FAIL", "error": error, "detail": dict(detail),
    }


def _tables(con: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _walk_body_paths(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in BODY_KEYS:
                found.append(child_path)
            found.extend(_walk_body_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_body_paths(child, f"{path}[{index}]"))
    return found


def _json_columns(con: sqlite3.Connection) -> Iterable[tuple[str, str, str]]:
    specs = {
        "external_source_registry": ("source_id", "definition_json"),
        "external_import_runs": ("run_id", "input_manifest_json"),
        "external_observations": ("observation_id", "value_json"),
        "external_facts": ("fact_id", "value_json"),
        "external_lifecycle_events": ("event_id", "payload_json"),
        "external_snapshots": ("snapshot_id", "manifest_json"),
        "external_snapshot_events": ("event_id", "payload_json"),
    }
    existing = _tables(con)
    for table, (id_column, json_column) in specs.items():
        if table not in existing:
            continue
        for row in con.execute(f"SELECT {id_column},{json_column} FROM {table}"):
            yield table, str(row[0]), str(row[1])


def _snapshot_chain(con: sqlite3.Connection) -> tuple[bool, dict[str, Any], str]:
    rows = con.execute("SELECT * FROM external_snapshot_events ORDER BY sequence").fetchall()
    previous = "GENESIS"
    for expected, row in enumerate(rows, start=1):
        if int(row["sequence"]) != expected or str(row["previous_event_checksum"]) != previous:
            return False, {"event_count": len(rows), "failed_sequence": expected}, "snapshot_event_chain_broken"
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return False, {"event_count": len(rows), "failed_sequence": expected}, "snapshot_event_payload_invalid"
        core = {
            "sequence": expected, "event_type": str(row["event_type"]),
            "snapshot_id": row["snapshot_id"], "snapshot_hash": row["snapshot_hash"],
            "previous_snapshot_id": row["previous_snapshot_id"],
            "previous_event_checksum": previous, "payload": payload,
            "occurred_at": str(row["occurred_at"]),
        }
        if checksum(core) != str(row["event_checksum"]):
            return False, {"event_count": len(rows), "failed_sequence": expected}, "snapshot_event_checksum_mismatch"
        previous = str(row["event_checksum"])
    authority = con.execute(
        "SELECT * FROM external_snapshot_authority ORDER BY authority_sequence"
    ).fetchall()
    previous_snapshot: str | None = None
    for expected, row in enumerate(authority, start=1):
        if int(row["authority_sequence"]) != expected or row["previous_snapshot_id"] != previous_snapshot:
            return False, {"event_count": len(rows), "authority_count": len(authority)}, "authority_chain_broken"
        event = con.execute(
            "SELECT event_type,snapshot_id,snapshot_hash FROM external_snapshot_events WHERE event_id=?",
            (row["activation_event_id"],),
        ).fetchone()
        expected_event = "activated" if str(row["action"]) == "activate" else str(row["action"])
        if (event is None or str(event["event_type"]) != expected_event
                or str(event["snapshot_id"]) != str(row["snapshot_id"])
                or str(event["snapshot_hash"]) != str(row["snapshot_hash"])):
            return False, {"event_count": len(rows), "authority_count": len(authority)}, "authority_event_mismatch"
        previous_snapshot = str(row["snapshot_id"])
    return True, {"event_count": len(rows), "authority_count": len(authority), "head_checksum": previous}, ""


def _active_payload_errors(con: sqlite3.Connection, active: Mapping[str, Any]) -> list[str]:
    """Recompute canonical payloads beneath the active manifest, not only stored hashes."""
    errors: list[str] = []
    for member in active["manifest"]["members"]:
        fact_id = str(member["fact_id"])
        fact = con.execute("SELECT * FROM external_facts WHERE fact_id=?", (fact_id,)).fetchone()
        if fact is None:
            errors.append(f"fact_missing:{fact_id}")
            continue
        try:
            fact_value = json.loads(str(fact["value_json"]))
        except json.JSONDecodeError:
            errors.append(f"fact_json_invalid:{fact_id}")
            continue
        fact_payload = {
            "run_id": str(fact["run_id"]), "subject": str(fact["subject"]),
            "predicate": str(fact["predicate"]), "value": fact_value,
            "valid_from": str(fact["valid_from"]),
            "valid_to": str(fact["valid_to"]) if fact["valid_to"] else None,
            "region": str(fact["region"]), "source_quality": float(fact["source_quality"]),
            "fact_confidence": float(fact["fact_confidence"]),
        }
        if checksum(fact_payload) != str(fact["payload_checksum"]):
            errors.append(f"fact_payload_checksum:{fact_id}")
        run = con.execute("SELECT * FROM external_import_runs WHERE run_id=?", (fact["run_id"],)).fetchone()
        if run is None:
            errors.append(f"run_missing:{fact['run_id']}")
        else:
            try:
                run_manifest = json.loads(str(run["input_manifest_json"]))
            except json.JSONDecodeError:
                errors.append(f"run_manifest_invalid:{run['run_id']}")
            else:
                if checksum(run_manifest) != str(run["input_manifest_checksum"]):
                    errors.append(f"run_manifest_checksum:{run['run_id']}")
        supports = con.execute(
            "SELECT s.*,o.* FROM external_fact_support s JOIN external_observations o "
            "ON o.observation_id=s.observation_id WHERE s.fact_id=?", (fact_id,),
        ).fetchall()
        if not supports:
            errors.append(f"support_missing:{fact_id}")
        for support in supports:
            support_id = str(support["support_id"])
            if checksum({"fact_id": fact_id, "observation_id": str(support["observation_id"])}) != str(support["support_checksum"]):
                errors.append(f"support_checksum:{support_id}")
            try:
                observation_value = json.loads(str(support["value_json"]))
            except json.JSONDecodeError:
                errors.append(f"observation_json_invalid:{support['observation_id']}")
                continue
            observation_payload = {
                "run_id": str(support["run_id"]), "source_id": str(support["source_id"]),
                "kind": str(support["observation_kind"]), "value": observation_value,
                "publication_time": str(support["publication_time"]),
                "valid_from": str(support["valid_from"]),
                "valid_to": str(support["valid_to"]) if support["valid_to"] else None,
                "observed_at": str(support["observed_at"]), "ingested_at": str(support["ingested_at"]),
                "region": str(support["region"]),
            }
            if checksum(observation_payload) != str(support["payload_checksum"]):
                errors.append(f"observation_payload_checksum:{support['observation_id']}")
    return errors


def doctor_external_context(
    external_db_path: Path | str = EXTERNAL_CONTEXT_DB,
    personal_db_path: Path | str = UNIFIED_DB,
    *,
    registry_path: Path | str = DEFAULT_REGISTRY,
    region: str = "global",
    now: str | None = None,
    max_age_seconds: int = 86400,
    binding: DecisionContextBinding | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run all checks via mode=ro/query_only and prove both DB files stayed unchanged."""
    external_path, personal_path, registry = Path(external_db_path), Path(personal_db_path), Path(registry_path)
    checked_at = now or _now()
    before = {"external": _fingerprint(external_path), "personal": _fingerprint(personal_path)}
    checks: list[dict[str, Any]] = []

    try:
        definitions = source_definitions(registry)
        con = _ro(external_path)
        try:
            rows = con.execute(
                "SELECT source_id,definition_json,definition_checksum FROM external_source_registry ORDER BY source_id"
            ).fetchall()
            expected = sorted((item.source_id, canonical_json(asdict(item)), item.definition_checksum) for item in definitions)
            actual = [(str(row[0]), str(row[1]), str(row[2])) for row in rows]
        finally:
            con.close()
        checks.append(_check("registry_projection", actual == expected,
                             {"expected_sources": len(expected), "projected_sources": len(actual)},
                             error="registry_drift" if actual != expected else ""))
    except Exception as exc:
        checks.append(_check("registry_projection", False, {}, error=f"registry_invalid:{exc}"))

    try:
        con = _ro(external_path)
        try:
            integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
            missing = sorted(EXTERNAL_ONLY_TABLES - _tables(con))
        finally:
            con.close()
        ok = integrity == "ok" and not fk and not missing
        checks.append(_check("sqlite_integrity", ok,
                             {"integrity": integrity, "foreign_key_violations": len(fk), "missing_tables": missing},
                             error="sqlite_integrity_failed" if not ok else ""))
    except Exception as exc:
        checks.append(_check("sqlite_integrity", False, {}, error=f"sqlite_unreadable:{exc}"))

    active: dict[str, Any] | None = None
    try:
        active = get_active_snapshot(external_path)
        if active is None:
            raise ExternalSnapshotError("external_authority_missing")
        con = _ro(external_path)
        try:
            payload_errors = _active_payload_errors(con, active)
        finally:
            con.close()
        checks.append(_check("active_manifest", not payload_errors, {
            "snapshot_id": active["snapshot_id"], "snapshot_hash": active["snapshot_hash"],
            "member_count": len(active["manifest"]["members"]), "payload_errors": payload_errors,
        }, error="active_payload_drift" if payload_errors else ""))
    except Exception as exc:
        checks.append(_check("active_manifest", False, {}, error=f"active_manifest_invalid:{exc}"))

    try:
        con = _ro(external_path)
        try:
            chain_ok, detail, error = _snapshot_chain(con)
        finally:
            con.close()
        checks.append(_check("snapshot_event_chain", chain_ok, detail, error=error))
    except Exception as exc:
        checks.append(_check("snapshot_event_chain", False, {}, error=f"snapshot_event_chain_invalid:{exc}"))

    try:
        if active is None:
            raise ExternalContextServiceError("external_authority_missing")
        policy = validate_active_snapshot_policy(
            external_path, snapshot_id=str(active["snapshot_id"]), snapshot_hash=str(active["snapshot_hash"]),
            region=region, now=checked_at, max_age_seconds=max_age_seconds, conflict_policy="reject",
        )
        source_ids = {str(item["source_id"]) for item in active["manifest"]["watermarks"]}
        expected_ids = {item.source_id for item in source_definitions(registry)}
        parity = source_ids == expected_ids
        checks.append(_check("watermarks_freshness", parity, {
            **policy, "source_ids": sorted(source_ids), "expected_source_ids": sorted(expected_ids),
        }, error="watermark_source_parity" if not parity else ""))
        checks.append(_check("conflict_state", True, {"unresolved_conflicts": 0}))
    except ExternalContextServiceError as exc:
        checks.append(_check("watermarks_freshness", False, {}, error=f"{exc.code}:{exc.detail}"))
        checks.append(_check("conflict_state", exc.code != "external_conflict_unresolved", {},
                             error=exc.code if exc.code == "external_conflict_unresolved" else ""))
    except Exception as exc:
        checks.append(_check("watermarks_freshness", False, {}, error=f"watermark_invalid:{exc}"))
        checks.append(_check("conflict_state", False, {}, error="conflict_state_unverified"))

    try:
        con = _ro(external_path)
        leaks: list[str] = []
        try:
            for table, record_id, raw in _json_columns(con):
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    leaks.append(f"{table}:{record_id}:invalid_json")
                    continue
                leaks.extend(f"{table}:{record_id}:{item}" for item in _walk_body_paths(value))
        finally:
            con.close()
        checks.append(_check("body_leakage", not leaks, {"leak_count": len(leaks), "locations": leaks[:20]},
                             error="body_leakage_detected" if leaks else ""))
    except Exception as exc:
        checks.append(_check("body_leakage", False, {}, error=f"body_scan_failed:{exc}"))

    try:
        if external_path.resolve() == personal_path.resolve():
            raise ValueError("database_paths_identical")
        external_con, personal_con = _ro(external_path), _ro(personal_path)
        try:
            external_tables, personal_tables = _tables(external_con), _tables(personal_con)
        finally:
            external_con.close(); personal_con.close()
        wrong_external = sorted(PERSONAL_ONLY_TABLES & external_tables)
        wrong_personal = sorted(EXTERNAL_ONLY_TABLES & personal_tables)
        separated = not wrong_external and not wrong_personal
        checks.append(_check("authority_separation", separated, {
            "external_db": str(external_path), "personal_db": str(personal_path),
            "personal_tables_in_external": wrong_external, "external_tables_in_personal": wrong_personal,
        }, error="authority_conflation" if not separated else ""))
    except Exception as exc:
        checks.append(_check("authority_separation", False, {}, error=f"authority_separation_invalid:{exc}"))

    try:
        candidate = binding or create_decision_context_binding(
            personal_path, external_path, region=region,
            max_external_age_seconds=max_age_seconds, now=checked_at,
        )
        parity = validate_decision_context_binding(candidate, personal_path, external_path, now=checked_at)
        checks.append(_check("dual_binding_parity", True, {
            "personal_snapshot_id": parity["personal"]["snapshot_id"],
            "external_snapshot_id": parity["external"]["snapshot_id"],
            "binding_hash": parity["binding"]["binding_hash"],
        }))
    except DecisionContextBindingError as exc:
        checks.append(_check("dual_binding_parity", False, {}, error=f"{exc.code}:{exc.detail}"))
    except Exception as exc:
        checks.append(_check("dual_binding_parity", False, {}, error=f"dual_binding_invalid:{exc}"))

    after = {"external": _fingerprint(external_path), "personal": _fingerprint(personal_path)}
    unchanged = before == after
    checks.append(_check("read_only_execution", unchanged,
                         {"external_unchanged": before["external"] == after["external"],
                          "personal_unchanged": before["personal"] == after["personal"]},
                         error="doctor_mutated_authority" if not unchanged else ""))
    failed = [item for item in checks if not item["ok"]]
    return {
        "schema_version": DOCTOR_SCHEMA_VERSION, "ok": not failed,
        "status": "PASS" if not failed else "FAIL", "checked_at": checked_at,
        "critical_total": len(checks), "critical_pass": len(checks) - len(failed),
        "critical_fail": len(failed), "checks": checks,
        "privacy": {"metadata_only": True, "raw_bodies": 0, "personal_writes": 0,
                    "external_writes": 0},
    }


__all__ = ["DOCTOR_SCHEMA_VERSION", "doctor_external_context"]
