"""Deterministic non-mutating safety gates for analysis publication."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

from personal_knowledge.core.privacy_guard import guard_jsonable
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding,
    validate_decision_context_binding,
)

from .runs import load_policy
from .schema import canonical_json, checksum


REASON_ORDER = (
    "schema_invalid", "evidence_missing", "binding_drift", "privacy_risk",
    "stale_context", "unresolved_conflict", "region_mismatch", "prompt_injection",
    "forbidden_domain", "external_action_intent",
)
_INJECTION = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?(?:previous|prior)|system\s+prompt|developer\s+message|"
    r"jailbreak|override\s+(?:the\s+)?instructions|<\|(?:system|assistant)|"
    r"untrusted_evidence_(?:begin|end))"
)
_EXTERNAL_ACTION = re.compile(
    r"(?i)\b(deploy\s+(?:now|immediately|to\b)|execute|run\s+(?:the\s+)?command|send\s+(?:an?\s+)?(?:email|message)|"
    r"purchase|buy|transfer\s+(?:funds|money)|delete\s+(?:the\s+)?(?:file|record)|"
    r"call\s+(?:the\s+)?tool|dispatch)\b"
)
_ACTION_KEYS = frozenset({"command", "tool_call", "dispatch_target", "execute", "external_action"})


@dataclass(frozen=True)
class SafetyGateReceipt:
    status: str
    allowed: bool
    reason_codes: tuple[str, ...]
    input_checksum_before: str
    input_checksum_after: str
    output_checksum_before: str
    output_checksum_after: str
    authority_fingerprints_before: Mapping[str, str]
    authority_fingerprints_after: Mapping[str, str]
    unchanged: bool


def _fingerprint(path: Path | str) -> str:
    target = Path(path)
    if not target.exists():
        return "missing"
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(value: Any) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    action_keys: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _ACTION_KEYS:
                action_keys.append(str(key))
            child_text, child_keys = _walk(item)
            texts.extend(child_text); action_keys.extend(child_keys)
    elif isinstance(value, (tuple, list)):
        for item in value:
            child_text, child_keys = _walk(item)
            texts.extend(child_text); action_keys.extend(child_keys)
    elif isinstance(value, str):
        texts.append(value)
    return texts, action_keys


def _binding_reason(exc: Exception) -> str:
    code = str(getattr(exc, "code", "binding_drift"))
    if "stale" in code or "expired" in code or "lifecycle" in code:
        return "stale_context"
    if "conflict" in code:
        return "unresolved_conflict"
    if "region" in code:
        return "region_mismatch"
    return "binding_drift"


def evaluate_safety_gates(
    *,
    request_payload: Mapping[str, Any],
    response_payload: Mapping[str, Any],
    binding: DecisionContextBinding | Mapping[str, Any],
    personal_db_path: Path | str,
    external_db_path: Path | str,
    analysis_db_path: Path | str,
    policy_path: Path | str,
    now: str | None = None,
) -> SafetyGateReceipt:
    """Return an abstention receipt on any deterministic violation; write nothing."""
    paths = {
        "personal": personal_db_path, "external": external_db_path, "analysis": analysis_db_path,
    }
    before_files = {name: _fingerprint(path) for name, path in paths.items()}
    input_before, output_before = checksum(request_payload), checksum(response_payload)
    reasons: set[str] = set()
    try:
        policy, _ = load_policy(policy_path)
    except Exception:
        policy = {"domain": {"allow": ["project"]}}
        reasons.add("schema_invalid")
    try:
        validate_decision_context_binding(
            binding, personal_db_path, external_db_path, now=now,
        )
    except Exception as exc:
        reasons.add(_binding_reason(exc))
    for payload in (request_payload, response_payload):
        guarded, privacy = guard_jsonable(payload, mode="redact")
        if privacy.hit_count or canonical_json(guarded) != canonical_json(payload):
            reasons.add("privacy_risk")
    texts, action_keys = _walk((request_payload, response_payload))
    if any(_INJECTION.search(item) for item in texts):
        reasons.add("prompt_injection")
    if action_keys or any(_EXTERNAL_ACTION.search(item) for item in texts):
        reasons.add("external_action_intent")
    claims = response_payload.get("claims")
    if isinstance(claims, list) and any(
        isinstance(item, Mapping) and item.get("claim_type") == "factual" and not item.get("evidence")
        for item in claims
    ):
        reasons.add("evidence_missing")
    allowed_domains = set(policy.get("domain", {}).get("allow", ()))
    domains = {str(payload.get("domain")) for payload in (request_payload, response_payload)
               if payload.get("domain") is not None}
    if not domains or any(item not in allowed_domains for item in domains):
        reasons.add("forbidden_domain")
    input_after, output_after = checksum(request_payload), checksum(response_payload)
    after_files = {name: _fingerprint(path) for name, path in paths.items()}
    unchanged = (input_before == input_after and output_before == output_after and before_files == after_files)
    if not unchanged:
        reasons.add("external_action_intent")
    ordered = tuple(code for code in REASON_ORDER if code in reasons)
    return SafetyGateReceipt(
        status="pass" if not ordered else "abstain", allowed=not ordered,
        reason_codes=ordered, input_checksum_before=input_before, input_checksum_after=input_after,
        output_checksum_before=output_before, output_checksum_after=output_after,
        authority_fingerprints_before=before_files, authority_fingerprints_after=after_files,
        unchanged=unchanged,
    )


apply_safety_gates = evaluate_safety_gates

__all__ = ["REASON_ORDER", "SafetyGateReceipt", "apply_safety_gates", "evaluate_safety_gates"]
