"""Shared bounded read contract for External, Analysis, Pilot and Calibration."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, ROOT
from personal_knowledge.external_context.registry import ExternalContextService as SourceRegistryService
from personal_knowledge.external_context.service import ExternalContextService as ExternalFactService
from personal_knowledge.external_context.snapshots import ExternalSnapshotError, get_active_snapshot, get_snapshot
from personal_knowledge.intelligence.analysis.service import AnalysisReadError, AnalysisReadService
from personal_knowledge.intelligence.calibration.service import explain as explain_calibration
from personal_knowledge.intelligence.pilot.service import PilotServiceError, explain as explain_pilot, get_case, list_cases


INTERFACE_SCHEMA_VERSION = "decision_intelligence_read_v1"
DEFAULT_ANALYSIS_DB = ROOT / "var" / "db" / "decision_analysis.sqlite"
DEFAULT_PILOT_DB = ROOT / "var" / "db" / "project_pilot.sqlite"
DEFAULT_CALIBRATION_DB = ROOT / "var" / "db" / "recommendation_calibration.sqlite"


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


class DecisionIntelligenceReadService:
    def __init__(
        self, *, external_db: Path | str = EXTERNAL_CONTEXT_DB,
        analysis_db: Path | str = DEFAULT_ANALYSIS_DB,
        pilot_db: Path | str = DEFAULT_PILOT_DB,
        calibration_db: Path | str = DEFAULT_CALIBRATION_DB,
    ) -> None:
        self.external_db = Path(external_db)
        self.analysis_db = Path(analysis_db)
        self.pilot_db = Path(pilot_db)
        self.calibration_db = Path(calibration_db)

    @staticmethod
    def _privacy() -> dict[str, Any]:
        return {"metadata_only": True, "provider_bodies": 0, "credentials": 0, "writes": 0}

    @classmethod
    def _success(cls, operation: str, data: Any) -> dict[str, Any]:
        return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation, "ok": True, "status": "success", "data": data, "privacy": cls._privacy()}

    @classmethod
    def _error(cls, operation: str, code: str, detail: str = "") -> dict[str, Any]:
        return {"schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation, "ok": False, "status": "error", "error": {"code": code, "detail": detail}, "privacy": cls._privacy()}

    @staticmethod
    def _limit(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("invalid_limit")
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_limit") from exc
        if not 1 <= limit <= 100:
            raise ValueError("invalid_limit")
        return limit

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        try:
            handler = getattr(self, "_" + operation.replace(".", "_"), None)
            if handler is None:
                return self._error(operation, "unknown_operation", operation)
            return self._success(operation, handler(**params))
        except (AnalysisReadError, PilotServiceError, ExternalSnapshotError) as exc:
            return self._error(operation, getattr(exc, "code", "invalid_authority_state"), getattr(exc, "detail", str(exc)))
        except FileNotFoundError as exc:
            return self._error(operation, "database_missing", str(exc))
        except ValueError as exc:
            code = str(exc) if str(exc).startswith("invalid_") else "invalid_authority_state"
            return self._error(operation, code, "" if code == str(exc) else str(exc))
        except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
            return self._error(operation, "invalid_authority_state", str(exc))

    def _external_list(self, limit: Any = 50) -> dict[str, Any]:
        amount = self._limit(limit)
        sources = SourceRegistryService(db_path=self.external_db).invoke("sources.list")
        facts = ExternalFactService(self.external_db).invoke("facts.list", limit=amount)
        if not sources.get("ok"):
            raise ValueError("invalid_external_sources")
        if not facts.get("ok"):
            error = facts.get("error") or {}
            raise ExternalSnapshotError(str(error.get("code") or "invalid_external_state"), str(error.get("detail") or ""))
        active = ExternalFactService(self.external_db).invoke("snapshot.active")
        if not active.get("ok"):
            error = active.get("error") or {}
            raise ExternalSnapshotError(str(error.get("code") or "invalid_external_state"), str(error.get("detail") or ""))
        return {"sources": sources["data"]["items"], "snapshot": active["data"], "facts": facts["data"]["items"], "limit": amount}

    def _external_get(self, resource_type: str, resource_id: str | None = None) -> dict[str, Any]:
        if resource_type == "source":
            result = SourceRegistryService(db_path=self.external_db).invoke("sources.get", source_id=resource_id)
        elif resource_type == "fact":
            result = ExternalFactService(self.external_db).invoke("facts.get", fact_id=str(resource_id or ""))
        elif resource_type == "snapshot":
            item = get_snapshot(self.external_db, resource_id) if resource_id else get_active_snapshot(self.external_db)
            if item is None:
                raise ExternalSnapshotError("snapshot_missing", str(resource_id or "active"))
            return {"resource_type": resource_type, "item": item}
        else:
            raise ValueError("invalid_resource_type")
        if not result.get("ok"):
            error = result.get("error") or {}
            raise ExternalSnapshotError(str(error.get("code") or "invalid_external_state"), str(error.get("detail") or ""))
        return {"resource_type": resource_type, "item": result["data"]}

    def _external_explain(self, resource_type: str, resource_id: str | None = None) -> dict[str, Any]:
        return {**self._external_get(resource_type, resource_id), "limitations": ["allowlisted public metadata only", "external facts never become personal facts"], "next_actions": ["inspect another fact", "inspect the active snapshot"]}

    def _analysis_list(self, limit: Any = 50) -> dict[str, Any]:
        items = AnalysisReadService(self.analysis_db).list_runs(limit=self._limit(limit))
        return {"items": items, "count": len(items)}

    def _analysis_get(self, run_id: str) -> dict[str, Any]:
        return AnalysisReadService(self.analysis_db).get_run(run_id)

    def _analysis_explain(self, run_id: str) -> dict[str, Any]:
        return AnalysisReadService(self.analysis_db).explain(run_id)

    def _pilot_list(self, limit: Any = 50) -> dict[str, Any]:
        amount = self._limit(limit)
        items = list(list_cases(self.pilot_db))[:amount]
        return {"items": items, "count": len(items), "limit": amount}

    def _pilot_get(self, case_id: str) -> dict[str, Any]:
        return get_case(self.pilot_db, case_id)

    def _pilot_explain(self, case_id: str, as_of: str | None = None) -> dict[str, Any]:
        return explain_pilot(self.pilot_db, case_id, as_of=as_of)

    def _calibration_list(self, limit: Any = 50) -> dict[str, Any]:
        amount = self._limit(limit)
        con = _ro(self.calibration_db)
        try:
            ids = [str(row[0]) for row in con.execute("SELECT protocol_id FROM calibration_protocols ORDER BY frozen_at DESC,protocol_id DESC LIMIT ?", (amount,))]
        finally:
            con.close()
        return {"items": [{"protocol_id": item} for item in ids], "count": len(ids), "limit": amount}

    def _calibration_get(self, protocol_id: str) -> dict[str, Any]:
        view = explain_calibration(self.calibration_db, protocol_id)
        if not view["protocol"]:
            raise ValueError("invalid_protocol_id")
        return view

    def _calibration_explain(self, protocol_id: str) -> dict[str, Any]:
        return self._calibration_get(protocol_id)


__all__ = [
    "DEFAULT_ANALYSIS_DB", "DEFAULT_CALIBRATION_DB", "DEFAULT_PILOT_DB",
    "DecisionIntelligenceReadService", "INTERFACE_SCHEMA_VERSION",
]
