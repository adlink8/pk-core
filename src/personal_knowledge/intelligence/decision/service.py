"""Shared, checksum-verifying read service for decision feedback."""
from __future__ import annotations

from dataclasses import asdict
import base64
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from personal_knowledge.intelligence.service import IntelligenceService

from .schema import canonical_json, checksum
from .state_machine import DecisionStateError, project_history


INTERFACE_SCHEMA_VERSION = "decision_feedback_interface_v1"
MAX_LIMIT = 100


class DecisionServiceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise DecisionServiceError("database_missing", str(db_path))
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _json(row: sqlite3.Row, field: str, code: str) -> dict[str, Any]:
    try:
        value = json.loads(str(row[field]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise DecisionServiceError(code, str(row[0])) from exc
    if not isinstance(value, dict):
        raise DecisionServiceError(code, str(row[0]))
    return value


class DecisionFeedbackService:
    """One non-mutating backend shared by CLI, REST and MCP."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

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

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        handlers = {
            "recommendations.list": self.recommendations_list,
            "recommendations.get": self.recommendations_get,
            "recommendations.history": self.recommendations_history,
            "recommendations.outcomes": self.recommendations_outcomes,
            "recommendations.effectiveness": self.recommendations_effectiveness,
        }
        handler = handlers.get(operation)
        if handler is None:
            return self._error(operation, "unknown_operation", operation)
        try:
            return handler(**params)
        except DecisionServiceError as exc:
            return self._error(operation, exc.code, exc.detail)
        except DecisionStateError as exc:
            return self._error(operation, exc.code, exc.detail)
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._error(operation, "invalid_decision_state", str(exc))

    def _limit(self, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
            raise DecisionServiceError("invalid_limit", str(value))
        return value

    def _context(self, con: sqlite3.Connection, recommendation_id: str) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any]]:
        rec = con.execute(
            "SELECT * FROM decision_recommendations WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if rec is None:
            raise DecisionServiceError("recommendation_missing", recommendation_id)
        run = con.execute("SELECT * FROM decision_runs WHERE run_id=?", (rec["run_id"],)).fetchone()
        if run is None:
            raise DecisionServiceError("decision_run_missing", str(rec["run_id"]))
        payload = _json(rec, "payload_json", "recommendation_payload_invalid")
        if checksum(payload) != str(rec["payload_checksum"]):
            raise DecisionServiceError("recommendation_checksum_mismatch", recommendation_id)
        expected = {
            "recommendation_id": recommendation_id,
            "run_id": str(run["run_id"]),
            "source_run_id": str(run["source_run_id"]),
            "source_run_checksum": str(run["source_run_checksum"]),
            "snapshot_id": str(run["snapshot_id"]),
            "snapshot_hash": str(run["snapshot_hash"]),
            "cognitive_type": "recommendation",
            "authority_id": "a.decision_feedback",
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise DecisionServiceError("recommendation_binding_mismatch", recommendation_id)
        manifests: dict[str, dict[str, Any]] = {}
        for name in ("input_manifest", "output_manifest"):
            manifest = _json(run, f"{name}_json", "decision_run_manifest_invalid")
            if checksum(manifest) != str(run[f"{name}_checksum"]):
                raise DecisionServiceError("decision_run_checksum_mismatch", name)
            manifests[name] = manifest
        output = manifests["output_manifest"]
        rec_manifest = output.get("recommendations")
        if not isinstance(rec_manifest, list):
            raise DecisionServiceError("decision_run_manifest_invalid", "recommendations")
        core = {
            "schema_version": output.get("schema_version"),
            "run_id": str(run["run_id"]),
            "source_run_id": str(run["source_run_id"]),
            "source_run_checksum": str(run["source_run_checksum"]),
            "source_publication_sequence": int(run["source_publication_sequence"]),
            "snapshot_id": str(run["snapshot_id"]),
            "snapshot_hash": str(run["snapshot_hash"]),
            "policy_id": str(run["policy_id"]),
            "policy_version": str(run["policy_version"]),
            "input_manifest_checksum": str(run["input_manifest_checksum"]),
            "recommendations": rec_manifest,
        }
        if checksum(core) != str(run["run_checksum"]):
            raise DecisionServiceError("decision_run_checksum_mismatch", "run_checksum")
        expected_output = {
            **core,
            "run_checksum": str(run["run_checksum"]),
            "genesis_events": output.get("genesis_events"),
        }
        if canonical_json(expected_output) != canonical_json(output):
            raise DecisionServiceError("decision_run_manifest_invalid", "output_manifest")
        support_rows = con.execute(
            "SELECT * FROM decision_support_refs WHERE recommendation_id=? ORDER BY support_id",
            (recommendation_id,),
        ).fetchall()
        expected_support = payload.get("support")
        if not isinstance(expected_support, list) or len(support_rows) != len(expected_support):
            raise DecisionServiceError("support_manifest_mismatch", recommendation_id)
        actual_support: list[dict[str, Any]] = []
        for support in support_rows:
            support_payload = _json(support, "payload_json", "support_payload_invalid")
            if checksum(support_payload) != str(support["payload_checksum"]):
                raise DecisionServiceError("support_checksum_mismatch", str(support["support_id"]))
            if (
                support_payload.get("authority_id") != "a.personal_change"
                or support_payload.get("cognitive_type") not in {"fact", "observation", "inference"}
                or support_payload.get("provenance_class") != support_payload.get("cognitive_type")
                or support_payload.get("source_run_id") != str(run["source_run_id"])
                or support_payload.get("source_run_checksum") != str(run["source_run_checksum"])
                or support_payload.get("source_publication_sequence") != int(run["source_publication_sequence"])
                or support_payload.get("snapshot_id") != str(run["snapshot_id"])
                or support_payload.get("snapshot_hash") != str(run["snapshot_hash"])
            ):
                raise DecisionServiceError("support_binding_mismatch", str(support["support_id"]))
            actual_support.append(support_payload)
        if sorted(map(canonical_json, actual_support)) != sorted(map(canonical_json, expected_support)):
            raise DecisionServiceError("support_manifest_mismatch", recommendation_id)
        source = IntelligenceService(self.db_path).invoke(
            "state.history", run_id=str(run["source_run_id"]), limit=1
        )
        if not source.get("ok"):
            error = source.get("error", {})
            raise DecisionServiceError(
                "source_analysis_invalid", str(error.get("code") or "unknown")
            )
        if (
            source["run"]["run_checksum"] != str(run["source_run_checksum"])
            or source["snapshot"]["snapshot_id"] != str(run["snapshot_id"])
            or source["snapshot"]["snapshot_hash"] != str(run["snapshot_hash"])
        ):
            raise DecisionServiceError("source_analysis_version_drift", recommendation_id)
        # This is the authoritative genesis/typed-row/checksum-chain verifier.
        state = project_history(self.db_path, recommendation_id)
        return rec, run, {"payload": payload, "state": state}

    @staticmethod
    def _metadata(rec: sqlite3.Row, run: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "recommendation_id": str(rec["recommendation_id"]),
            "recommendation_checksum": str(rec["payload_checksum"]),
            "cognitive_type": "recommendation",
            "run_id": str(run["run_id"]),
            "run_checksum": str(run["run_checksum"]),
            "source_run_id": str(run["source_run_id"]),
            "source_run_checksum": str(run["source_run_checksum"]),
            "source_publication_sequence": int(run["source_publication_sequence"]),
            "snapshot_id": str(run["snapshot_id"]),
            "snapshot_hash": str(run["snapshot_hash"]),
            "policy_id": str(run["policy_id"]),
            "policy_version": str(run["policy_version"]),
            "subject": str(rec["subject"]),
            "domain": str(rec["domain"]),
            "scope": str(rec["scope"]),
            "recommendation_kind": str(rec["recommendation_kind"]),
            "horizon": str(rec["horizon"]),
            "confidence": float(rec["confidence"]),
            "uncertainty": str(rec["uncertainty"]),
            "expires_at": str(rec["expires_at"]),
            "rationale_codes": list(payload.get("rationale_codes") or ()),
            "support": [
                {
                    key: item.get(key)
                    for key in (
                        "cognitive_type", "authority_id", "record_id", "source_run_id",
                        "source_run_checksum", "source_publication_sequence", "snapshot_id",
                        "snapshot_hash", "provenance_class", "evidence_status",
                        "uncertainty", "record_checksum",
                    )
                }
                for item in payload.get("support", ()) if isinstance(item, Mapping)
            ],
        }

    def _success(self, operation: str, items: Any, *, total: int | None = None, limit: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"items": items} if isinstance(items, list) else dict(items)
        if total is not None:
            data["total_available"] = total
        if limit is not None:
            data["limit"] = limit
        empty = (isinstance(items, list) and not items)
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION,
            "operation": operation,
            "ok": True,
            "status": "empty" if empty else "success",
            "data": data,
            "privacy": {"metadata_only": True, "private_bodies": 0},
        }

    def recommendations_list(
        self, *, limit: int = 50, domain: str | None = None, cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = self._limit(limit)
        cursor_created_at: str | None = None
        cursor_rid: str | None = None
        if cursor:
            # 不透明游标:仅编码 (created_at, recommendation_id);解码失败 → fail-closed
            cursor_created_at, cursor_rid = self._decode_cursor(cursor)

        con = _ro(self.db_path)
        try:
            domain_conditions: list[str] = []
            domain_args: list[Any] = []
            if domain:
                domain_conditions.append("domain=?")
                domain_args.append(domain)
            domain_where = (" WHERE " + " AND ".join(domain_conditions)) if domain_conditions else ""
            # total_available = 匹配 domain 过滤的全量(忽略游标),保证分页视图计数稳定
            total = con.execute(
                "SELECT COUNT(*) FROM decision_recommendations" + domain_where, domain_args,
            ).fetchone()[0]

            conditions = list(domain_conditions)
            params: list[Any] = list(domain_args)
            if cursor_created_at is not None:
                conditions.append("(created_at, recommendation_id) < (?, ?)")
                params.extend([cursor_created_at, cursor_rid])
            where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            # 取 limit+1 行以稳定判定 has_more;newest-first 保证「加载更早」语义正确
            rows = con.execute(
                "SELECT recommendation_id, created_at FROM decision_recommendations"
                + where_sql
                + " ORDER BY created_at DESC, recommendation_id DESC LIMIT ?",
                params + [limit + 1],
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            items: list[dict[str, Any]] = []
            for row in page:
                recommendation_id = str(row[0])
                rec, run, context = self._context(con, recommendation_id)
                item = self._metadata(rec, run, context["payload"])
                item["confirmation_state"] = context["state"].confirmation_state
                item["action_state"] = context["state"].action_state
                item["current_sequence"] = context["state"].events[-1].sequence
                items.append(item)
            next_cursor: str | None = None
            if has_more and page:
                last = page[-1]
                next_cursor = self._encode_cursor(str(last[1]), str(last[0]))
            return self._success(
                "recommendations.list",
                {"items": items, "next_cursor": next_cursor},
                total=total, limit=limit,
            )
        finally:
            con.close()

    @staticmethod
    def _encode_cursor(created_at: str, recommendation_id: str) -> str:
        raw = f"{created_at}|{recommendation_id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        """解码不透明游标;任何格式错误 → DecisionServiceError('cursor_invalid')。"""
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        except Exception:
            raise DecisionServiceError("cursor_invalid", "decode_failed")
        parts = raw.split("|")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise DecisionServiceError("cursor_invalid", "malformed")
        return parts[0], parts[1]

    def recommendations_get(self, *, recommendation_id: str) -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            rec, run, context = self._context(con, recommendation_id)
            item = self._metadata(rec, run, context["payload"])
            item["confirmation_state"] = context["state"].confirmation_state
            item["action_state"] = context["state"].action_state
            item["current_sequence"] = context["state"].events[-1].sequence
            return self._success("recommendations.get", item)
        finally:
            con.close()

    def recommendations_history(self, *, recommendation_id: str, limit: int = 100) -> dict[str, Any]:
        limit = self._limit(limit)
        con = _ro(self.db_path)
        try:
            _, _, context = self._context(con, recommendation_id)
            state = context["state"]
            rows = [
                {
                    "event_id": event.event_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "typed_record_id": event.typed_record_id,
                    "previous_event_checksum": event.previous_event_checksum,
                    "payload_checksum": event.payload_checksum,
                }
                for event in state.events
            ]
            return self._success("recommendations.history", rows[:limit], total=len(rows), limit=limit)
        finally:
            con.close()

    def _typed_rows(self, recommendation_id: str, table: str, id_column: str) -> list[dict[str, Any]]:
        con = _ro(self.db_path)
        try:
            self._context(con, recommendation_id)
            rows = con.execute(
                f"SELECT * FROM {table} WHERE recommendation_id=? ORDER BY created_at,{id_column}",
                (recommendation_id,),
            ).fetchall()
            result = []
            for row in rows:
                payload = _json(row, "payload_json", "typed_record_payload_invalid")
                if checksum(payload) != str(row["payload_checksum"]):
                    raise DecisionServiceError("typed_record_checksum_mismatch", str(row[id_column]))
                result.append({
                    id_column: str(row[id_column]),
                    "recommendation_id": recommendation_id,
                    "payload_checksum": str(row["payload_checksum"]),
                    "record_type": payload.get("record_type"),
                    "cognitive_type": payload.get("cognitive_type"),
                    "causal_claim": payload.get("causal_claim"),
                    "metric": payload.get("metric"),
                    "unit": payload.get("unit"),
                    "adherence_status": payload.get("adherence_status"),
                    "rule_id": payload.get("rule_id"),
                    "rule_version": payload.get("rule_version"),
                    "verdict": payload.get("verdict"),
                    "uncertainty": payload.get("uncertainty"),
                })
            return result
        finally:
            con.close()

    def recommendations_outcomes(self, *, recommendation_id: str, limit: int = 50) -> dict[str, Any]:
        limit = self._limit(limit)
        rows = self._typed_rows(recommendation_id, "decision_outcomes", "outcome_id")
        return self._success("recommendations.outcomes", rows[:limit], total=len(rows), limit=limit)

    def recommendations_effectiveness(self, *, recommendation_id: str, limit: int = 50) -> dict[str, Any]:
        limit = self._limit(limit)
        rows = self._typed_rows(recommendation_id, "decision_effectiveness", "assessment_id")
        return self._success("recommendations.effectiveness", rows[:limit], total=len(rows), limit=limit)


__all__ = ["DecisionFeedbackService", "DecisionServiceError", "INTERFACE_SCHEMA_VERSION"]
