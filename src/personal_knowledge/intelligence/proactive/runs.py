"""Read-first planning and atomic publication for proactive intelligence runs."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

from .schema import (
    CANONICAL_DOMAINS, RELATION_TYPES, CandidateDraft, CoordinationDraft, CoordinationItem,
    ProactiveRun, SupportReference, canonical_domain, canonical_json, checksum,
    validate_metadata_payload,
)
from .ranking import (
    DEFAULT_NOISE_POLICY, DEFAULT_RANKING_POLICY, EvaluationContext, NoisePolicy,
    RankingPolicy, SurfaceRecord, evaluate_candidates, rank_candidates,
)
from .controls import ControlTarget, project_controls_connection

REGISTRY_ID = "a.proactive_intelligence"
REGISTRY_AUTHORITY_ROLE = "proactive_intelligence"
NO_CONTROL_FRONTIER_CHECKSUM = checksum({"control_events": []})
NO_DECISION_EVENT_FRONTIER_CHECKSUM = checksum({"decision_events": []})
PROACTIVE_TABLES = frozenset({
    "proactive_runs", "proactive_coordination_items", "proactive_candidates",
    "proactive_candidate_support", "proactive_evaluations",
    "proactive_control_events", "proactive_surface_events",
})


class ProactiveValidationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _assert_schema(con: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    existing = tables & PROACTIVE_TABLES
    if existing != PROACTIVE_TABLES:
        code = "proactive_schema_unapplied" if not existing else "proactive_schema_partial"
        raise ProactiveValidationError(code, ",".join(sorted(PROACTIVE_TABLES - existing)))


def _event_frontier(con: sqlite3.Connection, decision_run_id: str | None) -> str:
    if decision_run_id is None:
        return NO_DECISION_EVENT_FRONTIER_CHECKSUM
    rows = con.execute(
        "SELECT e.recommendation_id,e.sequence,e.payload_checksum FROM decision_events e "
        "JOIN decision_recommendations r ON r.recommendation_id=e.recommendation_id "
        "WHERE r.run_id=? ORDER BY e.recommendation_id,e.sequence", (decision_run_id,),
    ).fetchall()
    return checksum({"decision_events": [tuple(row) for row in rows]})


def _control_frontier(con: sqlite3.Connection) -> str:
    return checksum({"control_events": _control_frontier_manifest(con)})


def _control_frontier_manifest(con: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows = con.execute(
        "SELECT target_authority,target_type,target_id,sequence,payload_checksum "
        "FROM proactive_control_events ORDER BY target_authority,target_type,target_id,sequence"
    ).fetchall()
    return [tuple(row) for row in rows]


def _source_context(con: sqlite3.Connection, source_run_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT r.*,p.publication_sequence FROM personal_state_runs r "
        "JOIN personal_state_publications p ON p.run_id=r.run_id "
        "WHERE r.run_id=? AND r.status='committed'", (source_run_id,),
    ).fetchone()
    if row is None:
        raise ProactiveValidationError("source_run_unpublished", source_run_id)
    if checksum(json.loads(str(row["input_manifest_json"]))) != str(row["input_manifest_checksum"]):
        raise ProactiveValidationError("source_input_manifest_tampered", source_run_id)
    if checksum(json.loads(str(row["output_manifest_json"]))) != str(row["output_manifest_checksum"]):
        raise ProactiveValidationError("source_output_manifest_tampered", source_run_id)
    return row


def _decision_context(con: sqlite3.Connection, decision_run_id: str | None) -> sqlite3.Row | None:
    if decision_run_id is None:
        return None
    row = con.execute("SELECT * FROM decision_runs WHERE run_id=? AND status='committed'", (decision_run_id,)).fetchone()
    if row is None:
        raise ProactiveValidationError("decision_run_missing", decision_run_id)
    if checksum(json.loads(str(row["input_manifest_json"]))) != str(row["input_manifest_checksum"]):
        raise ProactiveValidationError("decision_input_manifest_tampered", decision_run_id)
    if checksum(json.loads(str(row["output_manifest_json"]))) != str(row["output_manifest_checksum"]):
        raise ProactiveValidationError("decision_output_manifest_tampered", decision_run_id)
    core = json.loads(str(row["output_manifest_json"]))
    if str(core.get("run_checksum")) != str(row["run_checksum"]):
        raise ProactiveValidationError("decision_run_checksum_tampered", decision_run_id)
    return row


def _validate_ref(con: sqlite3.Connection, ref: SupportReference, *, snapshot_id: str, snapshot_hash: str,
                  source_run_id: str, source_run_checksum: str, decision_run_id: str | None,
                  decision_run_checksum: str | None) -> None:
    if len(ref.record_checksum) != 64 or len(ref.source_run_checksum) != 64:
        raise ProactiveValidationError("support_checksum_invalid", ref.record_id)
    if ref.snapshot_id != snapshot_id or ref.snapshot_hash != snapshot_hash:
        raise ProactiveValidationError("support_snapshot_mismatch", ref.record_id)
    if ref.authority_id == "a.personal_change":
        if ref.source_run_id != source_run_id or ref.source_run_checksum != source_run_checksum:
            raise ProactiveValidationError("support_source_mismatch", ref.record_id)
        table_by_type = {"assertion": "personal_state_assertions", "change": "personal_state_changes", "risk": "personal_state_risks"}
        table = table_by_type.get(ref.record_type)
        if table is None:
            raise ProactiveValidationError("support_type_invalid", ref.record_type)
        row = con.execute(f"SELECT payload_checksum FROM {table} WHERE run_id=? AND {ref.record_type}_id=?", (source_run_id, ref.record_id)).fetchone()
    elif ref.authority_id == "a.decision_feedback":
        if decision_run_id is None or ref.source_run_id != decision_run_id or ref.source_run_checksum != decision_run_checksum:
            raise ProactiveValidationError("support_decision_mismatch", ref.record_id)
        table_and_id = {"recommendation": ("decision_recommendations", "recommendation_id"), "event": ("decision_events", "event_id"), "effectiveness": ("decision_effectiveness", "assessment_id")}.get(ref.record_type, (None, None))
        table, id_col = table_and_id
        if table is None:
            raise ProactiveValidationError("support_type_invalid", ref.record_type)
        if table == "decision_recommendations":
            row = con.execute(f"SELECT payload_checksum FROM {table} WHERE run_id=? AND {id_col}=?", (decision_run_id, ref.record_id)).fetchone()
        else:
            row = con.execute(f"SELECT t.payload_checksum FROM {table} t JOIN decision_recommendations r ON r.recommendation_id=t.recommendation_id WHERE r.run_id=? AND t.{id_col}=?", (decision_run_id, ref.record_id)).fetchone()
    else:
        raise ProactiveValidationError("support_authority_invalid", ref.authority_id)
    if row is None or str(row[0]) != ref.record_checksum:
        raise ProactiveValidationError("support_record_stale", ref.record_id)


def plan_run(
    db_path: Path, coordination: Iterable[CoordinationDraft], *,
    source_run_id: str, source_run_checksum: str, source_publication_sequence: int,
    decision_run_id: str | None = None, decision_run_checksum: str | None = None,
    coordination_policy: str, ranking_policy: str, noise_policy: str,
    input_manifest: Mapping[str, Any], candidate_drafts: Iterable[CandidateDraft] = (),
    ranking_config: RankingPolicy = DEFAULT_RANKING_POLICY,
    noise_config: NoisePolicy = DEFAULT_NOISE_POLICY,
    evaluation_context: EvaluationContext | None = None,
) -> ProactiveRun:
    drafts = tuple(coordination)
    candidate_inputs = tuple(candidate_drafts)
    if not drafts:
        raise ProactiveValidationError("coordination_required")
    validate_metadata_payload(input_manifest, "input_manifest")
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        _assert_schema(con)
        source = _source_context(con, source_run_id)
        if str(source["output_manifest_checksum"]) != source_run_checksum or int(source["publication_sequence"]) != source_publication_sequence:
            raise ProactiveValidationError("source_binding_changed", source_run_id)
        decision = _decision_context(con, decision_run_id)
        if decision is not None:
            if str(decision["run_checksum"]) != decision_run_checksum:
                raise ProactiveValidationError("decision_binding_changed", decision_run_id or "")
            for key in ("source_run_id", "source_run_checksum", "snapshot_id", "snapshot_hash"):
                expected = source_run_id if key == "source_run_id" else source_run_checksum if key == "source_run_checksum" else source[key]
                if str(decision[key]) != str(expected):
                    raise ProactiveValidationError("decision_source_mismatch", key)
        elif decision_run_checksum is not None:
            raise ProactiveValidationError("decision_binding_partial")
        snapshot_id, snapshot_hash = str(source["snapshot_id"]), str(source["snapshot_hash"])
        event_frontier = _event_frontier(con, decision_run_id)
        control_frontier_manifest = _control_frontier_manifest(con)
        control_frontier = checksum({"control_events": control_frontier_manifest})
        normalized: list[CoordinationDraft] = []
        for draft in drafts:
            if draft.relation_type not in RELATION_TYPES or not draft.source_refs:
                raise ProactiveValidationError("coordination_invalid", draft.relation_type)
            domains = tuple(sorted({canonical_domain(item) for item in draft.domains}, key=CANONICAL_DOMAINS.index))
            if not 1 <= len(domains) <= 8 or not 0 <= draft.confidence <= 1:
                raise ProactiveValidationError("coordination_invalid", draft.relation_type)
            for ref in draft.source_refs:
                _validate_ref(con, ref, snapshot_id=snapshot_id, snapshot_hash=snapshot_hash,
                              source_run_id=source_run_id, source_run_checksum=source_run_checksum,
                              decision_run_id=decision_run_id, decision_run_checksum=decision_run_checksum)
            decisive = {canonical_json(asdict(ref)) for ref in draft.source_refs}
            for resource in draft.resource_manifest:
                if not resource.resource_id.strip() or canonical_json(asdict(resource.source)) not in decisive:
                    raise ProactiveValidationError("resource_support_missing", resource.resource_id)
                _validate_ref(con, resource.source, snapshot_id=snapshot_id, snapshot_hash=snapshot_hash,
                              source_run_id=source_run_id, source_run_checksum=source_run_checksum,
                              decision_run_id=decision_run_id, decision_run_checksum=decision_run_checksum)
            validate_metadata_payload(asdict(draft), "coordination")
            normalized.append(CoordinationDraft(**{**asdict(draft), "domains": domains,
                "source_refs": draft.source_refs, "resource_manifest": draft.resource_manifest}))
        for candidate in candidate_inputs:
            for ref in candidate.support_refs:
                _validate_ref(con, ref, snapshot_id=snapshot_id, snapshot_hash=snapshot_hash,
                              source_run_id=source_run_id, source_run_checksum=source_run_checksum,
                              decision_run_id=decision_run_id, decision_run_checksum=decision_run_checksum)
        context = evaluation_context or EvaluationContext.fixed()
        controlled_candidates: list[CandidateDraft] = []
        for candidate in candidate_inputs:
            targets = [ControlTarget(ref.authority_id, ref.record_type, ref.record_id, ref.record_checksum)
                       for ref in candidate.support_refs]
            targets.append(ControlTarget("a.proactive_intelligence", "global", "proactive",
                                         checksum({"global": "proactive"})))
            targets.append(ControlTarget("a.proactive_intelligence", "policy", ranking_config.policy_id,
                                         checksum({"policy": ranking_config.policy_id})))
            targets.extend(ControlTarget("a.proactive_intelligence", "domain", domain,
                                         checksum({"domain": domain})) for domain in candidate.domains)
            projection = project_controls_connection(
                con, targets=tuple(targets), as_of=context.as_of, scope=candidate.scope,
                domains=candidate.domains, policies=(ranking_config.policy_id,),
            )
            metadata = dict(candidate.metadata or {})
            metadata["trust_control"] = {
                "frontier_checksum": control_frontier,
                "projection_checksum": projection.checksum,
                "active_event_ids": list(projection.active_event_ids),
                "correction_requested": projection.correction_requested,
            }
            controlled_candidates.append(replace(
                candidate,
                trust_eligible=candidate.trust_eligible and projection.eligible,
                reason_codes=tuple(sorted(set(candidate.reason_codes) | set(projection.reason_codes))),
                metadata=metadata,
            ))
        candidate_inputs = tuple(controlled_candidates)
    finally:
        con.close()
    manifest = {
        "schema_version": "proactive_run_v1", "registry_id": REGISTRY_ID,
        "source_run_id": source_run_id, "source_run_checksum": source_run_checksum,
        "source_publication_sequence": source_publication_sequence,
        "decision_run_id": decision_run_id, "decision_run_checksum": decision_run_checksum,
        "decision_event_frontier_checksum": event_frontier, "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash, "control_frontier_checksum": control_frontier,
        "control_frontier_manifest": control_frontier_manifest,
        "coordination_policy": coordination_policy, "ranking_policy": ranking_policy,
        "noise_policy": noise_policy, "request_input": input_manifest,
        "coordination_drafts": [asdict(item) for item in normalized],
        "candidate_drafts": [asdict(item) for item in candidate_inputs],
        "ranking_config": asdict(ranking_config), "noise_config": asdict(noise_config),
        "evaluation_context": asdict(context),
    }
    input_checksum = checksum(manifest)
    run_id = f"pir_{input_checksum[:24]}"
    items: list[CoordinationItem] = []
    for draft in normalized:
        payload = {"schema_version": "proactive_coordination_v1", "run_id": run_id, **asdict(draft)}
        coordination_id = f"pci_{checksum(payload)[:24]}"
        payload = {**payload, "coordination_id": coordination_id}
        items.append(CoordinationItem(coordination_id, run_id, draft, payload, checksum(payload)))
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        prior_rows = con.execute(
            "SELECT dedup_key,expires_at FROM proactive_candidates WHERE run_id<>?", (run_id,)
        ).fetchall()
    finally:
        con.close()
    prior_keys = frozenset(str(row[0]) for row in prior_rows)
    expired_keys = frozenset(str(row[0]) for row in prior_rows if _parse_time(str(row[1])) <= _parse_time(context.as_of))
    candidates = rank_candidates(candidate_inputs, policy=ranking_config, run_id=run_id,
                                 prior_dedup_keys=prior_keys, expired_prior_keys=expired_keys)
    evaluations = evaluate_candidates(candidates, context=context, policy=noise_config,
                                      ranking_policy=ranking_config)
    core = {"run_id": run_id, "input_manifest_checksum": input_checksum,
            "coordination_items": [{"coordination_id": i.coordination_id, "payload_checksum": i.payload_checksum} for i in items],
            "candidates": [{"candidate_id": i.candidate_id, "payload_checksum": i.payload_checksum} for i in candidates],
            "evaluations": [{"evaluation_id": i.evaluation_id, "payload_checksum": i.payload_checksum} for i in evaluations]}
    run_checksum = checksum(core)
    output = {**core, "run_checksum": run_checksum}
    return ProactiveRun(run_id, REGISTRY_ID, source_run_id, source_run_checksum,
        source_publication_sequence, decision_run_id, decision_run_checksum,
        event_frontier, snapshot_id, snapshot_hash, control_frontier,
        coordination_policy, ranking_policy, noise_policy, manifest, input_checksum,
        output, checksum(output), run_checksum, tuple(items), candidates, evaluations)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ProactiveValidationError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _support_ref(value: Mapping[str, Any]) -> SupportReference:
    return SupportReference(**{key: str(item) for key, item in value.items()})


def _candidate_draft(value: Mapping[str, Any]) -> CandidateDraft:
    data = dict(value)
    data["domains"] = tuple(str(item) for item in data["domains"])
    data["target_group"] = tuple(str(item) for item in data["target_group"])
    data["reason_codes"] = tuple(str(item) for item in data["reason_codes"])
    data["support_refs"] = tuple(_support_ref(item) for item in data["support_refs"])
    return CandidateDraft(**data)


def validate_run(db_path: Path, run: ProactiveRun) -> ProactiveRun:
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        if _control_frontier(con) != run.control_frontier_checksum:
            raise ProactiveValidationError("control_frontier_changed")
        if _event_frontier(con, run.decision_run_id) != run.decision_event_frontier_checksum:
            raise ProactiveValidationError("decision_frontier_changed")
    finally:
        con.close()
    request = run.input_manifest.get("request_input")
    drafts = tuple(item.draft for item in run.coordination_items)
    candidate_values = run.input_manifest.get("candidate_drafts", [])
    candidate_drafts = tuple(_candidate_draft(item) for item in candidate_values if isinstance(item, Mapping))
    ranking_value = run.input_manifest.get("ranking_config", asdict(DEFAULT_RANKING_POLICY))
    noise_value = run.input_manifest.get("noise_config", asdict(DEFAULT_NOISE_POLICY))
    context_value = dict(run.input_manifest.get("evaluation_context", asdict(EvaluationContext.fixed())))
    context_value["surface_records"] = tuple(SurfaceRecord(**item) for item in context_value.get("surface_records", ()))
    context_value["explicit_suppressions"] = tuple(context_value.get("explicit_suppressions", ()))
    ranking_value = dict(ranking_value); ranking_value["weights"] = tuple(tuple(item) for item in ranking_value["weights"])
    expected = plan_run(db_path, drafts, source_run_id=run.source_run_id,
        source_run_checksum=run.source_run_checksum, source_publication_sequence=run.source_publication_sequence,
        decision_run_id=run.decision_run_id, decision_run_checksum=run.decision_run_checksum,
        coordination_policy=run.coordination_policy, ranking_policy=run.ranking_policy,
        noise_policy=run.noise_policy, input_manifest=request if isinstance(request, Mapping) else {},
        candidate_drafts=candidate_drafts, ranking_config=RankingPolicy(**ranking_value),
        noise_config=NoisePolicy(**noise_value), evaluation_context=EvaluationContext(**context_value))
    if expected != run:
        raise ProactiveValidationError("run_content_mismatch", run.run_id)
    return run


def _validate_existing(con: sqlite3.Connection, run: ProactiveRun) -> None:
    row = con.execute("SELECT * FROM proactive_runs WHERE run_id=?", (run.run_id,)).fetchone()
    if row is None or checksum(json.loads(str(row["input_manifest_json"]))) != str(row["input_manifest_checksum"]) or checksum(json.loads(str(row["output_manifest_json"]))) != str(row["output_manifest_checksum"]) or str(row["run_checksum"]) != run.run_checksum:
        raise ProactiveValidationError("existing_run_tampered", run.run_id)
    rows = con.execute("SELECT coordination_id,payload_json,payload_checksum FROM proactive_coordination_items WHERE run_id=? ORDER BY coordination_id", (run.run_id,)).fetchall()
    expected = sorted((i.coordination_id, canonical_json(i.payload), i.payload_checksum) for i in run.coordination_items)
    actual = sorted((str(r[0]), canonical_json(json.loads(str(r[1]))), str(r[2])) for r in rows)
    if actual != expected:
        raise ProactiveValidationError("existing_coordination_tampered", run.run_id)
    rows = con.execute("SELECT candidate_id,payload_json,payload_checksum FROM proactive_candidates WHERE run_id=? ORDER BY candidate_id", (run.run_id,)).fetchall()
    expected = sorted((i.candidate_id, canonical_json(i.payload), i.payload_checksum) for i in run.candidates)
    actual = sorted((str(r[0]), canonical_json(json.loads(str(r[1]))), str(r[2])) for r in rows)
    if actual != expected:
        raise ProactiveValidationError("existing_candidate_tampered", run.run_id)
    rows = con.execute(
        "SELECT support_id,candidate_id,authority_id,record_type,record_id,record_checksum,"
        "source_run_id,source_run_checksum,snapshot_id,snapshot_hash,payload_json,payload_checksum "
        "FROM proactive_candidate_support WHERE candidate_id IN "
        "(SELECT candidate_id FROM proactive_candidates WHERE run_id=?) ORDER BY support_id", (run.run_id,),
    ).fetchall()
    expected_support = []
    for candidate in run.candidates:
        for ref in candidate.support_refs:
            payload = {"candidate_id": candidate.candidate_id, **asdict(ref)}
            expected_support.append((
                f"pcs_{checksum(payload)[:24]}", candidate.candidate_id, ref.authority_id,
                ref.record_type, ref.record_id, ref.record_checksum, ref.source_run_id,
                ref.source_run_checksum, ref.snapshot_id, ref.snapshot_hash,
                canonical_json(payload), checksum(payload),
            ))
    actual_support = []
    for support in rows:
        try:
            payload = json.loads(str(support[10]))
        except json.JSONDecodeError as exc:
            raise ProactiveValidationError("existing_support_tampered", run.run_id) from exc
        actual_support.append((*tuple(str(value) for value in support[:10]), canonical_json(payload), str(support[11])))
    if sorted(actual_support) != sorted(expected_support):
        raise ProactiveValidationError("existing_support_tampered", run.run_id)
    for candidate in run.candidates:
        for ref in candidate.support_refs:
            try:
                _validate_ref(con, ref, snapshot_id=run.snapshot_id, snapshot_hash=run.snapshot_hash,
                              source_run_id=run.source_run_id, source_run_checksum=run.source_run_checksum,
                              decision_run_id=run.decision_run_id, decision_run_checksum=run.decision_run_checksum)
            except ProactiveValidationError as exc:
                raise ProactiveValidationError("existing_support_tampered", run.run_id) from exc
    rows = con.execute("SELECT evaluation_id,payload_json,payload_checksum FROM proactive_evaluations WHERE candidate_id IN (SELECT candidate_id FROM proactive_candidates WHERE run_id=?) ORDER BY evaluation_id", (run.run_id,)).fetchall()
    expected = sorted((i.evaluation_id, canonical_json(i.payload), i.payload_checksum) for i in run.evaluations)
    actual = sorted((str(r[0]), canonical_json(json.loads(str(r[1]))), str(r[2])) for r in rows)
    if actual != expected:
        raise ProactiveValidationError("existing_evaluation_tampered", run.run_id)


def publish_run(db_path: Path, run: ProactiveRun, *, write: bool, inject_failure_at: str | None = None) -> dict[str, Any]:
    validate_run(db_path, run)
    result = {"ok": True, "run_id": run.run_id, "run_checksum": run.run_checksum,
              "written": False, "existing": False, "coordination_count": len(run.coordination_items)}
    if not write:
        return result
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        _assert_schema(con)
        source = _source_context(con, run.source_run_id)
        decision = _decision_context(con, run.decision_run_id)
        if str(source["output_manifest_checksum"]) != run.source_run_checksum or int(source["publication_sequence"]) != run.source_publication_sequence or str(source["snapshot_id"]) != run.snapshot_id or str(source["snapshot_hash"]) != run.snapshot_hash:
            raise ProactiveValidationError("source_binding_changed")
        if decision is not None and str(decision["run_checksum"]) != run.decision_run_checksum:
            raise ProactiveValidationError("decision_binding_changed")
        if _event_frontier(con, run.decision_run_id) != run.decision_event_frontier_checksum:
            raise ProactiveValidationError("decision_frontier_changed")
        if _control_frontier(con) != run.control_frontier_checksum:
            raise ProactiveValidationError("control_frontier_changed")
        existing = con.execute("SELECT 1 FROM proactive_runs WHERE run_id=?", (run.run_id,)).fetchone()
        if existing:
            _validate_existing(con, run)
            con.commit()
            return {**result, "existing": True}
        registry = con.execute("SELECT authority_role FROM artifact_registry_entries WHERE registry_id=?", (REGISTRY_ID,)).fetchone()
        if registry is None:
            con.execute("INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)", (REGISTRY_ID, "A", REGISTRY_AUTHORITY_ROLE, "R4", run.run_checksum, _now()))
        elif str(registry[0]) != REGISTRY_AUTHORITY_ROLE:
            raise ProactiveValidationError("registry_mismatch")
        con.execute("INSERT INTO proactive_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            run.run_id, run.registry_id, run.source_run_id, run.source_run_checksum,
            run.source_publication_sequence, run.decision_run_id, run.decision_run_checksum,
            run.decision_event_frontier_checksum, run.snapshot_id, run.snapshot_hash,
            run.control_frontier_checksum, run.coordination_policy, run.ranking_policy,
            run.noise_policy, canonical_json(run.input_manifest), run.input_manifest_checksum,
            canonical_json(run.output_manifest), run.output_manifest_checksum, run.run_checksum,
            "committed", _now()))
        for item in run.coordination_items:
            d = item.draft
            con.execute("INSERT INTO proactive_coordination_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                item.coordination_id, run.run_id, d.relation_type, d.subject, d.scope,
                canonical_json(d.domains), d.valid_from, d.valid_to, d.observed_at,
                d.rule_id, d.rule_version, d.confidence, d.uncertainty,
                canonical_json(d.resource_manifest), canonical_json(d.source_refs),
                canonical_json(item.payload), item.payload_checksum, _now()))
        if inject_failure_at == "after_coordination":
            raise RuntimeError("injected proactive publication failure")
        for candidate in run.candidates:
            con.execute("INSERT INTO proactive_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                candidate.candidate_id, run.run_id, candidate.candidate_class,
                candidate.presentation_kind, candidate.subject, candidate.scope,
                canonical_json(candidate.domains), candidate.dedup_key, candidate.valid_from,
                candidate.expires_at, candidate.policy_id, candidate.policy_version,
                canonical_json(asdict(candidate.importance)), candidate.uncertainty,
                canonical_json(candidate.reason_codes), canonical_json(candidate.payload),
                candidate.payload_checksum, _now()))
            for ref in candidate.support_refs:
                support_payload = {"candidate_id": candidate.candidate_id, **asdict(ref)}
                support_id = f"pcs_{checksum(support_payload)[:24]}"
                con.execute("INSERT INTO proactive_candidate_support VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    support_id, candidate.candidate_id, ref.authority_id, ref.record_type,
                    ref.record_id, ref.record_checksum, ref.source_run_id, ref.source_run_checksum,
                    ref.snapshot_id, ref.snapshot_hash, canonical_json(support_payload),
                    checksum(support_payload), _now()))
        if inject_failure_at == "after_candidates":
            raise RuntimeError("injected proactive candidate failure")
        for evaluation in run.evaluations:
            con.execute("INSERT INTO proactive_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                evaluation.evaluation_id, evaluation.candidate_id, evaluation.policy_id,
                evaluation.policy_version, evaluation.window_start, evaluation.window_end,
                evaluation.result, canonical_json(evaluation.reason_codes),
                evaluation.state_checksum, canonical_json(evaluation.payload),
                evaluation.payload_checksum, _now()))
        if inject_failure_at == "after_evaluations":
            raise RuntimeError("injected proactive evaluation failure")
        con.commit()
        return {**result, "written": True}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
