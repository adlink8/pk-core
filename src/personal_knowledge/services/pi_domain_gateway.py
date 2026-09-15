"""Typed, loopback-only bridge from Pi tools to the existing Python authority."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from personal_knowledge.core.project_paths import ROOT
from personal_knowledge.services.capability_registry import load_registry, operations_for_profile
from personal_knowledge.services.evidence_sqlite_tool import (
    EVIDENCE_SQLITE_OPERATION,
    LEASE_SKILL_ID,
    PRIVACY_CEILING,
    EvidenceSqliteTool,
    knowledge_research_checksum,
)
from personal_knowledge.services.orchestration_service import GuardedOrchestrationInterface
from personal_knowledge.services.warehouse_mutations import MUTATION_OPERATIONS, WarehouseOperationLedger
from personal_knowledge.services.warehouse_tools import OPERATIONS as WAREHOUSE_READ_OPERATIONS, WarehouseTools
from personal_knowledge.services.semantic_maintenance_tools import OPERATIONS as SEMANTIC_OPERATIONS, SemanticMaintenanceTools
from personal_knowledge.services.retrieval_maintenance_tools import OPERATIONS as RETRIEVAL_OPERATIONS, RetrievalMaintenanceTools
from personal_knowledge.services.snapshot_release_tools import OPERATIONS as SNAPSHOT_OPERATIONS, SnapshotReleaseTools

PI_DOMAIN_GATEWAY_SCHEMA = "pi_domain_gateway_v1"
PI_DOMAIN_CAPABILITY_HEADER = "X-PI-Domain-Capability"
DEFAULT_CAPABILITY = "pi-domain-local-capability-v1"

# Python-owned canonical conversation navigation: schema-bound read-only
# providers (Plan 61-05). They expose only safe scope/history metadata, never
# canonical bodies; they are read-only by construction and carry no
# canonical/promotion operation.
CONVERSATION_READ_OPERATIONS: dict[str, dict[str, Any]] = {
    "conversation.thread.last": {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding", "limit"},
        "privacy": "R1",
    },
    "conversation.thread.recent": {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding", "limit", "cursor"},
        "privacy": "R1",
    },
    "conversation.thread.select": {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding", "conversation_id", "limit", "after"},
        "privacy": "R1",
    },
    "conversation.project_scopes.list": {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding"},
        "privacy": "R1",
    },
    "conversation.project_scope.select": {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding", "project_scope_id", "limit", "after"},
        "privacy": "R1",
    },
}

# Plan 61-07: dispatcher-bound reflection staging (guarded write). The provider
# accepts only the Plan 61-06 dispatcher-authenticated binding metadata (event
# identity + canonical checksum/watermark + source/snapshot/two-freshness/rule
# version + task/idempotency/binding); private payload fields can never enter.
REFLECTION_STAGE_OPERATION = "conversation.reflection.stage"

# Default metadata-only reflection ledger used by the gateway-provider path.
DEFAULT_REFLECTION_DB = ROOT / "var" / "db" / "conversation_reflection.sqlite"

# Plan 61-08: the fixed guarded Candidate review provider (HARNESS-06). The
# exact request shape is {candidate_id, action, expected_version,
# edited_payload?, edited_payload_checksum?, explicit_confirmation?,
# confirmation_token?, conflict_disposition?, feedback_id?, task_id, binding,
# idempotency_key}; ``capability`` is the loopback header and never a declared
# parameter. Private payload fields, batch inputs and provider/operation/
# authority overrides can never enter this map.
CANDIDATE_REVIEW_OPERATION = "candidate.review"

# Default metadata-only review ledger used by the gateway-provider path.
DEFAULT_CANDIDATE_REVIEW_DB = ROOT / "var" / "db" / "candidate_review.sqlite"

# Plan 61-09: the fixed read-only personal-model projection provider
# (HARNESS-07). The exact request shape is {scope, task_id, binding,
# idempotency_key}; ``capability`` is the loopback header and never a declared
# parameter. Private/override fields, provider/operation/endpoint/path/authority
# overrides can never enter this map. The provider derives ONLY from confirmed
# accepted review state (D-19-D-22, D-26, D-28-D-29).
PROJECTION_GET_OPERATION = "personal.model_projection.get"

# Plan 61-10: the four fixed deterministic proactive presentation providers
# (HARNESS-05 / D-23-D-25). State is a read; controls, dismiss and undo are
# guarded_writes. Each provider accepts only its exact declared request
# vocabulary (``capability`` is the loopback transport header and never a
# declared field); endpoint/path/provider/authority overrides plus
# learned-scheduling/permission/personal-value/canonical command fields can
# never enter these maps. The provider branches call only the deterministic
# Plan 61-07 adapter after validating scope/category/quiet-hour/item-identity
# and normalize the no-store metadata-only envelope (T-61-PROACTIVE-02/-03).
PROACTIVE_STATE_OPERATION = "proactive.state.get"
PROACTIVE_CONTROLS_OPERATION = "proactive.controls.update"
PROACTIVE_DISMISS_OPERATION = "proactive.dismiss"
PROACTIVE_UNDO_OPERATION = "proactive.dismiss.undo"

_PROJECT_SCOPE_RE = re.compile(r"^project:[A-Za-z0-9][A-Za-z0-9._:/@#-]{0,255}$")
_QUIET_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _proactive_scope_ok(scope: Any) -> bool:
    """Scope is exactly ``global`` or an approved ``project:`` identifier shape."""
    return scope == "global" or (isinstance(scope, str) and bool(_PROJECT_SCOPE_RE.match(scope)))


def _proactive_quiet_hours_ok(quiet_hours: Any) -> bool:
    """Quiet hours are a mapping with HH:MM start/end whenever enabled."""
    if quiet_hours is None:
        return True
    if not isinstance(quiet_hours, Mapping):
        return False
    if not quiet_hours.get("enabled"):
        return True
    start, end = quiet_hours.get("start"), quiet_hours.get("end")
    return (
        isinstance(start, str) and isinstance(end, str)
        and bool(_QUIET_TIME_RE.match(start)) and bool(_QUIET_TIME_RE.match(end))
    )


def _proactive_feedback_id(operation: str, scope: Any, idempotency_key: Any) -> str:
    """Deterministic append-only feedback id for a no-store metadata envelope."""
    seed = json.dumps(
        {"operation": operation, "scope": scope, "idempotency_key": idempotency_key},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "feedback_proactive_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


PROACTIVE_OPERATIONS: dict[str, dict[str, Any]] = {
    PROACTIVE_STATE_OPERATION: {
        "kind": "read",
        "allowed": {
            "scope", "events", "controls", "quiet_hours", "now", "manual_order",
            "task_id", "idempotency_key", "binding",
        },
        "privacy": "R2",
    },
    PROACTIVE_CONTROLS_OPERATION: {
        "kind": "guarded_write",
        "allowed": {
            "scope", "category", "enabled", "quiet_hours",
            "task_id", "idempotency_key", "binding",
        },
        "privacy": "R2",
    },
    PROACTIVE_DISMISS_OPERATION: {
        "kind": "guarded_write",
        "allowed": {
            "cluster_key", "feedback_id", "actor_identity_hash", "now", "feedback_log",
            "task_id", "idempotency_key", "binding",
        },
        "privacy": "R2",
    },
    PROACTIVE_UNDO_OPERATION: {
        "kind": "guarded_write",
        "allowed": {
            "dismissal_feedback_id", "feedback_id", "actor_identity_hash", "now", "feedback_log",
            "task_id", "idempotency_key", "binding",
        },
        "privacy": "R2",
    },
}

OPERATIONS: dict[str, dict[str, Any]] = {
    "domain.inspect": {"kind": "read", "allowed": {"task_id", "idempotency_key", "binding"}, "privacy": "R1"},
    "domain.candidate": {"kind": "read", "allowed": {"task_id", "idempotency_key", "binding", "evidence_refs", "proposal"}, "privacy": "R1"},
    "session.preview": {"kind": "guarded_write", "allowed": {"session_id", "transition", "payload", "actor_identity_hash", "expected_sequence", "now", "task_id", "idempotency_key", "binding"}, "privacy": "R1"},
    "session.confirm": {"kind": "guarded_write", "allowed": {"preview", "confirmation_token", "confirmed", "idempotency_key", "now", "task_id", "binding"}, "privacy": "R1"},
    REFLECTION_STAGE_OPERATION: {
        "kind": "guarded_write",
        "allowed": {
            "event_id", "canonical_checksum", "watermark", "source", "snapshot",
            "freshness", "rule_version", "scope", "publication_version", "occurred_at",
            "task_id", "idempotency_key", "binding",
        },
        "privacy": "R2",
    },
    CANDIDATE_REVIEW_OPERATION: {
        "kind": "guarded_write",
        "allowed": {
            "candidate_id", "action", "expected_version", "edited_payload",
            "edited_payload_checksum", "explicit_confirmation", "confirmation_token",
            "conflict_disposition", "feedback_id", "task_id", "idempotency_key",
            "binding",
        },
        "privacy": "R2",
    },
    "candidate.list": {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding", "scope", "limit", "cursor"},
        "privacy": "R2",
    },
    PROJECTION_GET_OPERATION: {
        "kind": "read",
        "allowed": {"task_id", "idempotency_key", "binding", "scope"},
        "privacy": "R2",
    },
    "evidence.sqlite_query": {
        "kind": "read",
        "allowed": {
            "task_id", "idempotency_key", "binding", "database_id", "query_id",
            "version", "parameters", "scope", "skill_id", "supporting_skills",
            "manifest_checksum", "privacy_ceiling",
        },
        "privacy": "R1",
        "checksum": "b06b0d5ce4f762515082aebc296bd804ff18ff6875d7af552f0aa936f163227e",
    },
    **CONVERSATION_READ_OPERATIONS,
    **PROACTIVE_OPERATIONS,
}

PROJECT_OPERATIONS: dict[str, dict[str, Any]] = {
    operation["id"]: {
        "kind": "read" if operation["side_effect_class"] == "none" else "guarded_write",
        "allowed": (
            {"task_id", "idempotency_key", "binding", "authority_id", "limit", "cursor", "start_date", "end_date", "filters", "snapshot_id", "watermark_id"}
            if operation["id"] in WAREHOUSE_READ_OPERATIONS else
            {"task_id", "idempotency_key", "binding", "source_scope", "snapshot_checksum", "watermark_checksum", "batch_limit", "extractor", "model_receipt", "schema_version", "evidence_refs", "records", "actor", "profile", "now", "preview", "confirmed"}
            if operation["id"] in SEMANTIC_OPERATIONS else
            {"task_id", "idempotency_key", "binding", "semantic_snapshot_checksum", "source_ids", "embedding_receipt", "index_schema_version", "actor", "profile", "now", "preview"}
            if operation["id"] == "index.build" else
            {"task_id", "idempotency_key", "binding", "generation_id", "expected_ids", "indexed_ids"}
            if operation["id"] == "index.reconcile" else
            {"task_id", "idempotency_key", "binding", "generation_id", "policy_id", "policy_checksum", "reconcile"}
            if operation["id"] == "index.evaluate" else
            {"task_id", "idempotency_key", "binding", "action", "snapshot_id", "generation_id", "manifest", "reconcile", "eval_passed", "eval_checksum", "current_pointer", "target_pointer", "protected_fingerprint", "actor", "profile", "now", "preview", "confirmed", "fault"}
            if operation["id"] in SNAPSHOT_OPERATIONS else
            {"task_id", "idempotency_key", "binding", "authority_id", "source_checksum", "snapshot_checksum", "watermark_checksum", "count", "actor", "profile", "before_fingerprint", "preview", "confirmed", "now", "crash_at", "operation_id"}
            if operation["id"] in MUTATION_OPERATIONS else
            {"task_id", "idempotency_key", "binding", "query", "record_id", "limit", "cursor", "snapshot_id", "source_id"}
        ),
        "privacy": operation["privacy_ceiling"],
        "checksum": operation["checksum"],
        "authority": operation["authority_class"],
    }
    for operation in operations_for_profile(load_registry(), "production")
}
PROJECT_ALIASES = {
    alias["name"]: operation["id"]
    for operation in operations_for_profile(load_registry(), "production")
    for alias in operation.get("aliases", [])
}


def canonical_project_operation(operation: str) -> str:
    return PROJECT_ALIASES.get(operation, operation)

class PiDomainGatewayError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail

def _error(operation: str, code: str) -> dict[str, Any]:
    return {"schema_version": PI_DOMAIN_GATEWAY_SCHEMA, "operation": operation, "ok": False, "status": "error", "error": {"code": code}}

def _ok(operation: str, data: Any) -> dict[str, Any]:
    return {"schema_version": PI_DOMAIN_GATEWAY_SCHEMA, "operation": operation, "ok": True, "status": "success", "data": data}

class PiDomainGateway:
    def __init__(self, *, service: GuardedOrchestrationInterface | None = None, capability: str | None = None,
                 read_handler=None, warehouse_tools: WarehouseTools | None = None,
                 warehouse_ledger: WarehouseOperationLedger | None = None,
                 semantic_tools: SemanticMaintenanceTools | None = None,
                 retrieval_tools: RetrievalMaintenanceTools | None = None,
                 snapshot_tools: SnapshotReleaseTools | None = None,
                 evidence_tool: EvidenceSqliteTool | None = None,
                 reflection_adapter=None, reflection_db: Path | str | None = None,
                 review_adapter=None, review_db: Path | str | None = None,
                 conversation_db: Path | str | None = None) -> None:
        self.service = service
        self.capability = capability or os.environ.get("PI_DOMAIN_CAPABILITY", DEFAULT_CAPABILITY)
        self.read_handler = read_handler
        self.warehouse_tools = warehouse_tools
        self.warehouse_ledger = warehouse_ledger
        self.semantic_tools = semantic_tools
        self.retrieval_tools = retrieval_tools
        self.snapshot_tools = snapshot_tools
        self.evidence_tool = evidence_tool
        self.reflection_adapter = reflection_adapter
        self.reflection_db = reflection_db
        self.review_adapter = review_adapter
        self.review_db = review_db
        self.conversation_db = conversation_db

    def _load_review_candidates(self, reflection_db):
        from personal_knowledge.application.conversation.harness_reflection import HarnessReflectionAdapter
        from personal_knowledge.application.conversation.extracted_candidate_store import ExtractedCandidateStore
        from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB

        candidates = HarnessReflectionAdapter.load_candidates(reflection_db)
        candidates.update(ExtractedCandidateStore(reflection_db).load_candidates(
            self.conversation_db or AGENT_CONVERSATIONS_DB))
        return candidates

    def _bound_review_adapter(self, *, read_only: bool = False):
        """Resolve persisted candidates on each request, including after restart."""
        if self.review_adapter is not None:
            return self.review_adapter
        from personal_knowledge.application.conversation.harness_candidate_review import (
            DEFAULT_CANDIDATE_REVIEW_DB, HarnessCandidateReviewAdapter,
        )
        from personal_knowledge.application.conversation.harness_reflection import HarnessReflectionAdapter

        review_db = self.review_db or DEFAULT_CANDIDATE_REVIEW_DB
        if read_only and not Path(review_db).exists():
            return None
        reflection_db = getattr(self.reflection_adapter, "db_path", None) or self.reflection_db or DEFAULT_REFLECTION_DB
        candidates = self._load_review_candidates(reflection_db)
        return HarnessCandidateReviewAdapter(db_path=review_db, candidates=candidates, read_only=read_only)

    def _check(self, operation: str, params: Mapping[str, Any], capability: str | None) -> None:
        canonical = canonical_project_operation(operation)
        spec = OPERATIONS.get(canonical) or PROJECT_OPERATIONS.get(canonical)
        if spec is None:
            raise PiDomainGatewayError("unknown_operation")
        if capability is None or not hmac.compare_digest(str(capability), str(self.capability)):
            raise PiDomainGatewayError("capability_invalid")
        if not isinstance(params, Mapping) or set(params) - spec["allowed"]:
            raise PiDomainGatewayError("undeclared_input")
        if canonical.startswith("domain.") and not params.get("task_id"):
            raise PiDomainGatewayError("task_id_required")
        if not params.get("idempotency_key"):
            raise PiDomainGatewayError("idempotency_key_required")
        if not params.get("binding"):
            raise PiDomainGatewayError("binding_required")

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None, *, capability: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        try:
            self._check(operation, params, capability)
            canonical = canonical_project_operation(operation)
            spec = OPERATIONS.get(canonical) or PROJECT_OPERATIONS[canonical]
            routed = {key: value for key, value in params.items() if key not in {"task_id", "binding"}}
            if canonical in WAREHOUSE_READ_OPERATIONS:
                data = (self.warehouse_tools or WarehouseTools()).invoke(
                    canonical, {key: value for key, value in routed.items() if key != "idempotency_key"}
                )
                data["capability_checksum"] = spec["checksum"]
                return _ok(canonical, data)
            if canonical in SEMANTIC_OPERATIONS or canonical in RETRIEVAL_OPERATIONS or canonical in SNAPSHOT_OPERATIONS:
                if canonical in SEMANTIC_OPERATIONS:
                    tool = self.semantic_tools
                elif canonical in RETRIEVAL_OPERATIONS:
                    tool = self.retrieval_tools
                else:
                    tool = self.snapshot_tools
                if tool is None:
                    return _ok(canonical, {"status": "authority_unavailable", "execution": "not_run", "capability_checksum": spec["checksum"]})
                data = tool.invoke(canonical, routed)
                if isinstance(data, dict):
                    data["capability_checksum"] = spec["checksum"]
                return _ok(canonical, data)
            if canonical in MUTATION_OPERATIONS:
                if self.warehouse_ledger is None:
                    return _ok(canonical, {"status": "authority_unavailable", "execution": "not_run", "capability_checksum": spec["checksum"]})
                data = self.warehouse_ledger.invoke(canonical, routed)
                if isinstance(data, dict):
                    data["capability_checksum"] = spec["checksum"]
                return _ok(canonical, data)
            if canonical == EVIDENCE_SQLITE_OPERATION:
                # Explicit operation registration only: no dynamic callable/path.
                # Lease, manifest checksum and privacy ceiling are validated here
                # (before the adapter), and the adapter repeats query-ID/scope/
                # binding denial. Never a promotion/canonical mutation route.
                if params.get("skill_id") != LEASE_SKILL_ID:
                    return _error(canonical, "lease_invalid")
                if params.get("manifest_checksum") != knowledge_research_checksum():
                    return _error(canonical, "manifest_drift")
                if params.get("privacy_ceiling") != PRIVACY_CEILING:
                    return _error(canonical, "privacy_ceiling_mismatch")
                tool = self.evidence_tool or EvidenceSqliteTool()
                data = tool.invoke({key: value for key, value in params.items() if key not in {"task_id", "idempotency_key"}})
                if isinstance(data, dict):
                    data["capability_checksum"] = spec.get("checksum")
                return _ok(canonical, data)
            if canonical == REFLECTION_STAGE_OPERATION:
                # Explicit dispatcher-bound provider only: the adapter validates
                # the full event/checksum/watermark/source/snapshot/freshness/
                # rule-version/task/idempotency/binding before any inference and
                # returns a safe staged/duplicate/rejected/failed envelope.
                from personal_knowledge.application.conversation.harness_reflection import (
                    HarnessReflectionAdapter,
                )
                adapter = self.reflection_adapter
                if adapter is None:
                    db = self.reflection_db or DEFAULT_REFLECTION_DB
                    adapter = HarnessReflectionAdapter(db_path=db)
                data = adapter.stage(**dict(params))
                if isinstance(data, dict):
                    data["capability_checksum"] = spec.get("checksum")
                return _ok(canonical, data)
            if canonical == "candidate.list":
                from personal_knowledge.application.conversation.harness_reflection import HarnessReflectionAdapter
                from personal_knowledge.application.conversation.candidate_review_store import CandidateReviewReadStore
                from personal_knowledge.application.conversation.harness_candidate_review import DEFAULT_CANDIDATE_REVIEW_DB
                scope = params.get("scope")
                limit = params.get("limit", 20)
                cursor = params.get("cursor", "")
                if not isinstance(scope, str) or not scope or len(scope) > 2048:
                    return _error(canonical, "scope_denied")
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100 or not isinstance(cursor, str) or len(cursor) > 256:
                    return _error(canonical, "parameter_invalid")
                candidates = self._load_review_candidates(self.reflection_db or DEFAULT_REFLECTION_DB)
                store = CandidateReviewReadStore(self.review_db or DEFAULT_CANDIDATE_REVIEW_DB, candidates)
                return _ok(canonical, store.list_candidates(scope=scope, limit=limit, cursor=cursor))
            if canonical == CANDIDATE_REVIEW_OPERATION:
                # Explicit guarded review provider only: the adapter validates
                # candidate/version/action/edit-checksum/confirmation/token/
                # conflict-disposition/idempotency and returns one safe no-store
                # review state. Never a promotion/rollback/canonical route and
                # never a dynamic callable name.
                adapter = self._bound_review_adapter()
                data = adapter.review(**dict(params))
                if isinstance(data, dict):
                    data["capability_checksum"] = spec.get("checksum")
                return _ok(canonical, data)
            if canonical == PROJECTION_GET_OPERATION:
                # Explicit fixed read-only provider only (HARNESS-07): derives
                # the safe versioned projection envelope exclusively from the
                # confirmed accepted review state of the bound review
                # adapter/ledger (D-28). Never a promotion/rollback/canonical/
                # pointer route and never a dynamic callable name.
                from personal_knowledge.services.harness_conversation_service import (
                    HarnessModelProjectionError,
                    HarnessModelProjectionProvider,
                )
                provider = HarnessModelProjectionProvider(
                    review_adapter=self._bound_review_adapter(read_only=True),
                    review_db=self.review_db,
                )
                try:
                    data = provider.get(**dict(params))
                except HarnessModelProjectionError as exc:
                    return _error(canonical, exc.code)
                if isinstance(data, dict):
                    data.setdefault("capability_checksum", spec.get("checksum"))
                return _ok(canonical, data)
            if canonical == PROACTIVE_STATE_OPERATION:
                # Explicit fixed read-only provider only (Plan 61-10): validates
                # the exact scope/category/quiet-hour vocabulary and projects
                # deterministic no-store metadata through the Plan 61-07 adapter.
                # Never a scheduling/permission/value/canonical mutation path and
                # never a dynamic callable name.
                from personal_knowledge.application.conversation.harness_proactive import (
                    ProactiveError,
                    project_proactive_state,
                )
                if not _proactive_scope_ok(params.get("scope")):
                    return _error(canonical, "unknown_scope")
                try:
                    projection = project_proactive_state(
                        events=params.get("events"),
                        controls=params.get("controls"),
                        quiet_hours=params.get("quiet_hours"),
                        now=params.get("now"),
                        manual_order=params.get("manual_order"),
                    )
                except ProactiveError as exc:
                    return _error(canonical, exc.code)
                data = dict(projection)
                data["scope"] = params.get("scope")
                data["controls"] = params.get("controls")
                data["feedback"] = {
                    "feedback_id": _proactive_feedback_id(
                        canonical, params.get("scope"), params.get("idempotency_key")
                    ),
                    "feedback_count": 0,
                }
                data["metadata_only"] = True
                return _ok(canonical, data)
            if canonical == PROACTIVE_CONTROLS_OPERATION:
                # Explicit guarded presentation-control provider only (Plan
                # 61-10): a bounded metadata envelope that validates the exact
                # category and quiet-hour vocabulary; it never schedules,
                # changes permissions/values or writes canonical/promotion/
                # rollback/watermark/active-pointer state.
                from personal_knowledge.application.conversation.harness_proactive import (
                    CONTROL_CATEGORIES,
                )
                if not _proactive_scope_ok(params.get("scope")):
                    return _error(canonical, "unknown_scope")
                if params.get("category") not in CONTROL_CATEGORIES:
                    return _error(canonical, "declared_category")
                if not _proactive_quiet_hours_ok(params.get("quiet_hours")):
                    return _error(canonical, "quiet_hours_invalid")
                return _ok(canonical, {
                    "scope": params.get("scope"),
                    "category": params.get("category"),
                    "enabled": bool(params.get("enabled")),
                    "quiet_hours": params.get("quiet_hours"),
                    "feedback": {
                        "feedback_id": _proactive_feedback_id(
                            canonical, params.get("scope"), params.get("idempotency_key")
                        ),
                        "feedback_count": 0,
                    },
                    "metadata_only": True,
                })
            if canonical == PROACTIVE_DISMISS_OPERATION:
                # Explicit guarded dismissal provider only (Plan 61-10): appends
                # exactly one dismissal feedback entry through the Plan 61-07
                # adapter; an exact idempotent retry appends nothing.
                from personal_knowledge.application.conversation.harness_proactive import (
                    ProactiveError,
                    apply_dismissal,
                )
                try:
                    result = apply_dismissal(
                        feedback_log=params.get("feedback_log", ()),
                        cluster_key=params.get("cluster_key"),
                        feedback_id=params.get("feedback_id"),
                        actor_identity_hash=params.get("actor_identity_hash"),
                        idempotency_key=params.get("idempotency_key"),
                        now=params.get("now"),
                    )
                except ProactiveError as exc:
                    return _error(canonical, exc.code)
                return _ok(canonical, {
                    "operation": "dismiss",
                    "existing": result["existing"],
                    "feedback_log": result["feedback_log"],
                    "feedback_count": len(result["feedback_log"]),
                    "receipt": result["receipt"],
                    "metadata_only": True,
                })
            if canonical == PROACTIVE_UNDO_OPERATION:
                # Explicit guarded undo provider only (Plan 61-10): appends a
                # new undo entry and never mutates the dismissal entry.
                from personal_knowledge.application.conversation.harness_proactive import (
                    ProactiveError,
                    undo_dismissal,
                )
                try:
                    result = undo_dismissal(
                        feedback_log=params.get("feedback_log", ()),
                        dismissal_feedback_id=params.get("dismissal_feedback_id"),
                        feedback_id=params.get("feedback_id"),
                        actor_identity_hash=params.get("actor_identity_hash"),
                        idempotency_key=params.get("idempotency_key"),
                        now=params.get("now"),
                    )
                except ProactiveError as exc:
                    return _error(canonical, exc.code)
                return _ok(canonical, {
                    "operation": "undo_dismissal",
                    "existing": result["existing"],
                    "feedback_log": result["feedback_log"],
                    "feedback_count": len(result["feedback_log"]),
                    "receipt": result["receipt"],
                    "metadata_only": True,
                })
            if spec["kind"] == "read":
                if self.read_handler is not None:
                    return _ok(canonical, self.read_handler(canonical, params))
                return _ok(canonical, {"status": "synthetic", "operation": canonical, "task_id": params.get("task_id"), "evidence_refs": params.get("evidence_refs", []), "capability_checksum": spec.get("checksum")})
            target = self.service or GuardedOrchestrationInterface()
            # Only the existing guarded interface receives writes; no dynamic callable names enter it.
            routed = {key: value for key, value in params.items() if key not in {"task_id", "binding", "idempotency_key"}}
            result = target.invoke(operation, **routed)
            return _ok(operation, result)
        except PiDomainGatewayError as exc:
            return _error(operation, exc.code)
        except Exception as exc:  # transport-safe envelope; detail stays local
            code = str(getattr(exc, "code", "") or "domain_unavailable").split(":", 1)[0]
            safe_codes = {
                "missing_parameter", "explicit_confirmation_required", "confirmation_expired", "preview_checksum_mismatch",
                "invalid_request", "provider_outcome_unknown", "preview_stale", "snapshot_binding_mismatch",
                "watermark_binding_mismatch", "idempotency_conflict", "idempotency_mismatch", "warehouse_authority_unavailable",
                "preview_required", "fingerprint_binding_mismatch",
                "lease_invalid", "manifest_drift", "privacy_ceiling_mismatch",
                "database_unknown", "unknown_query", "version_mismatch", "binding_required", "scope_denied",
                "sql_forbidden", "parameter_invalid", "path_forbidden", "limit_exceeded",
                "supporting_skill_rejected", "database_unavailable", "schema_gate_failed",
                "query_timeout", "descriptor_invalid", "undeclared_input",
                "unknown_scope", "declared_category", "quiet_hours_invalid", "dismissal_not_found",
            }
            return _error(operation, code if code in safe_codes else "domain_unavailable")

def invoke_pi_domain(operation: str, params: Mapping[str, Any] | None = None, *, capability: str | None = None, service=None) -> dict[str, Any]:
    return PiDomainGateway(service=service).invoke(operation, params, capability=capability)

__all__ = ["OPERATIONS", "PROJECT_OPERATIONS", "PROJECT_ALIASES", "PI_DOMAIN_GATEWAY_SCHEMA", "PI_DOMAIN_CAPABILITY_HEADER", "CANDIDATE_REVIEW_OPERATION", "PROJECTION_GET_OPERATION", "PROACTIVE_STATE_OPERATION", "PROACTIVE_CONTROLS_OPERATION", "PROACTIVE_DISMISS_OPERATION", "PROACTIVE_UNDO_OPERATION", "PROACTIVE_OPERATIONS", "PiDomainGateway", "PiDomainGatewayError", "canonical_project_operation", "invoke_pi_domain"]
