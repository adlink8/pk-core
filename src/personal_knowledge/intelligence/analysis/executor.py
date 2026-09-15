"""Single ordered executor for provider replay, validation, gates and publication."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from personal_knowledge.intelligence.decision.context_binding import DecisionContextBinding

from personal_knowledge.core.runtime_config import analysis_max_attempts

from .candidates import parse_candidate_package
from .evidence import validate_claim_evidence
from .gates import evaluate_safety_gates
from .inputs import ConfirmationEvent, PROMPT_VERSION, build_confirmed_input
from .providers import AnalysisProvider, ProviderError, ProviderRequest, ProviderResult, ProviderTimeout
from .runs import plan_run, publish_run
from .schema import AnalysisClaim, EvidenceReference, ProviderReceipt, SCHEMA_VERSION


@dataclass(frozen=True)
class ExecutionReceipt:
    status: str
    stage: str
    reason_codes: tuple[str, ...]
    attempts: int
    request_checksum: str | None = None
    response_checksum: str | None = None
    run_id: str | None = None
    candidate_id: str | None = None
    written: bool = False
    existing: bool = False
    telemetry: Mapping[str, Any] | None = None


def _abstain(
    stage: str,
    code: str | Iterable[str],
    *,
    attempts: int = 0,
    request_checksum: str | None = None,
    response_checksum: str | None = None,
    telemetry: Mapping[str, Any] | None = None,
) -> ExecutionReceipt:
    reasons = (code,) if isinstance(code, str) else tuple(code)
    return ExecutionReceipt(
        status="abstain", stage=stage, reason_codes=reasons, attempts=attempts,
        request_checksum=request_checksum, response_checksum=response_checksum,
        telemetry=telemetry,
    )


def execute_analysis(
    *,
    provider: AnalysisProvider,
    binding: DecisionContextBinding | Mapping[str, Any],
    personal_db_path: Path | str,
    external_db_path: Path | str,
    analysis_db_path: Path | str,
    policy_path: Path | str,
    goal: str,
    constraints: Iterable[str],
    weights: Mapping[str, float],
    risk_budget: str,
    confirmation: ConfirmationEvent | Mapping[str, Any],
    personal_evidence: Iterable[EvidenceReference | Mapping[str, Any]],
    external_evidence: Iterable[EvidenceReference | Mapping[str, Any]],
    domain: str = "project",
    temperature: float = 0.0,
    max_output_tokens: int = 4096,
    timeout_seconds: float = 30.0,
    max_attempts: int | None = None,
    max_total_tokens: int = 8192,
    write: bool = False,
    fault_at: str | None = None,
    now: str | None = None,
) -> ExecutionReceipt:
    """Execute input→provider→parse→evidence→safety→publish/abstain in order."""
    # Retry budget default is read from configuration (env / pi-budget.json);
    # the safe 1..3 window is unchanged.
    if max_attempts is None:
        max_attempts = analysis_max_attempts()
    if not 1 <= max_attempts <= 3:
        return _abstain("input", "provider_attempt_budget_invalid")
    try:
        confirmed = build_confirmed_input(
            binding=binding, personal_db_path=personal_db_path,
            external_db_path=external_db_path, goal=goal, constraints=constraints,
            weights=weights, risk_budget=risk_budget, confirmation=confirmation,
            personal_evidence=personal_evidence, external_evidence=external_evidence,
            domain=domain, policy_path=policy_path, now=now,
            temperature=temperature, max_output_tokens=max_output_tokens,
        )
        provider_request = ProviderRequest(
            prompt=confirmed.rendered_prompt, request_checksum=confirmed.request_checksum,
            temperature=temperature, max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return _abstain("input", str(getattr(exc, "code", "input_invalid")))

    result: ProviderResult | None = None
    attempts = 0
    last_error = "provider_error"
    while attempts < max_attempts:
        attempts += 1
        try:
            result = provider.generate(provider_request)
            break
        except ProviderTimeout:
            last_error = "provider_timeout"
        except ProviderError as exc:
            last_error = exc.code
            if not exc.retryable:
                break
        except Exception:
            last_error = "provider_error"
            break
    if result is None:
        return _abstain("provider", last_error, attempts=attempts,
                        request_checksum=confirmed.request_checksum)
    telemetry = asdict(result.telemetry)
    if (result.telemetry.output_tokens > max_output_tokens
            or result.telemetry.input_tokens + result.telemetry.output_tokens > max_total_tokens):
        return _abstain(
            "provider", "usage_limit_exceeded", attempts=attempts,
            request_checksum=confirmed.request_checksum,
            response_checksum=result.response_checksum, telemetry=telemetry,
        )
    try:
        candidate, claim_items = parse_candidate_package(
            result.response_payload,
            expected_binding_hash=str(confirmed.request_manifest["binding_hash"]),
            expected_request_checksum=confirmed.request_checksum,
        )
    except Exception as exc:
        return _abstain(
            "parse", str(getattr(exc, "code", "schema_invalid")), attempts=attempts,
            request_checksum=confirmed.request_checksum,
            response_checksum=result.response_checksum, telemetry=telemetry,
        )
    try:
        validate_claim_evidence(
            claim_items, allowlist=confirmed.request_manifest["evidence_allowlist"],
            binding=binding, personal_db_path=personal_db_path,
            external_db_path=external_db_path, now=now,
        )
    except Exception as exc:
        return _abstain(
            "evidence", str(getattr(exc, "code", "evidence_missing")), attempts=attempts,
            request_checksum=confirmed.request_checksum,
            response_checksum=result.response_checksum, telemetry=telemetry,
        )
    safety = evaluate_safety_gates(
        request_payload=confirmed.request_manifest, response_payload=result.response_payload,
        binding=binding, personal_db_path=personal_db_path,
        external_db_path=external_db_path, analysis_db_path=analysis_db_path,
        policy_path=policy_path, now=now,
    )
    if not safety.allowed:
        return _abstain(
            "safety", safety.reason_codes, attempts=attempts,
            request_checksum=confirmed.request_checksum,
            response_checksum=result.response_checksum, telemetry=telemetry,
        )
    provider_receipt = ProviderReceipt(
        provider=result.telemetry.provider, model=result.telemetry.model,
        prompt_version=str(confirmed.request_manifest.get("prompt_version") or PROMPT_VERSION),
        schema_version=SCHEMA_VERSION,
        policy_version=str(confirmed.request_manifest["policy_version"]),
        temperature=temperature, max_output_tokens=max_output_tokens,
        input_tokens=result.telemetry.input_tokens, output_tokens=result.telemetry.output_tokens,
        cost_amount=result.telemetry.cost_amount, cost_currency=result.telemetry.cost_currency,
        latency_ms=result.telemetry.latency_ms,
        request_checksum=confirmed.request_checksum,
        response_checksum=result.response_checksum, status=result.telemetry.status,
    )
    try:
        run = plan_run(
            binding=binding, policy_path=policy_path,
            request_manifest=confirmed.request_manifest,
            response_manifest=result.response_payload, candidate=candidate,
            claims=claim_items, receipt=provider_receipt,
        )
    except Exception as exc:
        return _abstain(
            "plan", str(getattr(exc, "code", "run_planning_fault")), attempts=attempts,
            request_checksum=confirmed.request_checksum,
            response_checksum=result.response_checksum, telemetry=telemetry,
        )
    try:
        published = publish_run(
            analysis_db_path, run, policy_path=policy_path, write=write, fault_at=fault_at,
        )
    except Exception:
        return _abstain(
            "publish", "publication_fault", attempts=attempts,
            request_checksum=confirmed.request_checksum,
            response_checksum=result.response_checksum, telemetry=telemetry,
        )
    status = "abstain" if candidate.status == "abstain" else "candidate"
    reasons = candidate.abstain_reasons if candidate.status == "abstain" else ()
    return ExecutionReceipt(
        status=status, stage="publish", reason_codes=tuple(reasons), attempts=attempts,
        request_checksum=confirmed.request_checksum,
        response_checksum=result.response_checksum, run_id=run.run_id,
        candidate_id=run.candidate.candidate_id, written=bool(published.get("written")),
        existing=bool(published.get("existing")), telemetry=telemetry,
    )


run_analysis = execute_analysis

__all__ = ["ExecutionReceipt", "execute_analysis", "run_analysis"]
