"""Read-first planning and atomic publication for decision-feedback runs."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw
from personal_knowledge.intelligence.service import IntelligenceService, IntelligenceServiceError

from .schema import (
    GENESIS_SENTINEL,
    SCHEMA_VERSION,
    CognitionReference,
    DecisionRun,
    DecisionSchemaError,
    Recommendation,
    RecommendationDraft,
    RecommendationGenesis,
    canonical_json,
    checksum,
)


REGISTRY_ID = "a.decision_feedback"
REGISTRY_AUTHORITY_ROLE = "decision_feedback"
_FORBIDDEN_KEYS = frozenset(
    {"content", "body", "raw_text", "evidence_quote", "prompt", "response_text",
     "knowledge_unit", "fact", "approved", "executed", "command", "credential",
     "dispatch_target"}
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s${][^\s]*"
)


class DecisionValidationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DecisionValidationError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise DecisionValidationError("invalid_time", f"{field}:timezone_required")


def _reject_private(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key)
            if label.lower() in _FORBIDDEN_KEYS:
                raise DecisionValidationError("forbidden_decision_field", f"{path}.{label}")
            _reject_private(item, f"{path}.{label}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _reject_private(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_RE.search(value):
        raise DecisionValidationError("secret_payload", path)


def _ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise DecisionValidationError("database_missing", str(db_path))
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _registry(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT layer,authority_role,privacy_class FROM artifact_registry_entries WHERE registry_id=?",
        (REGISTRY_ID,),
    ).fetchone()
    if row is None:
        raise DecisionValidationError("registry_missing", REGISTRY_ID)
    if tuple(map(str, row)) != ("A", REGISTRY_AUTHORITY_ROLE, "R4"):
        raise DecisionValidationError("registry_authority_mismatch", REGISTRY_ID)


def _source_context(db_path: Path, source_run_id: str) -> tuple[Any, int]:
    try:
        context = IntelligenceService(db_path, resolver=object())._load_context(
            snapshot_id=None, run_id=source_run_id
        )
    except IntelligenceServiceError as exc:
        code = "source_run_unpublished" if exc.code == "publication_sequence_missing" else f"source_{exc.code}"
        raise DecisionValidationError(code, exc.detail or source_run_id) from exc
    con = _ro(db_path)
    try:
        _registry(con)
        row = con.execute(
            "SELECT publication_sequence FROM personal_state_publications WHERE run_id=?",
            (source_run_id,),
        ).fetchone()
        if row is None:
            raise DecisionValidationError("source_run_unpublished", source_run_id)
        return context, int(row["publication_sequence"])
    finally:
        con.close()


def resolve_cognition_reference(
    db_path: Path,
    *,
    source_run_id: str,
    record_id: str | None,
    cognitive_type: str,
) -> CognitionReference:
    """Resolve one Phase 25 truth/inference as a typed reference, never a copy."""
    if cognitive_type not in {"fact", "observation", "inference"}:
        raise DecisionValidationError("invalid_cognitive_reference", cognitive_type)
    context, publication_sequence = _source_context(Path(db_path), source_run_id)
    source = context["selected"]
    selected = None
    for assertion in source.assertions:
        if (record_id is None or assertion.assertion_id == record_id) and assertion.provenance_class == cognitive_type:
            selected = assertion
            break
    if selected is not None:
        if not selected.evidence:
            raise DecisionValidationError("support_ineligible", selected.assertion_id)
        return CognitionReference(
            cognitive_type=cognitive_type,
            authority_id="a.personal_change",
            record_id=selected.assertion_id,
            source_run_id=source.run_id,
            source_run_checksum=source.output_manifest_checksum,
            source_publication_sequence=publication_sequence,
            snapshot_id=source.snapshot.snapshot_id,
            snapshot_hash=source.snapshot.snapshot_hash,
            provenance_class=selected.provenance_class,
            evidence_status="eligible",
            uncertainty=selected.uncertainty,
            record_checksum=selected.payload_checksum,
        )
    if cognitive_type != "inference" or record_id is None:
        raise DecisionValidationError("support_record_missing", record_id or cognitive_type)
    con = _ro(Path(db_path))
    try:
        for table, id_column in (("personal_state_changes", "change_id"), ("personal_state_risks", "risk_id")):
            row = con.execute(
                f"SELECT run_id,payload_json,payload_checksum,uncertainty FROM {table} WHERE {id_column}=?",
                (record_id,),
            ).fetchone()
            if row is None:
                continue
            if str(row["run_id"]) != source_run_id:
                raise DecisionValidationError("cross_run_support", record_id)
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as exc:
                raise DecisionValidationError("support_payload_invalid", record_id) from exc
            if checksum(payload) != str(row["payload_checksum"]):
                raise DecisionValidationError("support_checksum_mismatch", record_id)
            return CognitionReference(
                cognitive_type="inference", authority_id="a.personal_change",
                record_id=record_id, source_run_id=source.run_id,
                source_run_checksum=source.output_manifest_checksum,
                source_publication_sequence=publication_sequence,
                snapshot_id=source.snapshot.snapshot_id, snapshot_hash=source.snapshot.snapshot_hash,
                provenance_class="inference", evidence_status="eligible",
                uncertainty=str(row["uncertainty"]), record_checksum=str(row["payload_checksum"]),
            )
    finally:
        con.close()
    raise DecisionValidationError("support_record_missing", record_id)


def _recommendation_payload(
    draft: RecommendationDraft,
    *,
    run_id: str,
    source_run_id: str,
    source_run_checksum: str,
    snapshot_id: str,
    snapshot_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "cognitive_type": "recommendation",
        "authority_id": REGISTRY_ID,
        "run_id": run_id,
        "source_run_id": source_run_id,
        "source_run_checksum": source_run_checksum,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "subject": draft.subject,
        "domain": draft.domain,
        "scope": draft.scope,
        "recommendation_kind": draft.recommendation_kind,
        "target": draft.target,
        "horizon": draft.horizon,
        "rationale_codes": list(draft.rationale_codes),
        "expected_benefit": draft.expected_benefit,
        "costs_constraints": list(draft.costs_constraints),
        "assumptions": list(draft.assumptions),
        "contraindications": list(draft.contraindications),
        "confidence": draft.confidence,
        "uncertainty": draft.uncertainty,
        "expires_at": draft.expires_at,
        "support": [asdict(item) for item in draft.support],
    }


def _build_run(
    drafts: tuple[RecommendationDraft, ...],
    *,
    source_run_id: str,
    source_run_checksum: str,
    source_publication_sequence: int,
    snapshot_id: str,
    snapshot_hash: str,
    policy_id: str,
    policy_version: str,
    request_input: Mapping[str, Any],
) -> DecisionRun:
    input_manifest = {
        "schema_version": SCHEMA_VERSION,
        "registry_id": REGISTRY_ID,
        "source_run_id": source_run_id,
        "source_run_checksum": source_run_checksum,
        "source_publication_sequence": source_publication_sequence,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "request_input": request_input,
        "recommendation_drafts": [asdict(item) for item in drafts],
    }
    input_checksum = checksum(input_manifest)
    identity = {
        "registry_id": REGISTRY_ID,
        "source_run_id": source_run_id,
        "source_run_checksum": source_run_checksum,
        "source_publication_sequence": source_publication_sequence,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "input_manifest_checksum": input_checksum,
    }
    run_id = f"dfr_{checksum(identity)[:24]}"
    recommendations: list[Recommendation] = []
    for draft in drafts:
        payload = _recommendation_payload(
            draft, run_id=run_id, source_run_id=source_run_id,
            source_run_checksum=source_run_checksum, snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
        )
        recommendation_id = f"drec_{checksum(payload)[:24]}"
        payload = {**payload, "recommendation_id": recommendation_id}
        recommendations.append(Recommendation(
            recommendation_id=recommendation_id, run_id=run_id,
            source_run_id=source_run_id, source_run_checksum=source_run_checksum,
            snapshot_id=snapshot_id, snapshot_hash=snapshot_hash,
            subject=draft.subject, domain=draft.domain, scope=draft.scope,
            recommendation_kind=draft.recommendation_kind, target=draft.target,
            horizon=draft.horizon, rationale_codes=draft.rationale_codes,
            expected_benefit=draft.expected_benefit,
            costs_constraints=draft.costs_constraints, assumptions=draft.assumptions,
            contraindications=draft.contraindications, confidence=draft.confidence,
            uncertainty=draft.uncertainty, expires_at=draft.expires_at,
            support=draft.support, payload=payload, payload_checksum=checksum(payload),
        ))
    core = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "source_run_id": source_run_id,
        "source_run_checksum": source_run_checksum,
        "source_publication_sequence": source_publication_sequence,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "input_manifest_checksum": input_checksum,
        "recommendations": [
            {"recommendation_id": item.recommendation_id, "payload_checksum": item.payload_checksum}
            for item in recommendations
        ],
    }
    run_checksum = checksum(core)
    genesis: list[RecommendationGenesis] = []
    for item in recommendations:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event_type": "recommendation_published",
            "sequence": 1,
            "recommendation_id": item.recommendation_id,
            "recommendation_checksum": item.payload_checksum,
            "decision_run_id": run_id,
            "decision_run_checksum": run_checksum,
            "source_run_id": source_run_id,
            "source_run_checksum": source_run_checksum,
            "source_publication_sequence": source_publication_sequence,
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
            "previous_event_checksum": GENESIS_SENTINEL,
        }
        event_id = f"dev_{checksum(payload)[:24]}"
        genesis.append(RecommendationGenesis(
            event_id=event_id, recommendation_id=item.recommendation_id, sequence=1,
            event_type="recommendation_published", typed_record_id=item.recommendation_id,
            previous_event_checksum=GENESIS_SENTINEL, payload=payload,
            payload_checksum=checksum(payload),
        ))
    output_manifest = {
        **core,
        "run_checksum": run_checksum,
        "genesis_events": [
            {"event_id": item.event_id, "payload_checksum": item.payload_checksum}
            for item in genesis
        ],
    }
    return DecisionRun(
        run_id=run_id, registry_id=REGISTRY_ID, source_run_id=source_run_id,
        source_run_checksum=source_run_checksum,
        source_publication_sequence=source_publication_sequence,
        snapshot_id=snapshot_id, snapshot_hash=snapshot_hash,
        policy_id=policy_id, policy_version=policy_version,
        input_manifest=input_manifest, input_manifest_checksum=input_checksum,
        output_manifest=output_manifest,
        output_manifest_checksum=checksum(output_manifest), run_checksum=run_checksum,
        recommendations=tuple(recommendations), genesis_events=tuple(genesis),
    )


def plan_run(
    db_path: Path,
    recommendations: Iterable[RecommendationDraft],
    *,
    policy_id: str,
    policy_version: str,
    input_manifest: Mapping[str, Any],
) -> DecisionRun:
    drafts = tuple(recommendations)
    if not drafts:
        raise DecisionValidationError("recommendations_required")
    if not policy_id.strip() or not policy_version.strip():
        raise DecisionValidationError("policy_required")
    _reject_private(input_manifest, "input")
    for draft in drafts:
        _parse_time(draft.expires_at, "expires_at")
        _reject_private(asdict(draft), "recommendation")
    anchor = drafts[0].support[0]
    for draft in drafts:
        for ref in draft.support:
            if ref.source_run_id != anchor.source_run_id:
                raise DecisionValidationError("cross_run_support", ref.record_id)
            if ref.snapshot_id != anchor.snapshot_id or ref.snapshot_hash != anchor.snapshot_hash:
                raise DecisionValidationError("cross_snapshot_support", ref.record_id)
            if ref.source_run_checksum != anchor.source_run_checksum:
                raise DecisionValidationError("source_run_checksum_mismatch", ref.record_id)
            if ref.source_publication_sequence != anchor.source_publication_sequence:
                raise DecisionValidationError("source_publication_mismatch", ref.record_id)
            try:
                current = resolve_cognition_reference(
                    Path(db_path), source_run_id=ref.source_run_id,
                    record_id=ref.record_id, cognitive_type=ref.cognitive_type,
                )
            except DecisionSchemaError as exc:
                raise DecisionValidationError(exc.code, exc.detail) from exc
            if current != ref:
                if current.source_run_checksum != ref.source_run_checksum:
                    raise DecisionValidationError("source_run_checksum_mismatch", ref.record_id)
                if current.snapshot_id != ref.snapshot_id or current.snapshot_hash != ref.snapshot_hash:
                    raise DecisionValidationError("cross_snapshot_support", ref.record_id)
                if current.source_publication_sequence != ref.source_publication_sequence:
                    raise DecisionValidationError("source_publication_mismatch", ref.record_id)
                raise DecisionValidationError("support_reference_stale", ref.record_id)
    context, sequence = _source_context(Path(db_path), anchor.source_run_id)
    source = context["selected"]
    if source.output_manifest_checksum != anchor.source_run_checksum:
        raise DecisionValidationError("source_run_checksum_mismatch", anchor.source_run_id)
    if sequence != anchor.source_publication_sequence:
        raise DecisionValidationError("source_publication_mismatch", anchor.source_run_id)
    if source.snapshot.snapshot_id != anchor.snapshot_id or source.snapshot.snapshot_hash != anchor.snapshot_hash:
        raise DecisionValidationError("cross_snapshot_support", anchor.source_run_id)
    return _build_run(
        drafts, source_run_id=source.run_id,
        source_run_checksum=source.output_manifest_checksum,
        source_publication_sequence=sequence,
        snapshot_id=source.snapshot.snapshot_id, snapshot_hash=source.snapshot.snapshot_hash,
        policy_id=policy_id, policy_version=policy_version, request_input=input_manifest,
    )


def validate_run(db_path: Path, run: DecisionRun) -> DecisionRun:
    if run.registry_id != REGISTRY_ID:
        raise DecisionValidationError("registry_mismatch", run.registry_id)
    drafts = tuple(RecommendationDraft(
        subject=item.subject, domain=item.domain, scope=item.scope,
        recommendation_kind=item.recommendation_kind, target=item.target,
        horizon=item.horizon, rationale_codes=item.rationale_codes,
        expected_benefit=item.expected_benefit,
        costs_constraints=item.costs_constraints, assumptions=item.assumptions,
        contraindications=item.contraindications, confidence=item.confidence,
        uncertainty=item.uncertainty, expires_at=item.expires_at, support=item.support,
    ) for item in run.recommendations)
    request_input = run.input_manifest.get("request_input")
    if not isinstance(request_input, Mapping):
        raise DecisionValidationError("input_manifest_invalid")
    expected = plan_run(
        Path(db_path), drafts, policy_id=run.policy_id, policy_version=run.policy_version,
        input_manifest=request_input,
    )
    if expected != run:
        if expected.input_manifest_checksum != run.input_manifest_checksum:
            raise DecisionValidationError("input_manifest_checksum_mismatch")
        if expected.run_checksum != run.run_checksum:
            raise DecisionValidationError("run_checksum_mismatch")
        if expected.output_manifest_checksum != run.output_manifest_checksum:
            raise DecisionValidationError("output_manifest_checksum_mismatch")
        raise DecisionValidationError("decision_run_content_mismatch", run.run_id)
    return run


def _validate_existing(con: sqlite3.Connection, run: DecisionRun) -> None:
    row = con.execute("SELECT * FROM decision_runs WHERE run_id=?", (run.run_id,)).fetchone()
    if row is None:
        raise DecisionValidationError("existing_run_missing", run.run_id)
    if (
        str(row["run_checksum"]) != run.run_checksum
        or str(row["input_manifest_checksum"]) != run.input_manifest_checksum
        or str(row["output_manifest_checksum"]) != run.output_manifest_checksum
        or checksum(json.loads(str(row["input_manifest_json"]))) != run.input_manifest_checksum
        or checksum(json.loads(str(row["output_manifest_json"]))) != run.output_manifest_checksum
    ):
        raise DecisionValidationError("existing_run_checksum_mismatch", run.run_id)
    rec_rows = con.execute(
        "SELECT recommendation_id,payload_json,payload_checksum FROM decision_recommendations WHERE run_id=? ORDER BY recommendation_id",
        (run.run_id,),
    ).fetchall()
    if len(rec_rows) != len(run.recommendations):
        raise DecisionValidationError("recommendation_count_mismatch", run.run_id)
    by_id = {item.recommendation_id: item for item in run.recommendations}
    for row in rec_rows:
        item = by_id.get(str(row["recommendation_id"]))
        if item is None or canonical_json(json.loads(str(row["payload_json"]))) != canonical_json(item.payload) or str(row["payload_checksum"]) != item.payload_checksum or checksum(item.payload) != item.payload_checksum:
            raise DecisionValidationError("recommendation_checksum_mismatch", str(row["recommendation_id"]))
        support_rows = con.execute(
            "SELECT payload_json,payload_checksum FROM decision_support_refs WHERE recommendation_id=? ORDER BY support_id",
            (item.recommendation_id,),
        ).fetchall()
        expected_support = sorted((canonical_json(asdict(ref)), checksum(asdict(ref))) for ref in item.support)
        actual_support = sorted((canonical_json(json.loads(str(s["payload_json"]))), str(s["payload_checksum"])) for s in support_rows)
        if actual_support != expected_support:
            raise DecisionValidationError("support_checksum_mismatch", item.recommendation_id)
    for event in run.genesis_events:
        row = con.execute(
            "SELECT * FROM decision_events WHERE recommendation_id=? AND sequence=1",
            (event.recommendation_id,),
        ).fetchone()
        if row is None:
            raise DecisionValidationError("genesis_missing", event.recommendation_id)
        if str(row["event_type"]) != "recommendation_published" or str(row["previous_event_checksum"]) != GENESIS_SENTINEL or str(row["payload_checksum"]) != event.payload_checksum or canonical_json(json.loads(str(row["payload_json"]))) != canonical_json(event.payload) or checksum(event.payload) != event.payload_checksum:
            raise DecisionValidationError("genesis_checksum_mismatch", event.recommendation_id)


def publish_run(
    db_path: Path,
    run: DecisionRun,
    *,
    write: bool,
    inject_failure_at: str | None = None,
) -> dict[str, Any]:
    validate_run(Path(db_path), run)
    result = {
        "ok": True, "run_id": run.run_id, "run_checksum": run.run_checksum,
        "source_run_id": run.source_run_id, "snapshot_id": run.snapshot_id,
        "recommendation_count": len(run.recommendations), "written": False,
        "existing": False,
    }
    if not write:
        return result
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        _registry(con)
        source = con.execute(
            "SELECT r.output_manifest_json,r.output_manifest_checksum,r.snapshot_id,r.snapshot_hash,p.publication_sequence "
            "FROM personal_state_runs r JOIN personal_state_publications p ON p.run_id=r.run_id "
            "WHERE r.run_id=? AND r.status='committed'",
            (run.source_run_id,),
        ).fetchone()
        if source is None:
            raise DecisionValidationError("source_run_unpublished", run.source_run_id)
        if checksum(json.loads(str(source["output_manifest_json"]))) != str(source["output_manifest_checksum"]):
            raise DecisionValidationError("source_run_checksum_mismatch", run.source_run_id)
        if (
            str(source["output_manifest_checksum"]) != run.source_run_checksum
            or int(source["publication_sequence"]) != run.source_publication_sequence
            or str(source["snapshot_id"]) != run.snapshot_id
            or str(source["snapshot_hash"]) != run.snapshot_hash
        ):
            raise DecisionValidationError("source_binding_changed", run.source_run_id)
        existing = con.execute("SELECT 1 FROM decision_runs WHERE run_id=?", (run.run_id,)).fetchone()
        if existing is not None:
            _validate_existing(con, run)
            con.commit()
            return {**result, "existing": True}
        con.execute(
            "INSERT INTO decision_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.registry_id, run.source_run_id, run.source_run_checksum,
             run.source_publication_sequence, run.snapshot_id, run.snapshot_hash,
             run.policy_id, run.policy_version, canonical_json(run.input_manifest),
             run.input_manifest_checksum, canonical_json(run.output_manifest),
             run.output_manifest_checksum, run.run_checksum, "committed", _now()),
        )
        genesis_by_rec = {item.recommendation_id: item for item in run.genesis_events}
        for rec in run.recommendations:
            con.execute(
                "INSERT INTO decision_recommendations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.recommendation_id, run.run_id, rec.source_run_id,
                 rec.source_run_checksum, rec.snapshot_id, rec.snapshot_hash,
                 rec.subject, rec.domain, rec.scope, rec.recommendation_kind,
                 rec.target, rec.horizon, rec.confidence, rec.uncertainty,
                 rec.expires_at, canonical_json(rec.payload), rec.payload_checksum, _now()),
            )
            for ref in rec.support:
                payload = asdict(ref)
                support_id = f"dsr_{checksum([rec.recommendation_id, payload])[:24]}"
                con.execute(
                    "INSERT INTO decision_support_refs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (support_id, rec.recommendation_id, ref.cognitive_type,
                     ref.authority_id, ref.record_id, ref.source_run_id,
                     ref.source_run_checksum, ref.source_publication_sequence,
                     ref.snapshot_id, ref.snapshot_hash, ref.provenance_class,
                     ref.evidence_status, ref.uncertainty, ref.record_checksum,
                     canonical_json(payload), checksum(payload), _now()),
                )
            if inject_failure_at == "after_recommendation":
                raise RuntimeError("injected decision publication failure after recommendation")
            event = genesis_by_rec[rec.recommendation_id]
            con.execute(
                "INSERT INTO decision_events VALUES (?,?,?,?,?,?,?,?,?)",
                (event.event_id, event.recommendation_id, event.sequence,
                 event.event_type, event.typed_record_id,
                 event.previous_event_checksum, canonical_json(event.payload),
                 event.payload_checksum, _now()),
            )
            if inject_failure_at == "after_genesis":
                raise RuntimeError("injected decision publication failure after genesis")
        con.commit()
        return {**result, "written": True}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
