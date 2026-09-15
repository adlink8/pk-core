"""File-first bounded publication into the independent External Context authority.

This module accepts already-normalized in-memory/file payloads.  It deliberately
contains no HTTP client, crawler, or personal-authority integration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

from .lifecycle import append_lifecycle_event
from .migrate import inspect_schema
from .registry import DEFAULT_REGISTRY, source_definitions
from .schema import canonical_json, checksum, stable_id


IMPORT_SCHEMA_VERSION = "external_context_import_v1"
BODY_KEYS = frozenset({
    "body", "content", "raw", "raw_text", "full_text", "html", "markdown",
    "document", "article", "page", "text", "quote", "evidence_quote",
})
SECRET_KEYS = frozenset({
    "api_key", "access_token", "authorization", "cookie", "password", "secret", "token",
})
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|cookie|password|secret)\s*[:=]\s*\S+"
)
REQUIRED_MANIFEST = frozenset({
    "schema_version", "source_id", "source_definition_checksum",
    "quality_policy_version", "region", "observed_at", "ingested_at",
    "observations", "facts",
})
REQUIRED_OBSERVATION = frozenset({
    "key", "kind", "value", "publication_time", "valid_from", "valid_to", "region",
})
REQUIRED_FACT = frozenset({
    "key", "subject", "predicate", "value", "valid_from", "valid_to", "region",
    "source_quality", "fact_confidence", "observation_keys",
})


class ExternalIngestError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ExternalIngestError("invalid_time", field)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ExternalIngestError("invalid_time", field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ExternalIngestError("invalid_time", field)
    return parsed


def _walk(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            child_path = f"{path}.{key}" if path else key
            if key in BODY_KEYS:
                raise ExternalIngestError("body_like_field", child_path)
            if key in SECRET_KEYS:
                raise ExternalIngestError("secret_like_field", child_path)
            _walk(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if SECRET_VALUE_RE.search(value):
            raise ExternalIngestError("secret_like_value", path)
        if len(value) > 1024:
            raise ExternalIngestError("unbounded_value", path)
    elif not isinstance(value, (int, float, bool, type(None))):
        raise ExternalIngestError("unsupported_value", path)


def _exact_fields(value: Any, required: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalIngestError("invalid_record", label)
    fields = set(value)
    if fields != required:
        raise ExternalIngestError("invalid_fields", f"{label}:{sorted(fields ^ required)}")
    return value


def _validate_manifest(manifest: Mapping[str, Any], registry_path: Path) -> None:
    _exact_fields(manifest, REQUIRED_MANIFEST, "manifest")
    if len(canonical_json(manifest).encode("utf-8")) > 1_000_000:
        raise ExternalIngestError("manifest_too_large")
    _walk(manifest)
    if manifest["schema_version"] != IMPORT_SCHEMA_VERSION:
        raise ExternalIngestError("unsupported_manifest_version")
    definitions = {item.source_id: item for item in source_definitions(registry_path)}
    source_id = str(manifest["source_id"])
    source = definitions.get(source_id)
    if source is None:
        raise ExternalIngestError("source_not_allowlisted", source_id)
    if manifest["source_definition_checksum"] != source.definition_checksum:
        raise ExternalIngestError("source_definition_stale", source_id)
    if manifest["quality_policy_version"] != source.quality_policy_version:
        raise ExternalIngestError("quality_policy_version_mismatch", source_id)
    if manifest["region"] != source.region:
        raise ExternalIngestError("unsupported_region", str(manifest["region"]))
    observed = _utc(manifest["observed_at"], "observed_at")
    ingested = _utc(manifest["ingested_at"], "ingested_at")
    if observed > ingested:
        raise ExternalIngestError("invalid_time_order", "observed_at>ingested_at")
    observations = manifest["observations"]
    facts = manifest["facts"]
    if not isinstance(observations, list) or not 1 <= len(observations) <= 100:
        raise ExternalIngestError("invalid_observation_count")
    if not isinstance(facts, list) or not 1 <= len(facts) <= 100:
        raise ExternalIngestError("invalid_fact_count")
    observation_keys: set[str] = set()
    for index, raw in enumerate(observations):
        item = _exact_fields(raw, REQUIRED_OBSERVATION, f"observation[{index}]")
        key = str(item["key"])
        if not key or key in observation_keys:
            raise ExternalIngestError("duplicate_observation_key", key)
        observation_keys.add(key)
        if item["region"] != source.region:
            raise ExternalIngestError("unsupported_region", f"observation:{key}")
        publication = _utc(item["publication_time"], f"observation:{key}:publication_time")
        valid_from = _utc(item["valid_from"], f"observation:{key}:valid_from")
        valid_to = _utc(item["valid_to"], f"observation:{key}:valid_to") if item["valid_to"] else None
        if publication > observed or valid_from > observed or (valid_to and valid_to < valid_from):
            raise ExternalIngestError("invalid_time_order", f"observation:{key}")
    fact_keys: set[str] = set()
    for index, raw in enumerate(facts):
        item = _exact_fields(raw, REQUIRED_FACT, f"fact[{index}]")
        key = str(item["key"])
        if not key or key in fact_keys:
            raise ExternalIngestError("duplicate_fact_key", key)
        fact_keys.add(key)
        if not str(item["subject"]).strip() or not str(item["predicate"]).strip():
            raise ExternalIngestError("invalid_fact_identity", key)
        if item["region"] != source.region:
            raise ExternalIngestError("unsupported_region", f"fact:{key}")
        valid_from = _utc(item["valid_from"], f"fact:{key}:valid_from")
        valid_to = _utc(item["valid_to"], f"fact:{key}:valid_to") if item["valid_to"] else None
        if valid_from > observed or (valid_to and valid_to < valid_from):
            raise ExternalIngestError("invalid_time_order", f"fact:{key}")
        for score in ("source_quality", "fact_confidence"):
            value = item[score]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                raise ExternalIngestError("invalid_confidence", f"{key}:{score}")
        support = item["observation_keys"]
        if not isinstance(support, list) or not support or not set(support) <= observation_keys:
            raise ExternalIngestError("unresolved_provenance", key)


def load_bounded_manifest(path: Path) -> dict[str, Any]:
    """Load one local JSON manifest; no URL or implicit directory traversal."""
    file_path = Path(path)
    if file_path.suffix.lower() != ".json" or not file_path.is_file():
        raise ExternalIngestError("manifest_file_invalid", str(file_path))
    if file_path.stat().st_size > 1_000_000:
        raise ExternalIngestError("manifest_file_too_large")
    try:
        value = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalIngestError("manifest_file_unreadable", str(file_path)) from exc
    if not isinstance(value, dict):
        raise ExternalIngestError("invalid_manifest_root")
    return value


def authority_fingerprint(db_path: Path) -> str:
    con = sqlite3.connect(str(db_path))
    try:
        content: dict[str, Any] = {}
        for table in (
            "external_source_registry", "external_import_runs", "external_observations",
            "external_facts", "external_fact_support", "external_lifecycle_events",
        ):
            content[table] = con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        return checksum(content)
    finally:
        con.close()


def _overlaps(left_from: str, left_to: str | None, right_from: str, right_to: str | None) -> bool:
    high = "9999-12-31T23:59:59Z"
    return left_from <= (right_to or high) and right_from <= (left_to or high)


def publish_bounded_cohort(
    db_path: Path,
    manifest: Mapping[str, Any],
    *,
    input_manifest_checksum: str,
    registry_path: Path = DEFAULT_REGISTRY,
    fault_at: str | None = None,
) -> dict[str, Any]:
    """Validate and atomically publish a normalized cohort, or replay as no-op."""
    path = Path(db_path)
    registry = Path(registry_path)
    if inspect_schema(path, registry)["schema_state"] != "applied":
        raise ExternalIngestError("authority_not_ready")
    _validate_manifest(manifest, registry)
    digest = checksum(manifest)
    if input_manifest_checksum != digest:
        raise ExternalIngestError("manifest_checksum_mismatch")
    source_id = str(manifest["source_id"])
    run_id = stable_id("eir", {"source_id": source_id, "manifest_checksum": digest})
    con = connect_rw(path, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        assert_foreign_key_integrity(con)
        existing = con.execute(
            "SELECT run_id,input_manifest_json,status FROM external_import_runs "
            "WHERE source_id=? AND input_manifest_checksum=?", (source_id, digest),
        ).fetchone()
        if existing:
            if existing[1] != canonical_json(manifest) or existing[2] != "published":
                raise ExternalIngestError("published_manifest_tampered", str(existing[0]))
            con.rollback()
            return {"run_id": str(existing[0]), "published": False, "no_op": True}
        projected = con.execute(
            "SELECT definition_checksum FROM external_source_registry WHERE source_id=?", (source_id,),
        ).fetchone()
        if not projected or projected[0] != manifest["source_definition_checksum"]:
            raise ExternalIngestError("source_projection_stale", source_id)
        con.execute(
            "INSERT INTO external_import_runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, source_id, manifest["source_definition_checksum"], canonical_json(manifest),
             digest, "published", manifest["ingested_at"], manifest["ingested_at"]),
        )
        observation_ids: dict[str, str] = {}
        for raw in manifest["observations"]:
            payload = {
                "run_id": run_id, "source_id": source_id, "kind": raw["kind"],
                "value": raw["value"], "publication_time": raw["publication_time"],
                "valid_from": raw["valid_from"], "valid_to": raw["valid_to"],
                "observed_at": manifest["observed_at"], "ingested_at": manifest["ingested_at"],
                "region": raw["region"],
            }
            payload_digest = checksum(payload)
            observation_id = stable_id("eo", {"key": raw["key"], "payload": payload})
            observation_ids[str(raw["key"])] = observation_id
            con.execute(
                "INSERT INTO external_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, run_id, source_id, raw["kind"], canonical_json(raw["value"]),
                 raw["publication_time"], raw["valid_from"], raw["valid_to"],
                 manifest["observed_at"], manifest["ingested_at"], raw["region"], payload_digest),
            )
        if fault_at == "after_observations":
            raise RuntimeError("fault injection after_observations")
        fact_ids: list[str] = []
        for raw in manifest["facts"]:
            payload = {
                "run_id": run_id, "subject": raw["subject"], "predicate": raw["predicate"],
                "value": raw["value"], "valid_from": raw["valid_from"], "valid_to": raw["valid_to"],
                "region": raw["region"], "source_quality": float(raw["source_quality"]),
                "fact_confidence": float(raw["fact_confidence"]),
            }
            payload_digest = checksum(payload)
            fact_id = stable_id("ef", {"key": raw["key"], "payload": payload})
            conflicts = list(con.execute(
                "SELECT fact_id,value_json,valid_from,valid_to FROM external_facts "
                "WHERE subject=? AND predicate=? AND region=?",
                (raw["subject"], raw["predicate"], raw["region"]),
            ))
            conflicting = [row for row in conflicts if row[1] != canonical_json(raw["value"]) and _overlaps(
                str(row[2]), str(row[3]) if row[3] else None, raw["valid_from"], raw["valid_to"],
            )]
            stale = bool(raw["valid_to"] and raw["valid_to"] < manifest["ingested_at"])
            lifecycle = "conflict" if conflicting else ("stale" if stale else "current")
            con.execute(
                "INSERT INTO external_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (fact_id, run_id, raw["subject"], raw["predicate"], canonical_json(raw["value"]),
                 raw["valid_from"], raw["valid_to"], raw["region"], float(raw["source_quality"]),
                 float(raw["fact_confidence"]), lifecycle, payload_digest),
            )
            append_lifecycle_event(
                con, fact_id=fact_id, event_type="created", occurred_at=manifest["ingested_at"],
                payload={"run_id": run_id},
            )
            if stale:
                append_lifecycle_event(
                    con, fact_id=fact_id, event_type="staled", occurred_at=manifest["ingested_at"],
                    payload={"valid_to": raw["valid_to"]},
                )
            if conflicting:
                peers = sorted(str(row[0]) for row in conflicting)
                append_lifecycle_event(
                    con, fact_id=fact_id, event_type="conflicted", occurred_at=manifest["ingested_at"],
                    payload={"conflicting_fact_ids": peers},
                )
                for peer in peers:
                    append_lifecycle_event(
                        con, fact_id=peer, event_type="conflicted", occurred_at=manifest["ingested_at"],
                        payload={"conflicting_fact_ids": [fact_id]},
                    )
            for observation_key in raw["observation_keys"]:
                observation_id = observation_ids[str(observation_key)]
                support_digest = checksum({"fact_id": fact_id, "observation_id": observation_id})
                con.execute(
                    "INSERT INTO external_fact_support VALUES (?,?,?,?)",
                    (stable_id("efs", support_digest), fact_id, observation_id, support_digest),
                )
            fact_ids.append(fact_id)
        if fault_at == "after_facts":
            raise RuntimeError("fault injection after_facts")
        assert_foreign_key_integrity(con)
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.IntegrityError("external authority integrity check failed")
        con.commit()
        return {
            "run_id": run_id, "published": True, "no_op": False,
            "observation_count": len(observation_ids), "fact_count": len(fact_ids),
            "fact_ids": fact_ids,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


__all__ = [
    "IMPORT_SCHEMA_VERSION", "ExternalIngestError", "authority_fingerprint",
    "load_bounded_manifest", "publish_bounded_cohort",
]
