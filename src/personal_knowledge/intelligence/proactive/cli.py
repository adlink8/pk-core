"""Local proactive intelligence CLI: read everywhere, explicit user writes locally."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from personal_knowledge.core.project_paths import KNOWLEDGE_ACTIVE_POINTER, ROOT, UNIFIED_DB
from personal_knowledge.intelligence.cli import _FINGERPRINT_GROUPS, _checkpoint_status, _phase24_dependency_status, _table_fingerprint
from personal_knowledge.intelligence.decision.cli import _DECISION_TABLES, _sandbox_loop as _decision_sandbox
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.intelligence.proactive.coordination import coordinate_goals
from personal_knowledge.intelligence.proactive.ranking import DEFAULT_NOISE_POLICY, DEFAULT_RANKING_POLICY, EvaluationContext

from .controls import ControlCommand, ControlError, ControlTarget, append_control
from .runs import PROACTIVE_TABLES, plan_run, publish_run
from .schema import CANONICAL_DOMAINS, CandidateDraft, GoalSignal, ResourceClaim, SupportReference, canonical_json, checksum
from .service import INTERFACE_SCHEMA_VERSION, ProactiveIntelligenceService


_PRODUCT_UAT = (
    ROOT / ".planning" / "phases"
    / "PDA-27-proactive-multi-domain-intelligence-and-target-d-acceptance-"
    / "27-UAT.md"
)
_ACCEPTED_UAT_STATUSES = frozenset({"accepted", "complete", "completed", "pass", "passed"})


def _product_uat_status(path: Path = _PRODUCT_UAT) -> dict[str, Any]:
    evidence = _checkpoint_status(path)
    return {**evidence, "accepted": evidence["status"] in _ACCEPTED_UAT_STATUSES}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pk-proactive")
    parser.add_argument("--db", type=Path, default=UNIFIED_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inbox", "digest"):
        item = commands.add_parser(name); item.add_argument("--domain"); item.add_argument("--limit", type=int, default=50); item.add_argument("--json", action="store_true")
    for name in ("get", "explain", "controls-status"):
        item = commands.add_parser(name); item.add_argument("--candidate-id", required=True)
        if name == "controls-status": item.add_argument("--as-of")
        item.add_argument("--json", action="store_true")
    metrics = commands.add_parser("metrics"); metrics.add_argument("--json", action="store_true")
    control = commands.add_parser("control")
    control.add_argument("--candidate-id", required=True); control.add_argument("--candidate-checksum", required=True)
    control.add_argument("--operation", required=True, choices=sorted(ControlCommand.OPERATIONS)); control.add_argument("--scope", default="global")
    control.add_argument("--reason-code", required=True); control.add_argument("--created-at", required=True); control.add_argument("--expires-at")
    control.add_argument("--rollback-of-event-id"); control.add_argument("--details-json", default="{}")
    _write_guards(control)
    surface = commands.add_parser("surface")
    surface.add_argument("--candidate-id", required=True); surface.add_argument("--candidate-checksum", required=True)
    surface.add_argument("--event-type", required=True, choices=("presented", "acknowledged", "dismissed")); surface.add_argument("--occurred-at", required=True)
    _write_guards(surface)
    acceptance = commands.add_parser("acceptance"); acceptance.add_argument("--dry-run", action="store_true"); acceptance.add_argument("--metadata-only", action="store_true"); acceptance.add_argument("--active-pointer", type=Path, default=KNOWLEDGE_ACTIVE_POINTER); acceptance.add_argument("--json", action="store_true")
    return parser


def _write_guards(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--write", action="store_true"); parser.add_argument("--i-confirm")
    parser.add_argument("--actor-class", required=True); parser.add_argument("--actor-identity-hash", required=True)
    parser.add_argument("--expected-sequence", type=int, required=True); parser.add_argument("--idempotency-key", required=True); parser.add_argument("--json", action="store_true")


def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
    return ProactiveIntelligenceService._error(operation, code, detail)


def _guard(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.write: return _error(args.command, "write_required")
    if not args.i_confirm: return _error(args.command, "confirmation_required")
    if args.i_confirm != args.candidate_id: return _error(args.command, "confirmation_mismatch")
    if args.actor_class != "user" or len(args.actor_identity_hash) != 64: return _error(args.command, "human_actor_required")
    if args.expected_sequence < 0: return _error(args.command, "invalid_expected_sequence")
    if not args.idempotency_key.strip(): return _error(args.command, "idempotency_key_required")
    return None


def _append_surface(args: argparse.Namespace) -> dict[str, Any]:
    # Validate the complete candidate/run/source/frontier chain before opening a write transaction.
    read = ProactiveIntelligenceService(args.db).invoke("candidates.get", candidate_id=args.candidate_id)
    if not read.get("ok"): return read
    if read["data"]["candidate_checksum"] != args.candidate_checksum: return _error("surface", "candidate_checksum_mismatch")
    con = sqlite3.connect(args.db); con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON"); con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT * FROM proactive_surface_events WHERE candidate_id=? ORDER BY sequence", (args.candidate_id,)).fetchall()
        previous = checksum({"surface_event": "genesis"})
        for index, row in enumerate(rows, 1):
            payload = json.loads(str(row["payload_json"]))
            if int(row["sequence"]) != index or str(row["previous_event_checksum"]) != previous or checksum(payload) != str(row["payload_checksum"]):
                raise ValueError("surface_chain_tampered")
            previous = str(row["payload_checksum"])
        existing = next((row for row in rows if json.loads(str(row["payload_json"])).get("idempotency_key") == args.idempotency_key and str(row["actor_identity_hash"]) == args.actor_identity_hash), None)
        identity = {"candidate_id": args.candidate_id, "candidate_checksum": args.candidate_checksum, "event_type": args.event_type, "actor_identity_hash": args.actor_identity_hash, "expected_sequence": args.expected_sequence, "idempotency_key": args.idempotency_key, "occurred_at": args.occurred_at}
        if existing:
            if canonical_json(json.loads(str(existing["payload_json"]))) != canonical_json({**identity, "event_id": str(existing["event_id"]), "sequence": int(existing["sequence"]), "previous_event_checksum": str(existing["previous_event_checksum"])}): raise ValueError("idempotency_conflict")
            con.rollback(); return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": "surface", "ok": True, "status": "existing", "receipt": dict(existing), "external_actions": 0}
        if args.expected_sequence != len(rows): raise ValueError("stale_sequence")
        sequence = len(rows) + 1; event_id = f"pse_{checksum({**identity, 'sequence': sequence, 'previous_event_checksum': previous})[:24]}"
        payload = {**identity, "event_id": event_id, "sequence": sequence, "previous_event_checksum": previous}; payload_checksum = checksum(payload)
        con.execute("INSERT INTO proactive_surface_events VALUES (?,?,?,?,?,?,?,?,?,?)", (event_id, args.candidate_id, sequence, args.event_type, "user", args.actor_identity_hash, previous, canonical_json(payload), payload_checksum, args.occurred_at))
        con.commit(); return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": "surface", "ok": True, "status": "written", "receipt": {**payload, "payload_checksum": payload_checksum}, "external_actions": 0}
    except Exception as exc:
        con.rollback(); return _error("surface", str(exc))
    finally: con.close()


def _fingerprints(db_path: Path, pointer: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        groups = {name: [_table_fingerprint(con, table) for table in tables] for name, tables in {**_FINGERPRINT_GROUPS, "decision": _DECISION_TABLES, "proactive": tuple(sorted(PROACTIVE_TABLES))}.items()}
    finally: con.close()
    pointer_hash = hashlib.sha256(pointer.read_bytes()).hexdigest() if pointer.exists() else ""
    value = {"groups": groups, "active_pointer": {"exists": pointer.exists(), "checksum": pointer_hash}}
    return {**value, "checksum": checksum(value)}


def _technical_sandbox(*, fail_stage: str | None = None) -> dict[str, Any]:
    """Execute the complete Phase 25→26→27 chain in one disposable SQLite DB."""
    def extension(*, db: Path, resolver: Any, source_run: Any, decision_run: Any,
                  accepted: Any, rejected: Any, assessment: Any) -> dict[str, Any]:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            state_row = con.execute(
                "SELECT assertion_id,payload_checksum FROM personal_state_assertions WHERE run_id=? ORDER BY assertion_id LIMIT 1",
                (source_run.run_id,),
            ).fetchone()
            state_seq = int(con.execute(
                "SELECT publication_sequence FROM personal_state_publications WHERE run_id=?", (source_run.run_id,),
            ).fetchone()[0])
        finally:
            con.close()
        state_ref = SupportReference(
            "a.personal_change", "assertion", str(state_row[0]), str(state_row[1]),
            source_run.run_id, source_run.output_manifest_checksum,
            source_run.snapshot.snapshot_id, source_run.snapshot.snapshot_hash,
        )
        decision_ref = SupportReference(
            "a.decision_feedback", "recommendation", accepted.recommendation_id,
            accepted.payload_checksum, decision_run.run_id, decision_run.run_checksum,
            decision_run.snapshot_id, decision_run.snapshot_hash,
        )
        goals = []
        for index, domain in enumerate(CANONICAL_DOMAINS):
            resources = ()
            if domain in {"career", "project"}:
                resources = (ResourceClaim(
                    "time", 7.0 if domain == "career" else 6.0, "hours",
                    "2026-07-18T00:00:00Z", "2026-07-25T00:00:00Z", 10.0,
                    state_ref, False, "calendar:fixture-week",
                ),)
            goals.append(GoalSignal(
                f"goal:{domain}", domain, "fixture-user", "personal", "target-d",
                "2026-07-18T00:00:00Z", "2026-08-01T00:00:00Z",
                "2026-07-18T00:00:00Z", .9, "fixture only", state_ref, resources,
            ))
        coordinated = coordinate_goals(goals, as_of="2026-07-18T00:00:00Z")
        candidate_drafts = tuple(CandidateDraft(
            "goal_conflict" if domain in {"career", "project"} else "cross_domain_opportunity",
            "inbox_item", "fixture-user", "personal", (domain,), (f"goal:{domain}",),
            "2026-07-18T00:00:00Z", "2026-08-01T00:00:00Z",
            (decision_ref if domain == "project" else state_ref,), .8, .8, .8, .7, .9,
            .8, .2, "fixture only", ("fixture_only",),
            sensitive=(domain == "health"),
        ) for domain in CANONICAL_DOMAINS)
        run = plan_run(
            db, coordinated.items, source_run_id=source_run.run_id,
            source_run_checksum=source_run.output_manifest_checksum,
            source_publication_sequence=state_seq, decision_run_id=decision_run.run_id,
            decision_run_checksum=decision_run.run_checksum,
            coordination_policy="coordination-v1", ranking_policy="importance-v1",
            noise_policy="noise-v1", input_manifest={"mode": "target-d-sandbox"},
            candidate_drafts=candidate_drafts, evaluation_context=EvaluationContext.fixed(),
        )
        published = publish_run(db, run, write=True)
        service = ProactiveIntelligenceService(db)
        candidate = run.candidates[0]
        target = ControlTarget("a.proactive_intelligence", "candidate", candidate.candidate_id, candidate.payload_checksum)
        actor = checksum({"user": "target-d-sandbox"})
        suppression = append_control(db, ControlCommand(
            target, "suppress", "global", "user", actor, 0, "sandbox-suppress",
            "fixture", "2026-07-18T12:00:00Z", None, None, {},
        ), write=True).event
        suppressed = service.invoke("controls.status", candidate_id=candidate.candidate_id,
                                    as_of="2026-07-18T12:00:00Z")
        restore = append_control(db, ControlCommand(
            target, "restore", "global", "user", actor, 1, "sandbox-restore",
            "fixture", "2026-07-18T13:00:00Z", None, suppression.event_id, {},
        ), write=True).event
        restored = service.invoke("controls.status", candidate_id=candidate.candidate_id,
                                  as_of="2026-07-18T13:00:00Z")
        stale_rejected = False
        try:
            append_control(db, ControlCommand(
                target, "suppress", "global", "user", actor, 1, "sandbox-stale",
                "fixture", "2026-07-18T14:00:00Z", None, None, {},
            ), write=True)
        except ControlError as exc:
            stale_rejected = exc.code == "stale_sequence"
        future = plan_run(
            db, coordinated.items, source_run_id=source_run.run_id,
            source_run_checksum=source_run.output_manifest_checksum,
            source_publication_sequence=state_seq, decision_run_id=decision_run.run_id,
            decision_run_checksum=decision_run.run_checksum,
            coordination_policy="coordination-v1", ranking_policy="importance-v1",
            noise_policy="noise-v1", input_manifest={"mode": "target-d-after-restore"},
            candidate_drafts=candidate_drafts, evaluation_context=EvaluationContext.fixed(),
        )
        future_published = publish_run(db, future, write=True)
        state_service = IntelligenceService(db, resolver=resolver)
        current = state_service.invoke("state.current", run_id=source_run.run_id)
        history = state_service.invoke("state.history", run_id=source_run.run_id)
        read = service.invoke("candidates.get", candidate_id=candidate.candidate_id)
        explain = service.invoke("candidates.explain", candidate_id=candidate.candidate_id)
        stages = {
            "capture": bool(state_row and published.get("written")),
            "state_change_history": bool(current.get("ok") and history.get("ok") and history.get("data", {}).get("total_available", 0) > 0),
            "recommendation_confirmation_action_outcome_feedback": True,
            "eight_domain_coordination": {d for item in coordinated.items for d in item.domains} == set(CANONICAL_DOMAINS),
            "ranking_and_noise": len(run.candidates) == 8 and len(run.evaluations) == 8 and any(e.result != "eligible" for e in run.evaluations),
            "trust_control_and_restore": bool(suppressed.get("ok") and not suppressed.get("data", {}).get("eligible") and restored.get("ok") and restored.get("data", {}).get("eligible") and restore.rollback_of_event_id == suppression.event_id),
            "future_run_after_restore": bool(future_published.get("written") and future.control_frontier_checksum != run.control_frontier_checksum),
            "shared_read_explain": bool(read.get("ok") and explain.get("ok")),
        }
        if fail_stage is not None:
            if fail_stage not in stages:
                raise ValueError(f"unknown_fail_stage:{fail_stage}")
            stages[fail_stage] = False
        counts: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for evaluation in run.evaluations:
            counts[evaluation.result] = counts.get(evaluation.result, 0) + 1
            for reason in evaluation.reason_codes:
                reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "ok": all(stages.values()), "stage_results": stages,
            "domain_counts": {domain: 1 for domain in CANONICAL_DOMAINS},
            "candidate_counts": counts, "suppression_reason_counts": reasons,
            "control_and_rollback_results": {
                "suppressed": not suppressed.get("data", {}).get("eligible", True),
                "restored": restored.get("data", {}).get("eligible", False),
                "stale_append_rejected": stale_rejected, "history_erased": False,
            },
            "proactive_run_id": run.run_id, "future_run_id": future.run_id,
        }

    decision = _decision_sandbox(extension)
    proactive = dict(decision.get("extension") or {})
    stages = dict(proactive.get("stage_results") or {})
    stages["recommendation_confirmation_action_outcome_feedback"] = bool(decision.get("ok"))
    if fail_stage == "recommendation_confirmation_action_outcome_feedback":
        stages["recommendation_confirmation_action_outcome_feedback"] = False
    return {
        "ok": bool(stages and all(stages.values())), "disposable_sqlite": True,
        "stage_results": stages, "domain_counts": proactive.get("domain_counts", {}),
        "candidate_counts": proactive.get("candidate_counts", {}),
        "suppression_reason_counts": proactive.get("suppression_reason_counts", {}),
        "control_and_rollback_results": proactive.get("control_and_rollback_results", {}),
        "decision": {key: value for key, value in decision.items() if key != "extension"},
        "fixture_only": True, "external_actions": 0, "network_calls": 0, "paid_calls": 0,
    }


def _validate_applied_schemas(path: Path, *, snapshot_id: str | None,
                              snapshot_hash: str | None) -> dict[str, dict[str, Any]]:
    """Validate every applied analysis authority rather than treating table presence as proof."""
    gates: dict[str, dict[str, Any]] = {}
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        state_rows = con.execute(
            "SELECT run_id,snapshot_id,snapshot_hash FROM personal_state_runs WHERE status='committed' ORDER BY run_id"
        ).fetchall()
        if not state_rows:
            gates["phase25"] = {"ok": False, "reason": "personal_state_no_committed_run"}
        else:
            service = IntelligenceService(path)
            results = [service.invoke("state.current", run_id=str(row["run_id"])) for row in state_rows]
            binding_ok = all(str(row["snapshot_id"]) == snapshot_id and str(row["snapshot_hash"]) == snapshot_hash for row in state_rows)
            gates["phase25"] = {"ok": binding_ok and all(item.get("ok") for item in results),
                                "reason": "validated_committed_runs" if binding_ok and all(item.get("ok") for item in results) else "personal_state_binding_invalid",
                                "run_count": len(state_rows)}

        decision_rows = con.execute(
            "SELECT run_id,snapshot_id,snapshot_hash FROM decision_runs WHERE status='committed' ORDER BY run_id"
        ).fetchall()
        if not decision_rows:
            gates["phase26"] = {"ok": False, "reason": "decision_no_committed_run"}
        else:
            service = DecisionFeedbackService(path)
            results = [service.recommendations_list(limit=100) for _ in (0,)]
            binding_ok = all(str(row["snapshot_id"]) == snapshot_id and str(row["snapshot_hash"]) == snapshot_hash for row in decision_rows)
            gates["phase26"] = {"ok": binding_ok and all(item.get("ok") for item in results),
                                "reason": "validated_committed_runs" if binding_ok and all(item.get("ok") for item in results) else "decision_binding_invalid",
                                "run_count": len(decision_rows)}

        proactive_rows = con.execute(
            "SELECT run_id,snapshot_id,snapshot_hash FROM proactive_runs WHERE status='committed' ORDER BY run_id"
        ).fetchall()
        if not proactive_rows:
            gates["phase27"] = {"ok": False, "reason": "proactive_no_committed_run"}
        else:
            service = ProactiveIntelligenceService(path)
            valid = True
            reason = "validated_committed_runs"
            for row in proactive_rows:
                if str(row["snapshot_id"]) != snapshot_id or str(row["snapshot_hash"]) != snapshot_hash:
                    valid, reason = False, "proactive_snapshot_mismatch"
                    break
                try:
                    _, output = service._run(con, str(row["run_id"]))
                    expected_coordination = {str(item["coordination_id"]): str(item["payload_checksum"])
                                             for item in output.get("coordination_items", ())}
                    actual_coordination = {str(item[0]): str(item[1]) for item in con.execute(
                        "SELECT coordination_id,payload_checksum FROM proactive_coordination_items WHERE run_id=?",
                        (row["run_id"],),
                    )}
                    if actual_coordination != expected_coordination:
                        raise ValueError("coordination_manifest_mismatch")
                    expected_candidates = {str(item["candidate_id"]): str(item["payload_checksum"])
                                           for item in output.get("candidates", ())}
                    actual_candidates = {str(item[0]): str(item[1]) for item in con.execute(
                        "SELECT candidate_id,payload_checksum FROM proactive_candidates WHERE run_id=?",
                        (row["run_id"],),
                    )}
                    if actual_candidates != expected_candidates:
                        raise ValueError("candidate_manifest_mismatch")
                    for candidate_id in expected_candidates:
                        service._candidate(con, candidate_id)
                except Exception as exc:
                    valid, reason = False, f"proactive_integrity_invalid:{exc}"
                    break
            gates["phase27"] = {"ok": valid, "reason": reason, "run_count": len(proactive_rows)}
    finally:
        con.close()
    return gates


def run_acceptance(db_path: Path | str, *, pointer_path: Path = KNOWLEDGE_ACTIVE_POINTER) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists(): return _error("acceptance", "database_missing", str(path))
    before = _fingerprints(path, pointer_path); sandbox = _technical_sandbox(); proactive = before["groups"]["proactive"]
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        authority = con.execute("SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1").fetchone()
        snapshot_id = str(authority[0]) if authority and authority[0] else None
        snapshot = con.execute("SELECT manifest_hash FROM serving_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone() if snapshot_id else None
        snapshot_hash = str(snapshot[0]) if snapshot else None
    finally: con.close()
    def schema_state(group: str) -> str:
        rows = before["groups"][group]; present = [row for row in rows if row["exists"]]
        return "applied" if len(present) == len(rows) else "unapplied" if not present else "partial"
    existing, missing = [r["table"] for r in proactive if r["exists"]], [r["table"] for r in proactive if not r["exists"]]
    state = "applied" if not missing else "unapplied" if not existing else "partial"
    gate: dict[str, Any] = {"ok": state == "unapplied", "reason": f"proactive_schema_{state}", "existing_tables": existing, "missing_tables": missing, "count": 0}
    after = _fingerprints(path, pointer_path); unchanged = before == after; phase24 = _phase24_dependency_status(path)
    upstream_states = {"phase25": schema_state("analysis"), "phase26": schema_state("decision"), "phase27": state}
    applied_gates: dict[str, dict[str, Any]] = {}
    if any(value == "applied" for value in upstream_states.values()):
        try:
            applied_gates = _validate_applied_schemas(path, snapshot_id=snapshot_id, snapshot_hash=snapshot_hash)
        except (sqlite3.Error, ValueError) as exc:
            applied_gates = {name: {"ok": False, "reason": f"validation_error:{exc}"}
                             for name, value in upstream_states.items() if value == "applied"}
    for name, value in upstream_states.items():
        if value == "unapplied":
            applied_gates[name] = {"ok": True, "reason": f"{name}_schema_unapplied"}
        elif value == "partial":
            applied_gates[name] = {"ok": False, "reason": f"{name}_schema_partial"}
    gate = {**gate, **applied_gates.get("phase27", {})}
    technical_blockers = ([] if sandbox["ok"] else ["sandbox:failed"]) + ([f"{name}:{item['reason']}" for name, item in applied_gates.items() if not item.get("ok")]) + ([] if unchanged else ["fingerprints:changed"])
    technical_ok = not technical_blockers
    # Product release additionally requires strict review, lifecycle, final gate and explicit product UAT.
    checkpoint_statuses = {str(c["checkpoint"]): str(c["status"]) for c in phase24.get("checkpoints", ())}
    final_gate = all(status in {"pass", "passed", "complete", "completed"} for status in checkpoint_statuses.values())
    product_uat = _product_uat_status()
    explicit_uat = bool(product_uat["accepted"])
    release_ready = technical_ok and bool(phase24["human_review_strict"]["ok"]) and bool(phase24["lifecycle_strict"]["ok"]) and final_gate and explicit_uat
    return {"schema_version": "target_d_acceptance_v1", "operation": "acceptance", "ok": technical_ok, "technical_status": "passed" if technical_ok else "failed", "release_status": "release_ready" if release_ready else "release_blocked", "release_ready": release_ready, "release_blockers": {"technical": technical_blockers, "phase24": list(phase24.get("reason_codes") or []) + ([] if explicit_uat else ["product_uat:missing"])}, "product_uat": product_uat, "dry_run": True, "metadata_only": True, "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash, "phase25_binding": {"schema_state": upstream_states["phase25"], **applied_gates.get("phase25", {})}, "phase26_binding": {"schema_state": upstream_states["phase26"], **applied_gates.get("phase26", {})}, "phase27_schema_state": state, "sandbox": sandbox, "candidate_counts": sandbox["candidate_counts"], "suppression_reason_counts": sandbox["suppression_reason_counts"], "domain_counts": sandbox["domain_counts"], "control_and_rollback_results": sandbox["control_and_rollback_results"], "live": {"phase25_schema_state": upstream_states["phase25"], "phase26_schema_state": upstream_states["phase26"], "proactive_schema_state": state, "proactive_status": gate, "applied_validation": applied_gates, "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash}, "fingerprints": {"before": before, "after": after, "unchanged": unchanged}, "before_fingerprint": before["checksum"], "after_fingerprint": after["checksum"], "unchanged": unchanged, "phase24": phase24, "persisted_rows": 0, "mutations": 0 if unchanged else 1, "private_bodies": 0, "external_actions": 0, "network_calls": 0, "paid_calls": 0}


def _invoke(args: argparse.Namespace) -> dict[str, Any]:
    service = ProactiveIntelligenceService(args.db)
    operations = {"inbox": "inbox.list", "digest": "digest.get", "get": "candidates.get", "explain": "candidates.explain", "controls-status": "controls.status", "metrics": "metrics.get"}
    if args.command in operations:
        values = vars(args).copy()
        for key in ("db", "command", "json"): values.pop(key, None)
        # Preserve service defaults for optional CLI arguments. Passing an
        # explicit None would override controls_status' open-ended as_of value.
        values = {key: value for key, value in values.items() if value is not None}
        return service.invoke(operations[args.command], **values)
    if args.command == "acceptance":
        if not args.dry_run or not args.metadata_only: return _error("acceptance", "dry_run_metadata_only_required")
        return run_acceptance(args.db, pointer_path=args.active_pointer)
    blocked = _guard(args)
    if blocked: return blocked
    if args.command == "surface": return _append_surface(args)
    try:
        read = service.invoke("candidates.get", candidate_id=args.candidate_id)
        if not read.get("ok"): return read
        if read["data"]["candidate_checksum"] != args.candidate_checksum:
            return _error("control", "candidate_checksum_mismatch")
        command = ControlCommand(ControlTarget("a.proactive_intelligence", "candidate", args.candidate_id, args.candidate_checksum), args.operation, args.scope, args.actor_class, args.actor_identity_hash, args.expected_sequence, args.idempotency_key, args.reason_code, args.created_at, args.expires_at, args.rollback_of_event_id, json.loads(args.details_json))
        receipt = append_control(args.db, command, write=True)
        return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": "control", "ok": True, "status": "written" if receipt.written else "existing", "receipt": asdict(receipt), "privacy": {"metadata_only": True, "private_bodies": 0}, "external_actions": 0}
    except ControlError as exc: return _error("control", exc.code, exc.detail)
    except (json.JSONDecodeError, TypeError, ValueError) as exc: return _error("control", "invalid_write_arguments", str(exc))


def main(argv: list[str] | None = None) -> int:
    result = _invoke(build_parser().parse_args(argv)); print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str)); return 0 if result.get("ok") else 2


if __name__ == "__main__": raise SystemExit(main())
