"""Read-first planning and atomic publication for personal-state analysis runs."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import re
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw
from personal_knowledge.retrieval.evidence import EvidenceResolver

from .schema import (
    ASSERTION_KINDS,
    ASSERTION_LIFECYCLES,
    EVIDENCE_TYPES,
    PRIVACY_CLASSES,
    PROVENANCE_CLASSES,
    SCHEMA_VERSION,
    EvidenceReference,
    PersonalStateRun,
    SnapshotBinding,
    StateAssertion,
    ValidatedAssertion,
    ValidatedEvidence,
    canonical_json,
    checksum,
)


REGISTRY_ID = "a.personal_change"
REGISTRY_AUTHORITY_ROLE = "personal_change_analysis"
_ROLE_BY_EVIDENCE_TYPE = {
    "canonical_message": "canonical_message",
    "knowledge_unit": "canonical_knowledge",
    "turn": "turn_summary",
    "google_signal": "google_assertion",
}
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"content", "body", "raw_text", "evidence_quote", "prompt", "response_text"}
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s${][^\s]*"
)


class PersonalStateValidationError(ValueError):
    """Stable fail-closed validation error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PersonalStateValidationError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise PersonalStateValidationError("invalid_time", f"{field}:timezone_required")
    return parsed


def _reject_private_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key)
            if label.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                raise PersonalStateValidationError("private_payload", f"{path}.{label}")
            _reject_private_payload(item, f"{path}.{label}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _reject_private_payload(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_RE.search(value):
        raise PersonalStateValidationError("secret_payload", path)


def _ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise PersonalStateValidationError("database_missing", str(db_path))
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _validate_registry(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT layer,authority_role FROM artifact_registry_entries WHERE registry_id=?",
        (REGISTRY_ID,),
    ).fetchone()
    if row is None:
        raise PersonalStateValidationError("registry_missing", REGISTRY_ID)
    if str(row["layer"]) != "A" or str(row["authority_role"]) != REGISTRY_AUTHORITY_ROLE:
        raise PersonalStateValidationError("registry_authority_mismatch", REGISTRY_ID)


def _resolve_snapshot(con: sqlite3.Connection, snapshot_id: str | None) -> SnapshotBinding:
    if snapshot_id:
        row = con.execute(
            "SELECT snapshot_id,manifest_hash,status FROM serving_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT s.snapshot_id,s.manifest_hash,s.status FROM serving_authority a "
            "JOIN serving_snapshots s ON s.snapshot_id=a.active_snapshot_id "
            "WHERE a.singleton_id=1"
        ).fetchone()
    if row is None:
        raise PersonalStateValidationError("snapshot_missing", snapshot_id or "active")
    if str(row["status"]) != "validated":
        raise PersonalStateValidationError("snapshot_not_validated", str(row["snapshot_id"]))
    members = {
        str(member["serving_role"]): dict(member)
        for member in con.execute(
            "SELECT m.serving_role,m.artifact_version_id,m.watermark_id,"
            "v.version,v.checksum,v.privacy_class,v.registry_id "
            "FROM serving_snapshot_members m "
            "JOIN artifact_versions v ON v.artifact_version_id=m.artifact_version_id "
            "WHERE m.snapshot_id=? ORDER BY m.serving_role",
            (row["snapshot_id"],),
        ).fetchall()
    }
    if not members:
        raise PersonalStateValidationError("snapshot_members_missing", str(row["snapshot_id"]))
    return SnapshotBinding(
        snapshot_id=str(row["snapshot_id"]),
        snapshot_hash=str(row["manifest_hash"]),
        members=members,
    )


def _resolved_evidence(
    evidence: EvidenceReference,
    *,
    snapshot: SnapshotBinding,
    resolver: EvidenceResolver,
) -> ValidatedEvidence:
    if evidence.artifact_type not in EVIDENCE_TYPES:
        raise PersonalStateValidationError("unknown_evidence_type", evidence.artifact_type)
    expected_role = _ROLE_BY_EVIDENCE_TYPE[evidence.artifact_type]
    if evidence.serving_role != expected_role:
        raise PersonalStateValidationError(
            "evidence_role_mismatch", f"{evidence.artifact_type}:{evidence.serving_role}"
        )
    member = snapshot.members.get(evidence.serving_role)
    if member is None:
        raise PersonalStateValidationError("evidence_role_missing", evidence.serving_role)
    if str(member.get("artifact_version_id") or "") != evidence.artifact_version_id:
        raise PersonalStateValidationError("evidence_version_mismatch", evidence.ref)
    if evidence.privacy_class not in PRIVACY_CLASSES:
        raise PersonalStateValidationError("invalid_privacy_class", evidence.privacy_class)
    if evidence.privacy_class != str(member.get("privacy_class") or ""):
        raise PersonalStateValidationError("evidence_privacy_mismatch", evidence.ref)

    result = resolver.resolve(
        evidence.ref,
        artifact_type=evidence.artifact_type,
        include_content=False,
        source_version=evidence.artifact_version_id,
    )
    metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
    privacy_marker = str(
        metadata.get("privacy")
        or metadata.get("privacy_class")
        or metadata.get("privacy_tier")
        or ""
    ).lower()
    if (
        result.get("status") != "ok"
        or result.get("eligible") is not True
        or privacy_marker in {"secret", "blocked", "ineligible"}
    ):
        raise PersonalStateValidationError(
            "evidence_ineligible", f"{evidence.artifact_type}:{evidence.ref}"
        )
    if result.get("content") not in {None, ""}:
        raise PersonalStateValidationError("resolver_returned_body", evidence.ref)
    safe_resolution = {
        "ref": evidence.ref,
        "artifact_type": evidence.artifact_type,
        "artifact_version_id": evidence.artifact_version_id,
        "metadata": metadata,
        "evidence_refs": result.get("evidence_refs") or [],
    }
    _reject_private_payload(safe_resolution, "evidence")
    digest = checksum(safe_resolution)
    if evidence.checksum and evidence.checksum != digest:
        raise PersonalStateValidationError("evidence_checksum_mismatch", evidence.ref)
    return ValidatedEvidence(
        ref=evidence.ref,
        artifact_type=evidence.artifact_type,
        serving_role=evidence.serving_role,
        artifact_version_id=evidence.artifact_version_id,
        evidence_checksum=digest,
        privacy_class=evidence.privacy_class,
    )


def _assertion_payload(
    assertion: ValidatedAssertion,
    *,
    snapshot: SnapshotBinding,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "assertion_kind": assertion.assertion_kind,
        "provenance_class": assertion.provenance_class,
        "subject": assertion.subject,
        "domain": assertion.domain,
        "scope": assertion.scope,
        "predicate": assertion.predicate,
        "value": assertion.value,
        "valid_from": assertion.valid_from,
        "valid_to": assertion.valid_to,
        "observed_at": assertion.observed_at,
        "confidence": float(assertion.confidence),
        "uncertainty": assertion.uncertainty,
        "lifecycle": assertion.lifecycle,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "evidence": [item for item in assertion.evidence],
    }


def _validated_assertion(
    assertion: StateAssertion,
    *,
    snapshot: SnapshotBinding,
    resolver: EvidenceResolver,
) -> ValidatedAssertion:
    if assertion.assertion_kind not in ASSERTION_KINDS:
        raise PersonalStateValidationError("invalid_assertion_kind", assertion.assertion_kind)
    if assertion.provenance_class not in PROVENANCE_CLASSES:
        raise PersonalStateValidationError("invalid_provenance_class", assertion.provenance_class)
    if assertion.lifecycle not in ASSERTION_LIFECYCLES:
        raise PersonalStateValidationError("invalid_assertion_lifecycle", assertion.lifecycle)
    for field, value in (
        ("subject", assertion.subject),
        ("domain", assertion.domain),
        ("scope", assertion.scope),
        ("predicate", assertion.predicate),
    ):
        if not value.strip():
            raise PersonalStateValidationError("missing_assertion_field", field)
    if not 0.0 <= float(assertion.confidence) <= 1.0:
        raise PersonalStateValidationError("invalid_confidence", str(assertion.confidence))
    valid_from = _parse_time(assertion.valid_from, "valid_from")
    _parse_time(assertion.observed_at, "observed_at")
    if assertion.valid_to and _parse_time(assertion.valid_to, "valid_to") < valid_from:
        raise PersonalStateValidationError("invalid_time_interval", assertion.predicate)
    if not assertion.evidence:
        raise PersonalStateValidationError("evidence_required", assertion.predicate)
    _reject_private_payload(assertion.value, "assertion.value")

    evidence = tuple(
        sorted(
            (
                _resolved_evidence(item, snapshot=snapshot, resolver=resolver)
                for item in assertion.evidence
            ),
            key=lambda item: (item.artifact_type, item.ref, item.artifact_version_id),
        )
    )
    evidence_keys = {(item.artifact_type, item.ref) for item in evidence}
    if len(evidence_keys) != len(evidence):
        raise PersonalStateValidationError("duplicate_evidence", assertion.predicate)
    validated = ValidatedAssertion(
        assertion_id="",
        assertion_kind=assertion.assertion_kind,
        provenance_class=assertion.provenance_class,
        subject=assertion.subject,
        domain=assertion.domain,
        scope=assertion.scope,
        predicate=assertion.predicate,
        value=assertion.value,
        valid_from=assertion.valid_from,
        valid_to=assertion.valid_to,
        observed_at=assertion.observed_at,
        confidence=float(assertion.confidence),
        uncertainty=assertion.uncertainty,
        lifecycle=assertion.lifecycle,
        evidence=evidence,
        payload_checksum="",
    )
    payload = _assertion_payload(validated, snapshot=snapshot)
    _reject_private_payload(payload, "assertion")
    payload_checksum = checksum(payload)
    return ValidatedAssertion(
        assertion_id=f"psa_{payload_checksum[:24]}",
        assertion_kind=validated.assertion_kind,
        provenance_class=validated.provenance_class,
        subject=validated.subject,
        domain=validated.domain,
        scope=validated.scope,
        predicate=validated.predicate,
        value=validated.value,
        valid_from=validated.valid_from,
        valid_to=validated.valid_to,
        observed_at=validated.observed_at,
        confidence=validated.confidence,
        uncertainty=validated.uncertainty,
        lifecycle=validated.lifecycle,
        evidence=validated.evidence,
        payload_checksum=payload_checksum,
    )


def _run_context_digest(
    snapshot: SnapshotBinding,
    producer_version: str,
    input_manifest: Mapping[str, Any],
) -> str:
    return checksum(
        {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "snapshot_members": snapshot.members,
            "producer_version": producer_version,
            "input": input_manifest,
        }
    )


def _scope_assertions(
    assertions: Iterable[ValidatedAssertion],
    *,
    context_digest: str,
) -> tuple[ValidatedAssertion, ...]:
    return tuple(
        sorted(
            (
                replace(
                    item,
                    assertion_id=f"psa_{checksum([context_digest, item.payload_checksum])[:24]}",
                )
                for item in assertions
            ),
            key=lambda item: item.assertion_id,
        )
    )


def plan_run(
    db_path: Path,
    assertions: Iterable[StateAssertion],
    *,
    producer_version: str,
    input_manifest: Mapping[str, Any],
    snapshot_id: str | None = None,
    resolver: EvidenceResolver | None = None,
) -> PersonalStateRun:
    """Build and validate a deterministic run without writing any state."""
    if not producer_version.strip():
        raise PersonalStateValidationError("producer_version_required")
    _reject_private_payload(input_manifest, "input_manifest")
    con = _ro(db_path)
    try:
        _validate_registry(con)
        snapshot = _resolve_snapshot(con, snapshot_id)
    finally:
        con.close()
    evidence_resolver = resolver or EvidenceResolver(unified_db=db_path)
    source_assertions = tuple(assertions)
    if any(item.assertion_id for item in source_assertions):
        raise PersonalStateValidationError("preassigned_assertion_id")
    base_validated = tuple(
        sorted(
            (
                _validated_assertion(item, snapshot=snapshot, resolver=evidence_resolver)
                for item in source_assertions
            ),
            key=lambda item: item.assertion_id,
        )
    )
    if not base_validated:
        raise PersonalStateValidationError("assertions_required")
    if len({item.payload_checksum for item in base_validated}) != len(base_validated):
        raise PersonalStateValidationError("duplicate_assertion")
    context_digest = _run_context_digest(snapshot, producer_version, input_manifest)
    validated = _scope_assertions(base_validated, context_digest=context_digest)
    canonical_input = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "snapshot_members": snapshot.members,
        "producer_version": producer_version,
        "input": input_manifest,
        "assertions": [item for item in validated],
    }
    input_digest = checksum(canonical_input)
    identity = {
        "registry_id": REGISTRY_ID,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "producer_version": producer_version,
        "input_manifest_checksum": input_digest,
    }
    run_id = f"psr_{checksum(identity)[:24]}"
    output_manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "assertion_ids": [item.assertion_id for item in validated],
        "assertion_count": len(validated),
        "evidence_count": sum(len(item.evidence) for item in validated),
    }
    return PersonalStateRun(
        run_id=run_id,
        registry_id=REGISTRY_ID,
        snapshot=snapshot,
        producer_version=producer_version,
        input_manifest=canonical_input,
        input_manifest_checksum=input_digest,
        output_manifest=output_manifest,
        output_manifest_checksum=checksum(output_manifest),
        assertions=validated,
    )


