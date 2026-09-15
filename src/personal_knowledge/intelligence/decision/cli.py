"""Local CLI for decision reads and explicitly confirmed append-only writes."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable, Mapping

from personal_knowledge.core.lifecycle import ensure_lifecycle_schema
from personal_knowledge.core.project_paths import KNOWLEDGE_ACTIVE_POINTER, UNIFIED_DB
from personal_knowledge.core.schema_ddl import SCHEMA_SQL
from personal_knowledge.intelligence.cli import (
    _FINGERPRINT_GROUPS,
    _phase24_dependency_status,
    _table_fingerprint,
    run_acceptance as run_personal_state_acceptance,
)
from personal_knowledge.intelligence.runs import plan_run as plan_state_run
from personal_knowledge.intelligence.runs import publish_run as publish_state_run
from personal_knowledge.intelligence.schema import EvidenceReference, StateAssertion, checksum

from .effectiveness import EffectivenessRule, assess_outcome, load_outcome
from .runs import plan_run as plan_decision_run
from .runs import publish_run as publish_decision_run
from .runs import resolve_cognition_reference
from .schema import RecommendationDraft
from .service import DecisionFeedbackService, INTERFACE_SCHEMA_VERSION
from .state_machine import (
    DecisionStateError,
    project_history,
    record_action,
    record_assessment,
    record_confirmation,
    record_outcome,
)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pk-decision")
    parser.add_argument("--db", type=Path, default=UNIFIED_DB)
    commands = parser.add_subparsers(dest="command", required=True)

    recommendations = commands.add_parser("recommendations")
    reads = recommendations.add_subparsers(dest="read_command", required=True)
    listing = reads.add_parser("list")
    listing.add_argument("--domain")
    listing.add_argument("--limit", type=int, default=50)
    _common(listing)
    for name in ("get", "history", "outcomes", "effectiveness"):
        item = reads.add_parser(name)
        item.add_argument("--recommendation-id", required=True)
        if name != "get":
            item.add_argument("--limit", type=int, default=50)
        _common(item)

    for name in ("confirm", "action", "outcome"):
        write = commands.add_parser(name)
        write.add_argument("--recommendation-id", required=True)
        write.add_argument("--recommendation-checksum", required=True)
        write.add_argument("--write", action="store_true")
        write.add_argument("--i-confirm")
        write.add_argument("--actor-class", required=True)
        write.add_argument("--actor-identity-hash", required=True)
        write.add_argument("--expected-sequence", type=int, required=True)
        write.add_argument("--idempotency-key", required=True)
        write.add_argument("--occurred-at", required=True)
        _common(write)
        if name == "confirm":
            write.add_argument("--decision", required=True, choices=("accept", "reject", "defer", "revoke_before_action"))
            write.add_argument("--reason-code", required=True)
        elif name == "action":
            write.add_argument("--action-state", required=True, choices=("planned", "started", "completed", "abandoned", "not_taken"))
            write.add_argument("--source-class", default="user_attested", choices=("user_attested", "user_external_ref"))
            write.add_argument("--reason-code", required=True)
            write.add_argument("--external-ref-checksum")
        else:
            write.add_argument("--action-id", required=True)
            write.add_argument("--action-checksum", required=True)
            write.add_argument("--source-class", required=True, choices=("user_reported", "evidence_measured"))
            write.add_argument("--measurement-definition", required=True)
            write.add_argument("--metric", required=True)
            write.add_argument("--baseline-value", type=float)
            write.add_argument("--target-value", type=float)
            write.add_argument("--observed-value", type=float)
            write.add_argument("--unit", required=True)
            write.add_argument("--direction", required=True, choices=("increase", "decrease", "maintain"))
            write.add_argument("--window-start", required=True)
            write.add_argument("--window-end", required=True)
            write.add_argument("--adherence-status", required=True, choices=("adhered", "non_adherent", "unknown"))
            write.add_argument("--confidence", required=True, type=float)
            write.add_argument("--evidence-ref-json", action="append", default=[])
            write.add_argument("--uncertainty", action="append", default=[])
            write.add_argument("--confounder", action="append", default=[])
            write.add_argument("--concurrent-action", action="append", default=[])

    acceptance = commands.add_parser("acceptance")
    acceptance.add_argument("--dry-run", action="store_true")
    acceptance.add_argument("--metadata-only", action="store_true")
    acceptance.add_argument("--active-pointer", type=Path, default=KNOWLEDGE_ACTIVE_POINTER)
    _common(acceptance)
    return parser


def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
    return DecisionFeedbackService._error(operation, code, detail)


def _guard(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.write:
        return _error(args.command, "write_required")
    if not args.i_confirm:
        return _error(args.command, "confirmation_required")
    if args.i_confirm != args.recommendation_id:
        return _error(args.command, "confirmation_mismatch")
    if args.actor_class != "user" or len(args.actor_identity_hash) != 64:
        return _error(args.command, "human_actor_required")
    if args.expected_sequence < 1:
        return _error(args.command, "invalid_expected_sequence")
    if not args.idempotency_key.strip():
        return _error(args.command, "idempotency_key_required")
    return None


def _receipt(operation: str, value: Any) -> dict[str, Any]:
    return {
        "schema_version": INTERFACE_SCHEMA_VERSION,
        "operation": operation,
        "ok": True,
        "status": "written",
        "receipt": asdict(value),
        "privacy": {"metadata_only": True, "private_bodies": 0},
        "external_actions": 0,
    }


def _invoke(args: argparse.Namespace) -> dict[str, Any]:
    service = DecisionFeedbackService(args.db)
    if args.command == "recommendations":
        operation = f"recommendations.{args.read_command}"
        params = vars(args).copy()
        for key in ("db", "command", "read_command", "json"):
            params.pop(key, None)
        return service.invoke(operation, **params)
    if args.command == "acceptance":
        if not args.dry_run or not args.metadata_only:
            return _error("acceptance", "dry_run_metadata_only_required")
        return run_acceptance(args.db, pointer_path=args.active_pointer)
    blocked = _guard(args)
    if blocked:
        return blocked
    try:
        common = dict(
            recommendation_id=args.recommendation_id,
            recommendation_checksum=args.recommendation_checksum,
            actor_class=args.actor_class,
            actor_identity_hash=args.actor_identity_hash,
            expected_sequence=args.expected_sequence,
            idempotency_key=args.idempotency_key,
            occurred_at=args.occurred_at,
        )
        if args.command == "confirm":
            value = record_confirmation(
                args.db, **common, decision=args.decision, reason_code=args.reason_code
            )
        elif args.command == "action":
            value = record_action(
                args.db, **common, action_state=args.action_state,
                source_class=args.source_class, reason_code=args.reason_code,
                external_ref_checksum=args.external_ref_checksum,
            )
        else:
            refs = tuple(json.loads(item) for item in args.evidence_ref_json)
            value = record_outcome(
                args.db, **common, action_id=args.action_id,
                action_checksum=args.action_checksum, source_class=args.source_class,
                measurement_definition=args.measurement_definition, metric=args.metric,
                baseline_value=args.baseline_value, target_value=args.target_value,
                observed_value=args.observed_value, unit=args.unit,
                direction=args.direction, window_start=args.window_start,
                window_end=args.window_end, adherence_status=args.adherence_status,
                evidence_refs=refs, confidence=args.confidence,
                uncertainty=tuple(args.uncertainty), confounders=tuple(args.confounder),
                concurrent_actions=tuple(args.concurrent_action),
            )
        return _receipt(args.command, value)
    except DecisionStateError as exc:
        return _error(args.command, exc.code, exc.detail)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _error(args.command, "invalid_write_arguments", str(exc))


class _SandboxResolver:
    def resolve(self, ref: str, **_: Any) -> dict[str, Any]:
        return {
            "ref": ref,
            "artifact_type": "knowledge_unit",
            "status": "ok",
            "eligible": True,
            "metadata": {"privacy_class": "R4"},
            "evidence_refs": [],
            "content": None,
        }


def _sandbox_loop(extension: Callable[..., Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Exercise writes only in a disposable database and return checksums/counts."""
    with tempfile.TemporaryDirectory(prefix="phase26-acceptance-") as temp:
        db = Path(temp) / "decision.sqlite"
        con = sqlite3.connect(db)
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA_SQL)
        ensure_lifecycle_schema(con)
        for row in (
            ("a.personal_change", "A", "personal_change_analysis", "R4", "a", "now"),
            ("a.decision_feedback", "A", "decision_feedback", "R4", "d", "now"),
            ("s.knowledge_unit", "S", "canonical_knowledge", "R4", "s", "now"),
        ):
            con.execute("INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)", row)
        con.execute(
            "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("av1", "s.knowledge_unit", "v1", "source", "sqlite_table", "canonical_knowledge_units",
             "validated", "R4", None, None, "{}", "now"),
        )
        con.execute(
            "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
            ("ss1", "{}", "snapshot-hash", "validated", "gate", "now", "now"),
        )
        con.execute(
            "INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)",
            ("ss1", "canonical_knowledge", "av1"),
        )
        con.execute(
            "UPDATE serving_authority SET active_snapshot_id='ss1',activated_at='now' WHERE singleton_id=1"
        )
        con.commit(); con.close()

        resolver = _SandboxResolver()
        source = plan_state_run(
            db,
            [StateAssertion(
                assertion_kind="goal", provenance_class="fact", subject="user",
                domain="work", scope="personal", predicate="complete_target",
                value="D", valid_from="2026-07-18T00:00:00Z",
                observed_at="2026-07-18T00:00:00Z",
                evidence=(EvidenceReference(
                    ref="ku1", artifact_type="knowledge_unit",
                    serving_role="canonical_knowledge", artifact_version_id="av1",
                    privacy_class="R4",
                ),),
            )],
            producer_version="phase25-v1",
            input_manifest={"source": "phase26-acceptance"},
            resolver=resolver,
        )
        publish_state_run(db, source, write=True, resolver=resolver)
        ref = resolve_cognition_reference(
            db, source_run_id=source.run_id, record_id=None, cognitive_type="fact"
        )
        drafts = tuple(
            RecommendationDraft(
                subject="user", domain="work", scope="personal",
                recommendation_kind=kind, target=target, horizon="next_session",
                rationale_codes=("goal_gap",), expected_benefit="bounded progress",
                costs_constraints=("human gates remain",), assumptions=("source valid",),
                contraindications=(), confidence=.8, uncertainty="observational only",
                expires_at="2026-08-01T00:00:00Z", support=(ref,),
            )
            for kind, target in (("next_step", "close_target_d"), ("alternative", "pause"))
        )
        run = plan_decision_run(
            db, drafts, policy_id="bounded-next-step", policy_version="v1",
            input_manifest={"mode": "sandbox"},
        )
        publish_decision_run(db, run, write=True)
        accepted, rejected = run.recommendations
        actor = "1" * 64
        record_confirmation(
            db, recommendation_id=accepted.recommendation_id,
            recommendation_checksum=accepted.payload_checksum, decision="accept",
            actor_class="user", actor_identity_hash=actor, reason_code="selected",
            expected_sequence=1, idempotency_key="confirm-accept",
            occurred_at="2026-07-18T01:00:00Z",
        )
        for sequence, state in ((2, "planned"), (3, "started"), (4, "completed")):
            record_action(
                db, recommendation_id=accepted.recommendation_id,
                recommendation_checksum=accepted.payload_checksum,
                action_state=state, source_class="user_attested", actor_class="user",
                actor_identity_hash=actor, reason_code=f"user_{state}",
                expected_sequence=sequence, idempotency_key=f"action-{state}",
                occurred_at="2026-07-18T01:01:00Z",
            )
        con = sqlite3.connect(db)
        action_id, action_checksum = con.execute(
            "SELECT action_id,payload_checksum FROM decision_actions WHERE action_state='completed'"
        ).fetchone()
        con.close()
        evidence = ({
            "cognitive_type": ref.cognitive_type, "authority_id": ref.authority_id,
            "record_id": ref.record_id, "record_checksum": ref.record_checksum,
            "source_run_id": ref.source_run_id, "snapshot_id": ref.snapshot_id,
            "snapshot_hash": ref.snapshot_hash,
        },)
        outcome_receipt = record_outcome(
            db, recommendation_id=accepted.recommendation_id,
            recommendation_checksum=accepted.payload_checksum, action_id=action_id,
            action_checksum=action_checksum, source_class="user_reported",
            actor_class="user", actor_identity_hash=actor,
            measurement_definition="bounded progress count", metric="progress",
            baseline_value=1.0, target_value=2.0, observed_value=2.0, unit="count",
            direction="increase", window_start="2026-07-18T01:00:00Z",
            window_end="2026-07-25T01:00:00Z", adherence_status="adhered",
            evidence_refs=evidence, confidence=.8, uncertainty=(), confounders=(),
            concurrent_actions=(), expected_sequence=5, idempotency_key="outcome",
            occurred_at="2026-07-25T01:01:00Z",
        )
        outcome = load_outcome(db, outcome_receipt.record_id)
        assessment = assess_outcome(
            outcome, EffectivenessRule("goal-attainment", "1", "progress", "count", "increase", 86400),
            action_state="completed",
        )
        record_assessment(
            db, assessment=assessment, expected_sequence=6,
            idempotency_key="assessment", occurred_at="2026-07-25T01:02:00Z",
        )
        record_confirmation(
            db, recommendation_id=rejected.recommendation_id,
            recommendation_checksum=rejected.payload_checksum, decision="reject",
            actor_class="user", actor_identity_hash=actor, reason_code="not_selected",
            expected_sequence=1, idempotency_key="confirm-reject",
            occurred_at="2026-07-18T01:00:00Z",
        )
        accepted_state = project_history(db, accepted.recommendation_id)
        rejected_state = project_history(db, rejected.recommendation_id)
        result = {
            "ok": (
                [event.sequence for event in accepted_state.events] == list(range(1, 8))
                and accepted_state.confirmation_state == "accepted"
                and accepted_state.action_state == "completed"
                and assessment.causal_claim is False
                and rejected_state.confirmation_state == "rejected"
                and rejected_state.action_state is None
            ),
            "decision_run_id": run.run_id,
            "decision_run_checksum": run.run_checksum,
            "accepted_history_length": len(accepted_state.events),
            "rejected_history_length": len(rejected_state.events),
            "assessment_verdict": assessment.verdict,
            "causal_claim": assessment.causal_claim,
            "external_actions": 0,
        }
        if extension is not None:
            result["extension"] = dict(extension(
                db=db, resolver=resolver, source_run=source, decision_run=run,
                accepted=accepted, rejected=rejected, assessment=assessment,
            ))
            result["ok"] = bool(result["ok"] and result["extension"].get("ok"))
        return result


