"""Checksum-verifying, metadata-only proactive intelligence read service."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .controls import ControlTarget, active_control_frontier, project_controls
from .runs import PROACTIVE_TABLES
from .schema import canonical_json, checksum, validate_metadata_payload

INTERFACE_SCHEMA_VERSION = "proactive_intelligence_interface_v1"
MAX_LIMIT = 100


class ProactiveServiceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ProactiveServiceError("database_missing", str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _payload(row: sqlite3.Row, field: str = "payload_json") -> dict[str, Any]:
    try:
        value = json.loads(str(row[field]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProactiveServiceError("payload_invalid", str(row[0])) from exc
    if not isinstance(value, dict) or checksum(value) != str(row["payload_checksum"]):
        raise ProactiveServiceError("payload_checksum_mismatch", str(row[0]))
    try:
        validate_metadata_payload(value)
    except ValueError as exc:
        raise ProactiveServiceError("privacy_boundary_violation", str(row[0])) from exc
    return value


class ProactiveIntelligenceService:
    """Single non-mutating backend shared by CLI, REST and MCP."""

    READ_OPERATIONS = frozenset({
        "inbox.list", "digest.get", "candidates.get", "candidates.explain",
        "controls.status", "metrics.get",
    })

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    @staticmethod
    def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
        return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
                "ok": False, "status": "error", "error": {"code": code, "detail": detail},
                "privacy": {"metadata_only": True, "private_bodies": 0}}

    @staticmethod
    def _success(operation: str, data: Mapping[str, Any]) -> dict[str, Any]:
        return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
                "ok": True, "status": "success", "data": dict(data),
                "privacy": {"metadata_only": True, "private_bodies": 0}}

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        handlers = {
            "inbox.list": self.inbox_list,
            "digest.get": self.digest_get,
            "candidates.get": self.candidates_get,
            "candidates.explain": self.candidates_explain,
            "controls.status": self.controls_status,
            "metrics.get": self.metrics_get,
        }
        if operation not in self.READ_OPERATIONS or operation not in handlers:
            return self._error(operation, "unknown_operation", operation)
        try:
            return handlers[operation](**params)
        except ProactiveServiceError as exc:
            return self._error(operation, exc.code, exc.detail)
        except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._error(operation, "invalid_proactive_state", str(exc))

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
            raise ProactiveServiceError("invalid_limit", str(value))
        return value

    def _schema(self, con: sqlite3.Connection) -> None:
        tables = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        existing = tables & PROACTIVE_TABLES
        if existing != PROACTIVE_TABLES:
            code = "proactive_schema_unapplied" if not existing else "proactive_schema_partial"
            raise ProactiveServiceError(code, ",".join(sorted(PROACTIVE_TABLES - existing)))

    def _run(self, con: sqlite3.Connection, run_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
        run = con.execute("SELECT * FROM proactive_runs WHERE run_id=? AND status='committed'", (run_id,)).fetchone()
        if run is None:
            raise ProactiveServiceError("proactive_run_missing", run_id)
        manifests: dict[str, dict[str, Any]] = {}
        for name in ("input_manifest", "output_manifest"):
            try:
                value = json.loads(str(run[f"{name}_json"]))
            except json.JSONDecodeError as exc:
                raise ProactiveServiceError("run_manifest_invalid", name) from exc
            if not isinstance(value, dict) or checksum(value) != str(run[f"{name}_checksum"]):
                raise ProactiveServiceError("run_manifest_checksum_mismatch", name)
            manifests[name] = value
        output = manifests["output_manifest"]
        core = {key: output.get(key) for key in ("run_id", "input_manifest_checksum", "coordination_items", "candidates", "evaluations")}
        if core["run_id"] != run_id or checksum(core) != str(run["run_checksum"]):
            raise ProactiveServiceError("run_checksum_mismatch", run_id)
        if canonical_json({**core, "run_checksum": str(run["run_checksum"])}) != canonical_json(output):
            raise ProactiveServiceError("run_manifest_invalid", "output_manifest")
        source = con.execute("SELECT output_manifest_checksum,snapshot_id,snapshot_hash FROM personal_state_runs WHERE run_id=? AND status='committed'", (run["source_run_id"],)).fetchone()
        if source is None or str(source[0]) != str(run["source_run_checksum"]):
            raise ProactiveServiceError("source_binding_invalid", str(run["source_run_id"]))
        if str(source[1]) != str(run["snapshot_id"]) or str(source[2]) != str(run["snapshot_hash"]):
            raise ProactiveServiceError("snapshot_binding_invalid", run_id)
        historical = manifests["input_manifest"].get("control_frontier_manifest")
        if not isinstance(historical, list):
            raise ProactiveServiceError("control_frontier_manifest_missing", run_id)
        actual_historical = []
        for item in historical:
            if not isinstance(item, list) or len(item) != 5:
                raise ProactiveServiceError("control_frontier_manifest_invalid", run_id)
            row = con.execute(
                "SELECT target_authority,target_type,target_id,sequence,payload_checksum "
                "FROM proactive_control_events WHERE target_authority=? AND target_type=? AND target_id=? AND sequence=?",
                tuple(item[:4]),
            ).fetchone()
            if row is None or tuple(row) != tuple(item):
                raise ProactiveServiceError("control_frontier_history_tampered", run_id)
            actual_historical.append(tuple(row))
        if checksum({"control_events": actual_historical}) != str(run["control_frontier_checksum"]):
            raise ProactiveServiceError("control_frontier_history_tampered", run_id)
        if run["decision_run_id"] is not None:
            decision = con.execute("SELECT run_checksum FROM decision_runs WHERE run_id=? AND status='committed'", (run["decision_run_id"],)).fetchone()
            if decision is None or str(decision[0]) != str(run["decision_run_checksum"]):
                raise ProactiveServiceError("decision_binding_invalid", str(run["decision_run_id"]))
            frontier = checksum({"decision_events": [tuple(row) for row in con.execute(
                "SELECT e.recommendation_id,e.sequence,e.payload_checksum FROM decision_events e JOIN decision_recommendations r ON r.recommendation_id=e.recommendation_id WHERE r.run_id=? ORDER BY e.recommendation_id,e.sequence", (run["decision_run_id"],))]})
            if frontier != str(run["decision_event_frontier_checksum"]):
                raise ProactiveServiceError("decision_frontier_changed", run_id)
        return run, output

    def _candidate(self, con: sqlite3.Connection, candidate_id: str) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any], sqlite3.Row]:
        row = con.execute("SELECT * FROM proactive_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if row is None:
            raise ProactiveServiceError("candidate_missing", candidate_id)
        payload = _payload(row)
        run, manifest = self._run(con, str(row["run_id"]))
        expected = {str(item.get("candidate_id")): str(item.get("payload_checksum")) for item in manifest.get("candidates", ()) if isinstance(item, dict)}
        if expected.get(candidate_id) != str(row["payload_checksum"]):
            raise ProactiveServiceError("candidate_manifest_mismatch", candidate_id)
        support = con.execute("SELECT * FROM proactive_candidate_support WHERE candidate_id=? ORDER BY support_id", (candidate_id,)).fetchall()
        support_payloads = [{key: value for key, value in _payload(item).items() if key != "candidate_id"} for item in support]
        if sorted(map(canonical_json, support_payloads)) != sorted(map(canonical_json, payload.get("support") or ())):
            raise ProactiveServiceError("support_manifest_mismatch", candidate_id)
        for item in support:
            source = {"a.personal_change": {"assertion": ("personal_state_assertions", "assertion_id"), "change": ("personal_state_changes", "change_id"), "risk": ("personal_state_risks", "risk_id")},
                      "a.decision_feedback": {"recommendation": ("decision_recommendations", "recommendation_id"), "event": ("decision_events", "event_id"), "effectiveness": ("decision_effectiveness", "assessment_id")}}.get(str(item["authority_id"]), {}).get(str(item["record_type"]))
            if source:
                actual = con.execute(f"SELECT payload_checksum FROM {source[0]} WHERE {source[1]}=?", (item["record_id"],)).fetchone()
                if actual is None or str(actual[0]) != str(item["record_checksum"]):
                    raise ProactiveServiceError("support_record_stale", str(item["record_id"]))
        evaluation = con.execute("SELECT * FROM proactive_evaluations WHERE candidate_id=? ORDER BY window_end DESC,evaluation_id DESC LIMIT 1", (candidate_id,)).fetchone()
        if evaluation is None:
            raise ProactiveServiceError("evaluation_missing", candidate_id)
        _payload(evaluation)
        return row, run, payload, evaluation

    def _metadata(self, row: sqlite3.Row, run: sqlite3.Row, payload: Mapping[str, Any], evaluation: sqlite3.Row) -> dict[str, Any]:
        domains = tuple(json.loads(str(row["domains_json"])))
        targets = [ControlTarget("a.proactive_intelligence", "candidate", str(row["candidate_id"]), str(row["payload_checksum"])),
                   ControlTarget("a.proactive_intelligence", "global", "proactive", checksum({"global": "proactive"})),
                   ControlTarget("a.proactive_intelligence", "policy", str(row["policy_id"]), checksum({"policy": str(row["policy_id"])}))]
        targets.extend(ControlTarget("a.proactive_intelligence", "domain", domain, checksum({"domain": domain})) for domain in domains)
        overlay = project_controls(self.db_path, targets=tuple(targets), as_of="9999-12-31T23:59:59Z",
                                   scope=str(row["scope"]), domains=domains, policies=(str(row["policy_id"]),))
        return {"candidate_id": str(row["candidate_id"]), "candidate_checksum": str(row["payload_checksum"]),
                "run_id": str(run["run_id"]), "run_checksum": str(run["run_checksum"]),
                "source_run_id": str(run["source_run_id"]), "source_run_checksum": str(run["source_run_checksum"]),
                "source_publication_sequence": int(run["source_publication_sequence"]),
                "decision_run_id": run["decision_run_id"], "decision_run_checksum": run["decision_run_checksum"],
                "decision_event_frontier_checksum": str(run["decision_event_frontier_checksum"]),
                "control_frontier_checksum": str(run["control_frontier_checksum"]),
                "current_control_frontier_checksum": active_control_frontier(self.db_path),
                "current_control_eligible": overlay.eligible,
                "current_control_reason_codes": list(overlay.reason_codes),
                "snapshot_id": str(run["snapshot_id"]), "snapshot_hash": str(run["snapshot_hash"]),
                "policy_id": str(row["policy_id"]), "policy_version": str(row["policy_version"]),
                "candidate_class": str(row["candidate_class"]), "presentation_kind": str(row["presentation_kind"]),
                "subject": str(row["subject"]), "scope": str(row["scope"]),
                "domains": list(domains), "dedup_key": str(row["dedup_key"]),
                "valid_from": str(row["valid_from"]), "expires_at": str(row["expires_at"]),
                "importance": dict(payload.get("importance") or {}), "uncertainty": str(row["uncertainty"]),
                "reason_codes": list(payload.get("reason_codes") or ()), "evidence_status": "checksum_verified",
                "evaluation": {"evaluation_id": str(evaluation["evaluation_id"]), "result": str(evaluation["result"]),
                               "reason_codes": list(json.loads(str(evaluation["reason_codes_json"]))),
                               "state_checksum": str(evaluation["state_checksum"]), "payload_checksum": str(evaluation["payload_checksum"])},
                "support": [{key: item.get(key) for key in ("authority_id", "record_type", "record_id", "record_checksum", "source_run_id", "source_run_checksum", "snapshot_id", "snapshot_hash")} for item in payload.get("support", ()) if isinstance(item, Mapping)]}

    def inbox_list(self, *, limit: int = 50, domain: str | None = None) -> dict[str, Any]:
        limit = self._limit(limit); con = _ro(self.db_path)
        try:
            self._schema(con)
            query, args = "SELECT candidate_id FROM proactive_candidates", ()
            if domain:
                query, args = query + " WHERE domains_json LIKE ?", (f'%"{domain}"%',)
            ids = [str(r[0]) for r in con.execute(query + " ORDER BY created_at,candidate_id", args)]
            items = []
            for cid in ids:
                row, run, payload, evaluation = self._candidate(con, cid)
                item = self._metadata(row, run, payload, evaluation)
                if str(evaluation["result"]) == "eligible" and item["current_control_eligible"]:
                    items.append(item)
            return self._success("inbox.list", {"items": items[:limit], "total_available": len(items), "limit": limit})
        finally: con.close()

    def digest_get(self, *, limit: int = 50, domain: str | None = None) -> dict[str, Any]:
        result = self.inbox_list(limit=limit, domain=domain)
        data = result["data"]
        return self._success("digest.get", {"presentation_kind": "digest_item", "items": data["items"], "candidate_ids": [i["candidate_id"] for i in data["items"]], "total_available": data["total_available"], "limit": limit, "contradictory_evidence_merged": False})

    def candidates_get(self, *, candidate_id: str) -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            self._schema(con); return self._success("candidates.get", self._metadata(*self._candidate(con, candidate_id)))
        finally: con.close()

    def candidates_explain(self, *, candidate_id: str) -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            self._schema(con); row, run, payload, evaluation = self._candidate(con, candidate_id)
            item = self._metadata(row, run, payload, evaluation)
            return self._success("candidates.explain", {"candidate": item, "reason_codes": item["reason_codes"], "uncertainty": item["uncertainty"], "evidence_status": item["evidence_status"], "checksum_chain": {"candidate": item["candidate_checksum"], "run": item["run_checksum"], "control_frontier": item["control_frontier_checksum"], "evaluation": item["evaluation"]["payload_checksum"]}})
        finally: con.close()

    def controls_status(self, *, candidate_id: str, as_of: str = "9999-12-31T23:59:59Z") -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            self._schema(con); row, _, _, _ = self._candidate(con, candidate_id)
            target = ControlTarget("a.proactive_intelligence", "candidate", candidate_id, str(row["payload_checksum"]))
            projected = project_controls(self.db_path, targets=(target,), as_of=as_of, scope=str(row["scope"]), domains=tuple(json.loads(str(row["domains_json"]))))
            events = con.execute("SELECT event_id,sequence,operation,scope,reason_code,rollback_of_event_id,payload_checksum,created_at FROM proactive_control_events WHERE target_authority=? AND target_type=? AND target_id=? ORDER BY sequence", (target.authority, target.record_type, target.record_id)).fetchall()
            return self._success("controls.status", {"candidate_id": candidate_id, "as_of": as_of, "eligible": projected.eligible, "reason_codes": list(projected.reason_codes), "winning_event_id": projected.winning_event_id, "active_event_ids": list(projected.active_event_ids), "correction_requested": projected.correction_requested, "projection_checksum": projected.checksum, "frontier_checksum": active_control_frontier(self.db_path), "history": [dict(e) for e in events]})
        finally: con.close()

    def metrics_get(self) -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            self._schema(con)
            candidates = con.execute("SELECT candidate_class,domains_json FROM proactive_candidates ORDER BY candidate_id").fetchall()
            evaluations = con.execute("SELECT result,reason_codes_json FROM proactive_evaluations ORDER BY evaluation_id").fetchall()
            results = Counter(str(r["result"]) for r in evaluations); reasons = Counter()
            for row in evaluations: reasons.update(map(str, json.loads(str(row["reason_codes_json"]))))
            domains = Counter(d for row in candidates for d in json.loads(str(row["domains_json"])))
            feedback = Counter(str(r[0]) for r in con.execute("SELECT operation FROM proactive_control_events WHERE operation IN ('mark_not_useful','mark_wrong_timing','correct')"))
            return self._success("metrics.get", {"candidate_counts": dict(Counter(str(r["candidate_class"]) for r in candidates)), "domain_counts": dict(domains), "evaluation_counts": dict(results), "suppression_reason_counts": dict(reasons), "feedback_counts": dict(feedback), "control_frontier_checksum": active_control_frontier(self.db_path), "external_actions": 0, "network_calls": 0, "paid_calls": 0})
        finally: con.close()


__all__ = ["ProactiveIntelligenceService", "ProactiveServiceError", "INTERFACE_SCHEMA_VERSION"]