def validate_run(
    db_path: Path,
    run: PersonalStateRun,
    *,
    resolver: EvidenceResolver | None = None,
) -> PersonalStateRun:
    """Revalidate an already planned run against current immutable authorities."""
    if run.registry_id != REGISTRY_ID:
        raise PersonalStateValidationError("registry_authority_mismatch", run.registry_id)
    if not run.producer_version.strip():
        raise PersonalStateValidationError("producer_version_required")
    con = _ro(db_path)
    try:
        _validate_registry(con)
        current = _resolve_snapshot(con, run.snapshot.snapshot_id)
    finally:
        con.close()
    if current.snapshot_hash != run.snapshot.snapshot_hash:
        raise PersonalStateValidationError("snapshot_hash_mismatch", run.snapshot.snapshot_id)
    if canonical_json(current.members) != canonical_json(run.snapshot.members):
        raise PersonalStateValidationError("snapshot_members_mismatch", run.snapshot.snapshot_id)
    evidence_resolver = resolver or EvidenceResolver(unified_db=db_path)
    base_assertions: list[ValidatedAssertion] = []
    for assertion in run.assertions:
        refreshed = _validated_assertion(
            StateAssertion(
                assertion_kind=assertion.assertion_kind,
                provenance_class=assertion.provenance_class,
                subject=assertion.subject,
                domain=assertion.domain,
                scope=assertion.scope,
                predicate=assertion.predicate,
                value=assertion.value,
                valid_from=assertion.valid_from,
                valid_to=assertion.valid_to,
                observed_at=assertion.observed_at,
                confidence=assertion.confidence,
                uncertainty=assertion.uncertainty,
                lifecycle=assertion.lifecycle,
                assertion_id="",
                evidence=tuple(
                    EvidenceReference(
                    ref=item.ref,
                    artifact_type=item.artifact_type,
                    serving_role=item.serving_role,
                    artifact_version_id=item.artifact_version_id,
                    checksum=item.evidence_checksum,
                    privacy_class=item.privacy_class,
                    )
                    for item in assertion.evidence
                ),
            ),
            snapshot=current,
            resolver=evidence_resolver,
        )
        base_assertions.append(refreshed)
    input_value = run.input_manifest.get("input")
    context_digest = _run_context_digest(current, run.producer_version, input_value)
    refreshed_assertions = _scope_assertions(
        base_assertions, context_digest=context_digest
    )
    expected_by_id = {item.assertion_id: item for item in refreshed_assertions}
    actual_by_id = {item.assertion_id: item for item in run.assertions}
    if set(expected_by_id) != set(actual_by_id):
        raise PersonalStateValidationError("assertion_id_mismatch")
    if expected_by_id != actual_by_id:
        raise PersonalStateValidationError("assertion_drift")
    canonical_input = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": current.snapshot_id,
        "snapshot_hash": current.snapshot_hash,
        "snapshot_members": current.members,
        "producer_version": run.producer_version,
        "input": input_value,
        "assertions": refreshed_assertions,
    }
    if canonical_json(canonical_input) != canonical_json(run.input_manifest):
        raise PersonalStateValidationError("input_manifest_content_mismatch")
    if checksum(canonical_input) != run.input_manifest_checksum:
        raise PersonalStateValidationError("input_manifest_checksum_mismatch")
    expected_output = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.run_id,
        "snapshot_id": current.snapshot_id,
        "snapshot_hash": current.snapshot_hash,
        "assertion_ids": [item.assertion_id for item in refreshed_assertions],
        "assertion_count": len(refreshed_assertions),
        "evidence_count": sum(len(item.evidence) for item in refreshed_assertions),
    }
    if canonical_json(expected_output) != canonical_json(run.output_manifest):
        raise PersonalStateValidationError("output_manifest_content_mismatch")
    if checksum(expected_output) != run.output_manifest_checksum:
        raise PersonalStateValidationError("output_manifest_checksum_mismatch")
    identity = {
        "registry_id": run.registry_id,
        "snapshot_id": current.snapshot_id,
        "snapshot_hash": current.snapshot_hash,
        "producer_version": run.producer_version,
        "input_manifest_checksum": run.input_manifest_checksum,
    }
    if run.run_id != f"psr_{checksum(identity)[:24]}":
        raise PersonalStateValidationError("run_id_mismatch", run.run_id)
    return run


