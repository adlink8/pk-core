"""Strict parser for bounded structured decision-analysis candidates."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping

from .schema import (
    SCHEMA_VERSION, AnalysisClaim, AnalysisSchemaError, CandidateDraft,
    EvidenceReference, checksum, from_exact_mapping, reject_forbidden,
)


class CandidateParseError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


_FIELDS = {
    "schema_version", "binding_hash", "request_checksum", "domain", "status",
    "options", "no_action_baseline", "assumptions", "uncertainty",
    "missing_information", "stop_conditions", "abstain_reasons",
    "claims",
}
_TRADEOFF_FIELDS = ("benefits", "costs", "risks", "opportunity_cost")


def _text_list(value: Any, name: str, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value) or len(value) > 32:
        raise CandidateParseError("candidate_list_invalid", name)
    items = tuple(item.strip() for item in value if isinstance(item, str))
    if len(items) != len(value) or any(not item or len(item) > 1_000 for item in items):
        raise CandidateParseError("candidate_list_invalid", name)
    return items


def _tradeoff(value: Any, name: str, *, option: bool) -> dict[str, Any]:
    fields = {"benefits", "costs", "risks", "opportunity_cost", "reversibility"}
    if option:
        fields |= {"option_id", "title"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CandidateParseError("candidate_tradeoff_fields_invalid", name)
    result = dict(value)
    if option and (not isinstance(result["option_id"], str) or not result["option_id"].strip()
                   or not isinstance(result["title"], str) or not result["title"].strip()):
        raise CandidateParseError("candidate_option_identity_invalid", name)
    for field in _TRADEOFF_FIELDS:
        result[field] = list(_text_list(result[field], f"{name}.{field}", required=True))
    if result["reversibility"] not in {"high", "medium", "low", "irreversible"}:
        raise CandidateParseError("candidate_reversibility_invalid", name)
    return result


def parse_candidate_package(
    value: str | bytes | Mapping[str, Any],
    *,
    expected_binding_hash: str,
    expected_request_checksum: str,
    max_options: int = 8,
) -> tuple[CandidateDraft, tuple[AnalysisClaim, ...]]:
    try:
        payload = json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CandidateParseError("candidate_json_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise CandidateParseError("candidate_fields_mismatch")
    try:
        reject_forbidden(payload, "candidate_response")
    except AnalysisSchemaError as exc:
        raise CandidateParseError(exc.code, exc.detail) from exc
    if payload["schema_version"] != SCHEMA_VERSION:
        raise CandidateParseError("candidate_schema_drift")
    if payload["binding_hash"] != expected_binding_hash:
        raise CandidateParseError("candidate_binding_mismatch")
    if payload["request_checksum"] != expected_request_checksum:
        raise CandidateParseError("candidate_request_mismatch")
    status = payload["status"]
    if status not in {"candidate", "abstain"}:
        raise CandidateParseError("candidate_status_invalid")
    if not isinstance(payload["options"], list) or len(payload["options"]) > max_options:
        raise CandidateParseError("candidate_options_invalid")
    options = tuple(_tradeoff(item, f"options[{index}]", option=True)
                    for index, item in enumerate(payload["options"]))
    if len({item["option_id"] for item in options}) != len(options):
        raise CandidateParseError("candidate_option_id_duplicate")
    baseline = payload["no_action_baseline"]
    if status == "candidate":
        if not options:
            raise CandidateParseError("candidate_options_required")
        baseline = _tradeoff(baseline, "no_action_baseline", option=False)
    elif baseline not in ({}, None):
        baseline = _tradeoff(baseline, "no_action_baseline", option=False)
    abstain = _text_list(payload["abstain_reasons"], "abstain_reasons", required=status == "abstain")
    raw_claims = payload["claims"]
    if not isinstance(raw_claims, list) or len(raw_claims) > 64:
        raise CandidateParseError("candidate_claims_invalid")
    claims: list[AnalysisClaim] = []
    for index, raw_claim in enumerate(raw_claims):
        fields = {"claim_id", "claim_type", "statement", "evidence"}
        if not isinstance(raw_claim, Mapping) or set(raw_claim) != fields:
            raise CandidateParseError("candidate_claim_fields_invalid", str(index))
        raw_evidence = raw_claim["evidence"]
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 16:
            raise CandidateParseError("candidate_claim_evidence_invalid", str(index))
        try:
            evidence = tuple(from_exact_mapping(EvidenceReference, item) for item in raw_evidence)
            core = {
                "claim_id": raw_claim["claim_id"], "claim_type": raw_claim["claim_type"],
                "statement": raw_claim["statement"],
                "evidence": [asdict(item) for item in evidence],
            }
            claim = AnalysisClaim(
                claim_id=str(raw_claim["claim_id"]), claim_type=str(raw_claim["claim_type"]),
                statement=str(raw_claim["statement"]), evidence=evidence,
                claim_checksum=checksum(core),
            )
        except (AnalysisSchemaError, TypeError, ValueError) as exc:
            raise CandidateParseError("candidate_claim_invalid", str(index)) from exc
        claims.append(claim)
    if len({item.claim_id for item in claims}) != len(claims):
        raise CandidateParseError("candidate_claim_id_duplicate")
    try:
        draft = CandidateDraft(
            domain=str(payload["domain"]), status=status, options=options,
            no_action_baseline=baseline or {},
            assumptions=_text_list(payload["assumptions"], "assumptions", required=status == "candidate"),
            uncertainty=_text_list(payload["uncertainty"], "uncertainty", required=status == "candidate"),
            missing_information=_text_list(payload["missing_information"], "missing_information", required=status == "candidate"),
            stop_conditions=_text_list(payload["stop_conditions"], "stop_conditions", required=status == "candidate"),
            abstain_reasons=abstain,
        )
    except AnalysisSchemaError as exc:
        raise CandidateParseError(exc.code, exc.detail) from exc
    return draft, tuple(claims)


def parse_candidate_response(
    value: str | bytes | Mapping[str, Any],
    *,
    expected_binding_hash: str,
    expected_request_checksum: str,
    max_options: int = 8,
) -> CandidateDraft:
    draft, _ = parse_candidate_package(
        value, expected_binding_hash=expected_binding_hash,
        expected_request_checksum=expected_request_checksum, max_options=max_options,
    )
    return draft


parse_candidate = parse_candidate_response

__all__ = [
    "CandidateParseError", "parse_candidate", "parse_candidate_package",
    "parse_candidate_response",
]
