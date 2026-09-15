"""Shared bounded transport contract for guarded decision sessions."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, ROOT, UNIFIED_DB
from personal_knowledge.intelligence.orchestration import (
    OrchestrationError,
    OrchestrationService,
    Preview,
    execute_calibrate,
    execute_confirmed_generation,
    execute_decide,
    execute_manual_action,
    execute_observe,
    execute_preregister,
    execute_publish,
)


INTERFACE_SCHEMA_VERSION = "guarded_orchestration_interface_v1"
DEFAULT_ORCHESTRATION_DB = ROOT / "var" / "db" / "decision_orchestration.sqlite"
DEFAULT_ANALYSIS_DB = ROOT / "var" / "db" / "decision_analysis.sqlite"
DEFAULT_PILOT_DB = ROOT / "var" / "db" / "project_pilot.sqlite"
DEFAULT_CALIBRATION_DB = ROOT / "var" / "db" / "recommendation_calibration.sqlite"


class GuardedOrchestrationInterface:
    def __init__(
        self,
        *,
        service: OrchestrationService | None = None,
        orchestration_db: Path | str = DEFAULT_ORCHESTRATION_DB,
        personal_db: Path | str = UNIFIED_DB,
        external_db: Path | str = EXTERNAL_CONTEXT_DB,
        analysis_db: Path | str = DEFAULT_ANALYSIS_DB,
        pilot_db: Path | str = DEFAULT_PILOT_DB,
        calibration_db: Path | str = DEFAULT_CALIBRATION_DB,
        confirmation_secret: bytes | None = None,
        generation_runner=None,
        calibration_runner=None,
    ) -> None:
        if service is None:
            secret = confirmation_secret or os.environ.get("PERSONAL_DATA_ORCHESTRATION_SECRET", "").encode()
            if len(secret) < 32:
                raise OrchestrationError("confirmation_secret_unavailable")
            service = OrchestrationService(
                db_path=orchestration_db,
                personal_db=personal_db,
                external_db=external_db,
                confirmation_secret=secret,
            )
        self.service = service
        self.analysis_db = Path(analysis_db)
        self.pilot_db = Path(pilot_db)
        self.calibration_db = Path(calibration_db)
        if generation_runner is None:
            # Normal generation is Pi-owned. The adapter preserves the
            # existing Python validation/publication contract, while the
            # actual provider task, session and model receipt are created by
            # the loopback Kernel route.
            from personal_knowledge.intelligence.analysis.providers import PiKernelProvider
            from personal_knowledge.intelligence.orchestration.generation import ExistingAnalysisAdapter
            generation_runner = ExistingAnalysisAdapter(
                provider=PiKernelProvider(purpose="guarded_generation"),
                personal_db=personal_db,
                external_db=external_db,
                analysis_db=analysis_db,
            )
        self.generation_runner = generation_runner
        self.calibration_runner = calibration_runner

    @staticmethod
    def _envelope(operation: str, *, ok: bool, data: Any = None, code: str = "", detail: str = "") -> dict[str, Any]:
        result = {
            "schema_version": INTERFACE_SCHEMA_VERSION,
            "operation": operation,
            "ok": ok,
            "status": "success" if ok else "error",
            "limitations": [
                "project domain and low risk only",
                "explicit confirmation required for every write",
                "no automated external action",
                "no automatic calibration promotion",
            ],
        }
        if ok:
            result["data"] = data
        else:
            result["error"] = {"code": code, "detail": detail}
        return result

    @staticmethod
    def _only(params: Mapping[str, Any], allowed: set[str]) -> None:
        extra = sorted(set(params) - allowed)
        if extra:
            raise OrchestrationError("undeclared_input", ",".join(extra))

    @staticmethod
    def _result(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        return value

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        try:
            handler = getattr(self, "_" + operation.replace(".", "_"), None)
            if handler is None:
                return self._envelope(operation, ok=False, code="unknown_operation", detail=operation)
            return self._envelope(operation, ok=True, data=self._result(handler(params)))
        except KeyError as exc:
            return self._envelope(operation, ok=False, code="missing_parameter", detail=str(exc.args[0]))
        except Exception as exc:
            code = str(getattr(exc, "code", "") or str(exc) or "invalid_request").split(":", 1)[0]
            detail = str(getattr(exc, "detail", "") or "")
            return self._envelope(operation, ok=False, code=code, detail=detail)

    def _session_prepare(self, p: Mapping[str, Any]):
        self._only(p, {"goal", "constraints", "weights", "actor_identity_hash", "domain", "risk_budget", "region", "max_external_age_seconds", "now"})
        return self.service.prepare(**p).to_dict()

    def _session_confirm(self, p: Mapping[str, Any]):
        self._only(p, {"preview", "confirmation_token", "confirmed", "idempotency_key", "now"})
        preview = Preview.from_dict(p["preview"])
        token = p.get("confirmation_token")
        if not token:
            if p.get("confirmed") is not True:
                raise OrchestrationError("explicit_confirmation_required")
            token = self.service.issue_confirmation(preview)
        return self.service.confirm(
            preview, confirmation_token=token,
            idempotency_key=p["idempotency_key"], now=p.get("now"),
        )

    def _session_preview(self, p: Mapping[str, Any]):
        self._only(p, {"session_id", "transition", "payload", "actor_identity_hash", "expected_sequence", "now"})
        return self.service.preview_transition(
            p["session_id"], p["transition"], p.get("payload") or {},
            actor_identity_hash=p["actor_identity_hash"],
            expected_sequence=int(p["expected_sequence"]), now=p.get("now"),
        ).to_dict()

    def _session_execute(self, p: Mapping[str, Any]):
        self._only(p, {"preview", "confirmation_token", "confirmed", "idempotency_key", "now"})
        preview = Preview.from_dict(p["preview"])
        token = p.get("confirmation_token")
        if not token:
            if p.get("confirmed") is not True:
                raise OrchestrationError("explicit_confirmation_required")
            token = self.service.issue_confirmation(preview)
        common = {
            "confirmation_token": token,
            "idempotency_key": p["idempotency_key"],
            "now": p.get("now"),
        }
        if not common["now"]:
            raise OrchestrationError("timestamp_required")
        if preview.operation == "generate":
            if self.generation_runner is None:
                raise OrchestrationError("generation_provider_unavailable")
            return execute_confirmed_generation(self.service, preview, runner=self.generation_runner, **common)
        if preview.operation == "publish":
            return execute_publish(self.service, preview, pilot_db=self.pilot_db, analysis_db=self.analysis_db, **common)
        if preview.operation == "decide":
            return execute_decide(self.service, preview, pilot_db=self.pilot_db, **common)
        if preview.operation == "preregister":
            return execute_preregister(self.service, preview, pilot_db=self.pilot_db, **common)
        if preview.operation in {"action_start", "action_complete"}:
            return execute_manual_action(self.service, preview, pilot_db=self.pilot_db, **common)
        if preview.operation == "observe":
            return execute_observe(self.service, preview, pilot_db=self.pilot_db, **common)
        if preview.operation == "calibrate":
            return execute_calibrate(
                self.service, preview, calibration_db=self.calibration_db,
                calibration_runner=self.calibration_runner, **common,
            )
        raise OrchestrationError("operation_unknown")

    def _session_resume(self, p: Mapping[str, Any]):
        self._only(p, {"session_id", "now"})
        return self.service.get(p["session_id"], now=p.get("now"))

    def _session_explain(self, p: Mapping[str, Any]):
        self._only(p, {"session_id", "now"})
        return self.service.explain(p["session_id"], now=p.get("now"))


__all__ = ["GuardedOrchestrationInterface", "INTERFACE_SCHEMA_VERSION"]