def publish_run(
    db_path: Path,
    run: PersonalStateRun,
    *,
    write: bool,
    resolver: EvidenceResolver | None = None,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    """Publish a whole validated run atomically; dry-run unless ``write=True``."""
    validate_run(db_path, run, resolver=resolver)
    result = {
        "ok": True,
        "run_id": run.run_id,
        "snapshot_id": run.snapshot.snapshot_id,
        "manifest_checksum": run.output_manifest_checksum,
        "assertion_count": len(run.assertions),
        "evidence_count": sum(len(item.evidence) for item in run.assertions),
        "written": False,
        "existing": False,
    }
    if not write:
        return result
    con = connect_rw(db_path, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT r.output_manifest_checksum,p.publication_sequence "
            "FROM personal_state_runs r LEFT JOIN personal_state_publications p "
            "ON p.run_id=r.run_id WHERE r.run_id=?",
            (run.run_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["output_manifest_checksum"]) != run.output_manifest_checksum:
                raise PersonalStateValidationError("existing_run_checksum_mismatch", run.run_id)
            if existing["publication_sequence"] is None:
                raise PersonalStateValidationError("publication_sequence_missing", run.run_id)
            con.commit()
            return {**result, "existing": True}
        con.execute(
            "INSERT INTO personal_state_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                run.registry_id,
                run.snapshot.snapshot_id,
                run.snapshot.snapshot_hash,
                run.producer_version,
                canonical_json(run.input_manifest),
                run.input_manifest_checksum,
                canonical_json(run.output_manifest),
                run.output_manifest_checksum,
                "committed",
                _now(),
            ),
        )
        con.execute(
            "INSERT INTO personal_state_publications (run_id,created_at) VALUES (?,?)",
            (run.run_id, _now()),
        )
        inserted = 0
        for assertion in run.assertions:
            # Persist exactly the canonical payload bound by payload_checksum.
            # Read hydration can therefore verify both the normalized columns
            # and the stored payload without consulting mutable source bodies.
            payload = _assertion_payload(assertion, snapshot=run.snapshot)
            con.execute(
                "INSERT INTO personal_state_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    assertion.assertion_id,
                    run.run_id,
                    assertion.assertion_kind,
                    assertion.provenance_class,
                    assertion.subject,
                    assertion.domain,
                    assertion.scope,
                    assertion.predicate,
                    canonical_json(assertion.value),
                    assertion.valid_from,
                    assertion.valid_to,
                    assertion.observed_at,
                    assertion.confidence,
                    assertion.uncertainty,
                    assertion.lifecycle,
                    canonical_json(payload),
                    assertion.payload_checksum,
                    _now(),
                ),
            )
            for item in assertion.evidence:
                evidence_id = f"pse_{checksum([assertion.assertion_id, item])[:24]}"
                con.execute(
                    "INSERT INTO personal_state_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        assertion.assertion_id,
                        run.snapshot.snapshot_id,
                        run.snapshot.snapshot_hash,
                        item.serving_role,
                        item.artifact_version_id,
                        item.artifact_type,
                        item.ref,
                        item.evidence_checksum,
                        "eligible",
                        item.privacy_class,
                        _now(),
                    ),
                )
            inserted += 1
            if inject_failure_after is not None and inserted >= inject_failure_after:
                raise RuntimeError("injected personal-state publication failure")
        con.commit()
        return {**result, "written": True}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