_DECISION_TABLES = (
    "decision_runs", "decision_recommendations", "decision_support_refs",
    "decision_confirmations", "decision_actions", "decision_outcomes",
    "decision_effectiveness", "decision_events",
)


def _live_fingerprints(db_path: Path, pointer_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        groups = {
            group: [_table_fingerprint(con, table) for table in tables]
            for group, tables in {**_FINGERPRINT_GROUPS, "decision": _DECISION_TABLES}.items()
        }
    finally:
        con.close()
    pointer_checksum = hashlib.sha256(pointer_path.read_bytes()).hexdigest() if pointer_path.exists() else ""
    value = {"groups": groups, "active_pointer": {"exists": pointer_path.exists(), "checksum": pointer_checksum}}
    return {**value, "checksum": checksum(value)}


def run_acceptance(
    db_path: Path | str,
    *,
    pointer_path: Path = KNOWLEDGE_ACTIVE_POINTER,
) -> dict[str, Any]:
    """Sandbox the loop, then inspect live metadata with zero write-capable calls."""
    path = Path(db_path)
    if not path.exists():
        return _error("acceptance", "database_missing", str(path))
    before = _live_fingerprints(path, pointer_path)
    sandbox = _sandbox_loop()
    phase25 = run_personal_state_acceptance(path, pointer_path=pointer_path, limit=10)
    decision_tables = before["groups"]["decision"]
    existing_tables = [str(item["table"]) for item in decision_tables if item["exists"]]
    missing_tables = [str(item["table"]) for item in decision_tables if not item["exists"]]
    decision_ready = not missing_tables
    decision_unapplied = not existing_tables
    decision_nonempty = any(item["row_count"] for item in decision_tables)
    decision_gate: dict[str, Any] = {
        "ok": decision_unapplied,
        "reason": "decision_schema_unapplied" if decision_unapplied else "decision_schema_partial",
        "count": 0,
        "existing_tables": existing_tables,
        "missing_tables": missing_tables,
    }
    if decision_ready:
        listing = DecisionFeedbackService(path).invoke("recommendations.list", limit=10)
        decision_gate = {
            "ok": bool(listing.get("ok")),
            "reason": "bounded_committed_decision_replay" if listing.get("ok") else str(listing.get("error", {}).get("code")),
            "count": int(listing.get("data", {}).get("total_available") or 0),
            "error": listing.get("error"),
            "existing_tables": existing_tables,
            "missing_tables": [],
        }
    source_reason = str(phase25.get("run_plan", {}).get("candidate_reason") or "source_analysis_unavailable")
    if source_reason in {"analysis_schema_unapplied", "run_missing"}:
        source_reason = "source_analysis_unavailable"
    after = _live_fingerprints(path, pointer_path)
    unchanged = before == after
    phase24 = _phase24_dependency_status(path)
    technical_ok = bool(sandbox["ok"] and phase25.get("ok") and decision_gate["ok"] and unchanged)
    technical_blockers: list[str] = []
    if not sandbox["ok"]:
        technical_blockers.append("sandbox:failed")
    if not phase25.get("ok"):
        technical_blockers.append("phase25:failed")
    if not decision_gate["ok"]:
        technical_blockers.append(f"decision:{decision_gate['reason']}")
    if not unchanged:
        technical_blockers.append("fingerprints:changed")
    phase24_blockers = list(phase24.get("reason_codes") or [])
    release_ready = technical_ok and not phase24["release_blocked"]
    return {
        "schema_version": "decision_feedback_acceptance_v1",
        "operation": "acceptance",
        "ok": technical_ok,
        "technical_status": "passed" if technical_ok else "failed",
        "release_status": "release_ready" if release_ready else "release_blocked",
        "release_blockers": {
            "technical": technical_blockers,
            "phase24": phase24_blockers,
        },
        "dry_run": True,
        "metadata_only": True,
        "sandbox": sandbox,
        "live": {
            "source_status": source_reason,
            "decision_status": decision_gate,
            "decision_schema_applied": decision_ready,
            "decision_schema_state": (
                "applied" if decision_ready else "unapplied" if decision_unapplied else "partial"
            ),
            "decision_rows_present": decision_nonempty,
        },
        "fingerprints": {"before": before, "after": after, "unchanged": unchanged},
        "phase24": phase24,
        "persisted_rows": 0,
        "mutations": 0 if unchanged else 1,
        "private_bodies": 0,
        "external_actions": 0,
        "network_calls": 0,
        "paid_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = _invoke(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
