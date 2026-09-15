"""Shared read-only service for snapshot-bound personal-state intelligence."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.retrieval.evidence import EvidenceResolver

from .changes import CHANGE_ALGORITHM_VERSION, compare_projections
from .explanations import (
    EXPLANATION_SCHEMA_VERSION,
    build_recent_changes,
    explain_state,
)
from .schema import (
    EVIDENCE_TYPES,
    PRIVACY_CLASSES,
    SCHEMA_VERSION,
    PersonalStateRun,
    SnapshotBinding,
    ValidatedAssertion,
    ValidatedEvidence,
    canonical_json,
    checksum,
)
from .runs import _ROLE_BY_EVIDENCE_TYPE, _assertion_payload
from .state_projection import StateKey, project_current_state


INTERFACE_SCHEMA_VERSION = "personal_state_interface_v1"
PROJECTION_RULE_VERSION = "personal_state_projection_v1"
MAX_HISTORY_LIMIT = 100


class IntelligenceServiceError(ValueError):
    """Stable typed error shared by CLI, REST and MCP adapters."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise IntelligenceServiceError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise IntelligenceServiceError("invalid_time", f"{field}:timezone_required")
    return parsed


def _read_connection(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise IntelligenceServiceError("database_missing", str(db_path))
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


class IntelligenceService:
    """One non-mutating backend for current/history/recent/explain operations."""

    def __init__(self, db_path: Path | str, *, resolver: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.resolver = resolver or EvidenceResolver(unified_db=self.db_path)

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        """Invoke an operation and normalize both success and typed failures."""
        operations = {
            "state.current": self.state_current,
            "state.history": self.state_history,
            "changes.recent": self.changes_recent,
            "state.explain": self.state_explain,
        }
        handler = operations.get(operation)
        if handler is None:
            return self._error(operation, "unknown_operation", operation)
        try:
            return handler(**params)
        except IntelligenceServiceError as exc:
            return self._error(operation, exc.code, exc.detail)
        except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
            return self._error(operation, "invalid_intelligence_state", str(exc))

    def state_current(
        self,
        *,
        snapshot_id: str | None = None,
        run_id: str | None = None,
        as_of: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = self._validate_limit(limit)
        context = self._load_context(snapshot_id=snapshot_id, run_id=run_id)
        projection = project_current_state(
            context["runs"], as_of=self._as_of(context, as_of)
        )
        items = [self._state_metadata(item) for item in projection.states[:limit]]
        status = self._result_status(items)
        return self._success("state.current", context, status, {
            "as_of": projection.as_of,
            "total_available": len(projection.states),
            "limit": limit,
            "items": items,
        })

    def state_history(
        self,
        *,
        snapshot_id: str | None = None,
        run_id: str | None = None,
        as_of: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = self._validate_limit(limit)
        context = self._load_context(snapshot_id=snapshot_id, run_id=run_id)
        projection = project_current_state(
            context["runs"], as_of=self._as_of(context, as_of)
        )
        rows: list[dict[str, Any]] = []
        for state in projection.states:
            for step in state.formation_path:
                rows.append({
                    "key": _json_value(state.key),
                    "run_id": step.run_id,
                    "assertion_id": step.assertion_id,
                    "assertion_type": state.key.assertion_kind,
                    "valid_from": step.valid_from,
                    "valid_to": step.valid_to,
                    "observed_at": step.observed_at,
                    "status": step.status,
                    "provenance_class": step.provenance_class,
                    "confidence": step.confidence,
                    "uncertainty": list(step.uncertainty),
                    "value_checksum": step.value_checksum,
                    "evidence_refs": list(step.evidence_refs),
                })
        rows.sort(key=lambda row: (row["observed_at"], row["assertion_id"]), reverse=True)
        selected = rows[:limit]
        return self._success("state.history", context, self._result_status(selected), {
            "as_of": projection.as_of,
            "total_available": len(rows),
            "limit": limit,
            "items": selected,
        })

    def changes_recent(
        self,
        *,
        snapshot_id: str | None = None,
        run_id: str | None = None,
        as_of: str | None = None,
        window_start: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = self._validate_limit(limit)
        context = self._load_context(snapshot_id=snapshot_id, run_id=run_id)
        end = self._as_of(context, as_of)
        start = window_start or self._earliest_time(context)
        _parse_time(start, "window_start")
        if _parse_time(start, "window_start") > _parse_time(end, "as_of"):
            raise IntelligenceServiceError("invalid_window")
        before = project_current_state(context["runs"], as_of=start)
        after = project_current_state(context["runs"], as_of=end)
        changes = compare_projections(before, after)
        summary = build_recent_changes(
            changes,
            run_id=context["selected"].run_id,
            run_checksum=context["selected"].output_manifest_checksum,
            as_of=end,
            window_start=start,
            limit=limit,
            resolver=self.resolver,
            evidence_catalog=self._evidence_catalog(context),
        )
        items = [_json_value(item) for item in summary.items]
        return self._success("changes.recent", context, self._result_status(items), {
            "as_of": end,
            "window_start": start,
            "total_available": summary.total_available,
            "limit": limit,
            "items": items,
            "manifest_checksum": summary.manifest_checksum,
        })

    def state_explain(
        self,
        *,
        assertion_kind: str,
        subject: str,
        domain: str,
        scope: str,
        predicate: str,
        snapshot_id: str | None = None,
        run_id: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        key = StateKey(
            assertion_kind=assertion_kind.strip(),
            subject=subject.strip(),
            domain=domain.strip(),
            scope=scope.strip(),
            predicate=predicate.strip(),
        )
        if any(not value for value in asdict(key).values()):
            raise IntelligenceServiceError("missing_state_key")
        context = self._load_context(snapshot_id=snapshot_id, run_id=run_id)
        end = self._as_of(context, as_of)
        projection = project_current_state(context["runs"], as_of=end, expected_keys=(key,))
        state = next((row for row in projection.states if row.key == key), None)
        if state is None:
            raise IntelligenceServiceError("state_key_missing")
        explanation = explain_state(
            state,
            snapshot_id=context["snapshot"].snapshot_id,
            snapshot_hash=context["snapshot"].snapshot_hash,
            run_id=context["selected"].run_id,
            run_checksum=context["selected"].output_manifest_checksum,
            as_of=end,
            resolver=self.resolver,
        )
        payload = _json_value(explanation)
        # Values are represented only by checksums at every transport boundary.
        return self._success(
            "state.explain",
            context,
            "uncertain" if explanation.abstained or state.status in {"unknown", "uncertain"} else "success",
            payload,
        )

    def _load_context(
        self, *, snapshot_id: str | None, run_id: str | None
    ) -> dict[str, Any]:
        con = _read_connection(self.db_path)
        try:
            selected_row = None
            if run_id:
                selected_row = con.execute(
                    "SELECT p.publication_sequence,r.* FROM personal_state_runs r "
                    "JOIN personal_state_publications p ON p.run_id=r.run_id "
                    "WHERE r.run_id=? AND r.status='committed'",
                    (run_id,),
                ).fetchone()
                if selected_row is None:
                    orphaned = con.execute(
                        "SELECT 1 FROM personal_state_runs WHERE run_id=? AND status='committed'",
                        (run_id,),
                    ).fetchone()
                    if orphaned is not None:
                        raise IntelligenceServiceError("publication_sequence_missing", run_id)
                    raise IntelligenceServiceError("run_missing", run_id)
                if snapshot_id and str(selected_row["snapshot_id"]) != snapshot_id:
                    raise IntelligenceServiceError("cross_snapshot_run", run_id)
                snapshot_id = str(selected_row["snapshot_id"])
            if not snapshot_id:
                authority = con.execute(
                    "SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1"
                ).fetchone()
                snapshot_id = str(authority[0]) if authority and authority[0] else ""
            if not snapshot_id:
                raise IntelligenceServiceError("snapshot_missing", "active")
            snapshot_row = con.execute(
                "SELECT snapshot_id,manifest_hash,status FROM serving_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot_row is None:
                raise IntelligenceServiceError("snapshot_missing", snapshot_id)
            if str(snapshot_row["status"]) != "validated":
                raise IntelligenceServiceError("snapshot_not_validated", snapshot_id)
            orphaned = con.execute(
                "SELECT r.run_id FROM personal_state_runs r "
                "LEFT JOIN personal_state_publications p ON p.run_id=r.run_id "
                "WHERE r.snapshot_id=? AND r.status='committed' "
                "AND p.publication_sequence IS NULL LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if orphaned is not None:
                raise IntelligenceServiceError(
                    "publication_sequence_missing", str(orphaned["run_id"])
                )
            if selected_row is None:
                selected_row = con.execute(
                    "SELECT p.publication_sequence,r.* FROM personal_state_runs r "
                    "JOIN personal_state_publications p ON p.run_id=r.run_id "
                    "WHERE r.snapshot_id=? AND r.status='committed' "
                    "ORDER BY p.publication_sequence DESC LIMIT 1",
                    (snapshot_id,),
                ).fetchone()
            if selected_row is None:
                orphaned = con.execute(
                    "SELECT run_id FROM personal_state_runs "
                    "WHERE snapshot_id=? AND status='committed' LIMIT 1",
                    (snapshot_id,),
                ).fetchone()
                if orphaned is not None:
                    raise IntelligenceServiceError(
                        "publication_sequence_missing", str(orphaned["run_id"])
                    )
                raise IntelligenceServiceError("run_missing", snapshot_id)
            selected_order = int(selected_row["publication_sequence"])
            rows = con.execute(
                "SELECT p.publication_sequence,r.* FROM personal_state_runs r "
                "JOIN personal_state_publications p ON p.run_id=r.run_id "
                "WHERE r.snapshot_id=? AND r.status='committed' "
                "AND p.publication_sequence <= ? ORDER BY p.publication_sequence",
                (snapshot_id, selected_order),
            ).fetchall()
            runs = tuple(self._hydrate_run(con, row) for row in rows)
            selected = next(
                (row for row in runs if row.run_id == str(selected_row["run_id"])), None
            )
            if selected is None:
                raise IntelligenceServiceError("run_missing", str(selected_row["run_id"]))
            if selected.snapshot.snapshot_hash != str(snapshot_row["manifest_hash"]):
                raise IntelligenceServiceError("snapshot_hash_mismatch", snapshot_id)
            return {
                "snapshot": selected.snapshot,
                "selected": selected,
                "runs": runs,
            }
        finally:
            con.close()

    def _hydrate_run(self, con: sqlite3.Connection, row: sqlite3.Row) -> PersonalStateRun:
        input_manifest = json.loads(str(row["input_manifest_json"]))
        output_manifest = json.loads(str(row["output_manifest_json"]))
        if checksum(input_manifest) != str(row["input_manifest_checksum"]):
            raise IntelligenceServiceError("input_manifest_checksum_mismatch", str(row["run_id"]))
        if checksum(output_manifest) != str(row["output_manifest_checksum"]):
            raise IntelligenceServiceError("output_manifest_checksum_mismatch", str(row["run_id"]))
        members = input_manifest.get("snapshot_members")
        if not isinstance(members, Mapping):
            raise IntelligenceServiceError("snapshot_members_missing", str(row["run_id"]))
        snapshot = SnapshotBinding(
            snapshot_id=str(row["snapshot_id"]),
            snapshot_hash=str(row["snapshot_hash"]),
            members=members,
        )
        assertions: list[ValidatedAssertion] = []
        assertion_rows = con.execute(
            "SELECT * FROM personal_state_assertions WHERE run_id=? ORDER BY assertion_id",
            (row["run_id"],),
        ).fetchall()
        for assertion in assertion_rows:
            evidence_rows = con.execute(
                "SELECT * FROM personal_state_evidence WHERE assertion_id=? "
                "ORDER BY evidence_type,evidence_ref,artifact_version_id",
                (assertion["assertion_id"],),
            ).fetchall()
            evidence_items: list[ValidatedEvidence] = []
            for item in evidence_rows:
                evidence_type = str(item["evidence_type"])
                serving_role = str(item["serving_role"])
                version_id = str(item["artifact_version_id"])
                privacy_class = str(item["privacy_class"])
                member = snapshot.members.get(serving_role)
                if evidence_type not in EVIDENCE_TYPES or _ROLE_BY_EVIDENCE_TYPE.get(
                    evidence_type
                ) != serving_role:
                    raise IntelligenceServiceError(
                        "evidence_role_mismatch", str(item["evidence_ref"])
                    )
                if (
                    str(item["snapshot_id"]) != snapshot.snapshot_id
                    or str(item["snapshot_hash"]) != snapshot.snapshot_hash
                    or member is None
                    or str(member.get("artifact_version_id") or "") != version_id
                ):
                    raise IntelligenceServiceError(
                        "evidence_snapshot_mismatch", str(item["evidence_ref"])
                    )
                if (
                    str(item["eligibility"]) != "eligible"
                    or privacy_class not in PRIVACY_CLASSES
                    or privacy_class != str(member.get("privacy_class") or "")
                    or not str(item["evidence_checksum"])
                ):
                    raise IntelligenceServiceError(
                        "evidence_integrity_mismatch", str(item["evidence_ref"])
                    )
                evidence_items.append(ValidatedEvidence(
                    ref=str(item["evidence_ref"]),
                    artifact_type=evidence_type,
                    serving_role=serving_role,
                    artifact_version_id=version_id,
                    evidence_checksum=str(item["evidence_checksum"]),
                    privacy_class=privacy_class,
                ))
            evidence = tuple(evidence_items)
            hydrated = ValidatedAssertion(
                assertion_id=str(assertion["assertion_id"]),
                assertion_kind=str(assertion["assertion_kind"]),
                provenance_class=str(assertion["provenance_class"]),
                subject=str(assertion["subject"]),
                domain=str(assertion["domain"]),
                scope=str(assertion["scope"]),
                predicate=str(assertion["predicate"]),
                value=json.loads(str(assertion["value_json"])),
                valid_from=str(assertion["valid_from"]),
                valid_to=str(assertion["valid_to"]) if assertion["valid_to"] else None,
                observed_at=str(assertion["observed_at"]),
                confidence=float(assertion["confidence"]),
                uncertainty=str(assertion["uncertainty"]),
                lifecycle=str(assertion["lifecycle"]),
                evidence=evidence,
                payload_checksum=str(assertion["payload_checksum"]),
            )
            try:
                stored_payload = json.loads(str(assertion["payload_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise IntelligenceServiceError(
                    "assertion_payload_invalid", hydrated.assertion_id
                ) from exc
            expected_payload = _assertion_payload(hydrated, snapshot=snapshot)
            if canonical_json(stored_payload) != canonical_json(expected_payload):
                raise IntelligenceServiceError(
                    "assertion_payload_mismatch", hydrated.assertion_id
                )
            if checksum(expected_payload) != hydrated.payload_checksum:
                raise IntelligenceServiceError(
                    "assertion_payload_checksum_mismatch", hydrated.assertion_id
                )
            assertions.append(hydrated)
        manifest_assertions = input_manifest.get("assertions")
        if not isinstance(manifest_assertions, list) or canonical_json(
            manifest_assertions
        ) != canonical_json(assertions):
            raise IntelligenceServiceError("assertion_input_manifest_mismatch", str(row["run_id"]))
        if tuple(output_manifest.get("assertion_ids") or ()) != tuple(
            item.assertion_id for item in assertions
        ):
            raise IntelligenceServiceError("assertion_manifest_mismatch", str(row["run_id"]))
        if int(output_manifest.get("assertion_count", -1)) != len(assertions) or int(
            output_manifest.get("evidence_count", -1)
        ) != sum(len(item.evidence) for item in assertions):
            raise IntelligenceServiceError("output_manifest_count_mismatch", str(row["run_id"]))
        return PersonalStateRun(
            run_id=str(row["run_id"]),
            registry_id=str(row["registry_id"]),
            snapshot=snapshot,
            producer_version=str(row["producer_version"]),
            input_manifest=input_manifest,
            input_manifest_checksum=str(row["input_manifest_checksum"]),
            output_manifest=output_manifest,
            output_manifest_checksum=str(row["output_manifest_checksum"]),
            assertions=tuple(assertions),
        )

    @staticmethod
    def _evidence_catalog(context: Mapping[str, Any]) -> dict[str, ValidatedEvidence]:
        catalog: dict[str, ValidatedEvidence] = {}
        conflicts: set[str] = set()
        for run in context["runs"]:
            for assertion in run.assertions:
                for item in assertion.evidence:
                    existing = catalog.get(item.ref)
                    if existing is not None and existing != item:
                        conflicts.add(item.ref)
                    else:
                        catalog[item.ref] = item
        for ref in conflicts:
            catalog.pop(ref, None)
        return catalog

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise IntelligenceServiceError("invalid_limit", str(limit))
        return limit

    @staticmethod
    def _earliest_time(context: Mapping[str, Any]) -> str:
        values = [item.valid_from for run in context["runs"] for item in run.assertions]
        if not values:
            raise IntelligenceServiceError("assertions_missing")
        return min(values, key=lambda value: _parse_time(value, "valid_from"))

    def _as_of(self, context: Mapping[str, Any], value: str | None) -> str:
        if value:
            _parse_time(value, "as_of")
            return value
        values = [item.observed_at for run in context["runs"] for item in run.assertions]
        if not values:
            raise IntelligenceServiceError("assertions_missing")
        return max(values, key=lambda item: _parse_time(item, "observed_at"))

    @staticmethod
    def _state_metadata(state: Any) -> dict[str, Any]:
        return {
            "key": _json_value(state.key),
            "status": state.status,
            "assertion_type": state.key.assertion_kind,
            "current_assertion_id": state.current_assertion_id,
            "current_value_checksum": (
                checksum(state.current_value) if state.current_assertion_id else None
            ),
            "provenance_class": state.provenance_class,
            "confidence": state.confidence,
            "uncertainty": list(state.uncertainty),
            "evidence_status": [
                {
                    "ref": item.ref,
                    "artifact_type": item.artifact_type,
                    "serving_role": item.serving_role,
                    "artifact_version_id": item.artifact_version_id,
                    "status": "eligible",
                    "privacy_class": item.privacy_class,
                }
                for item in state.evidence
            ],
        }

    @staticmethod
    def _result_status(items: Iterable[Mapping[str, Any]]) -> str:
        rows = tuple(items)
        if not rows:
            return "empty"
        uncertain = {"unknown", "uncertain", "conflict", "stale", "expired"}
        if any(
            str(row.get("status") or "") in uncertain
            or bool(row.get("abstained"))
            for row in rows
        ):
            return "uncertain"
        return "success"

    def _success(
        self,
        operation: str,
        context: Mapping[str, Any],
        status: str,
        data: Any,
    ) -> dict[str, Any]:
        selected = context["selected"]
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION,
            "operation": operation,
            "ok": True,
            "status": status,
            "snapshot": {
                "snapshot_id": selected.snapshot.snapshot_id,
                "snapshot_hash": selected.snapshot.snapshot_hash,
            },
            "run": {
                "run_id": selected.run_id,
                "run_checksum": selected.output_manifest_checksum,
                "producer_version": selected.producer_version,
                "input_manifest_checksum": selected.input_manifest_checksum,
            },
            "rule_versions": {
                "run_schema": SCHEMA_VERSION,
                "projection": PROJECTION_RULE_VERSION,
                "changes": CHANGE_ALGORITHM_VERSION,
                "explanation": EXPLANATION_SCHEMA_VERSION,
            },
            "privacy": {"metadata_only": True, "private_bodies": 0},
            "data": data,
        }

    @staticmethod
    def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION,
            "operation": operation,
            "ok": False,
            "status": "error",
            "error": {"code": code, "detail": detail},
            "privacy": {"metadata_only": True, "private_bodies": 0},
        }


def utc_now() -> str:
    """Explicit utility for callers that intentionally choose wall-clock as-of."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
