"""Deterministic evidence support decisions for retrieval candidates.

The decision is deliberately independent from evaluation labels and vector-score
thresholds.  It combines candidate lifecycle/privacy metadata, lexical grounding
between the query and candidate fields, and typed evidence resolution.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Callable, Iterable, Mapping


_BLOCKED_LIFECYCLES = {"deprecated", "superseded", "conflict", "retracted", "deleted"}
_BLOCKED_PRIVACY = {"secret", "blocked", "excluded", "system", "private_secret"}
_GENERIC_TOKENS = {
    "用户", "个人", "信息", "内容", "数据", "查询", "问题", "答案", "什么", "多少",
    "query", "case", "content", "private", "answer", "data", "user",
    "no", "the", "has", "what", "with", "from", "about",
}
_SENSITIVE_VALUE_REQUEST_RE = re.compile(
    r"(?:护照(?:号码|号)?|身份证(?:号码|号)?|银行卡|信用卡|支付账户(?:明细)?|"
    r"部署密钥|私钥|助记词|密码|口令|api[ _-]?key|access[ _-]?token|secret)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceSupportDecision:
    state: str
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    features: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        value["evidence_refs"] = list(self.evidence_refs)
        return value


def _tokens(value: str) -> set[str]:
    normalized = str(value or "").lower()
    latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", re.sub(r"\s+", "", normalized))
    cjk = {
        run[index : index + 2]
        for run in cjk_runs
        for index in range(max(0, len(run) - 1))
    }
    return (latin | cjk) - _GENERIC_TOKENS


def _refs(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    raw: list[Any] = []
    for key in ("evidence_refs", "source_message_ref", "canonical_message_id", "evidence_ref"):
        value = candidate.get(key)
        if isinstance(value, (list, tuple, set)):
            raw.extend(value)
        elif value:
            raw.append(value)
    return tuple(sorted({str(value) for value in raw if str(value)}))


def _required_literals(query: str) -> tuple[str, ...]:
    patterns = (
        r"仅当证据逐字包含(?:校验码)?\s*([^\s，。；！？]+)",
        r"only\s+answer\s+if\s+(?:the\s+)?evidence\s+(?:literally\s+)?contains\s+([^\s,.;!?]+)",
    )
    found: set[str] = set()
    for pattern in patterns:
        found.update(re.findall(pattern, query, flags=re.IGNORECASE))
    return tuple(sorted(found))


def decide_evidence_support(
    query: str,
    candidate: Mapping[str, Any],
    *,
    resolve: Callable[[str], Mapping[str, Any]] | None = None,
    resolved_evidence: Iterable[Mapping[str, Any]] | None = None,
) -> EvidenceSupportDecision:
    """Return supported/unsupported/uncertain with stable reason codes.

    ``resolve`` is injected so unit tests and non-default stores remain isolated.
    Expected labels and evaluation-only fields are never inspected.
    """
    lifecycle = str(candidate.get("lifecycle") or "current").lower()
    privacy = str(
        candidate.get("privacy_tier")
        or candidate.get("privacy_class")
        or candidate.get("evidence_scope")
        or ""
    ).lower()
    refs = _refs(candidate)

    if _SENSITIVE_VALUE_REQUEST_RE.search(query):
        return EvidenceSupportDecision(
            "unsupported", ("sensitive_value_request",), refs,
            {"query_safety_veto": True},
        )

    if lifecycle in _BLOCKED_LIFECYCLES:
        return EvidenceSupportDecision(
            "unsupported", ("lifecycle_not_current",), refs,
            {"lifecycle": lifecycle},
        )
    if privacy in _BLOCKED_PRIVACY or bool(candidate.get("source_evidence_ineligible")):
        return EvidenceSupportDecision(
            "unsupported", ("privacy_or_provenance_veto",), refs,
            {"privacy": privacy},
        )

    candidate_text = " ".join(
        str(candidate.get(key) or "")
        for key in ("subject", "question", "answer", "snippet", "title", "content")
    )
    overlap = sorted(_tokens(query).intersection(_tokens(candidate_text)))

    if resolved_evidence is None and resolve is not None:
        resolved = [dict(resolve(ref)) for ref in refs]
    else:
        resolved = [dict(item) for item in (resolved_evidence or [])]

    statuses = tuple(sorted(str(item.get("status") or "unknown") for item in resolved))
    required_literals = _required_literals(query)
    if required_literals:
        evidence_text = "\n".join(
            [candidate_text]
            + [str(item.get("content") or "") for item in resolved]
        )
        absent = tuple(value for value in required_literals if value not in evidence_text)
        if absent:
            return EvidenceSupportDecision(
                "unsupported", ("required_literal_absent",), refs,
                {
                    "evidence_statuses": statuses,
                    "query_overlap": overlap,
                    "required_literals": required_literals,
                    "absent_literals": absent,
                },
            )
    if any(status in {"ineligible", "blocked", "secret"} for status in statuses):
        return EvidenceSupportDecision(
            "unsupported", ("evidence_ineligible",), refs,
            {"evidence_statuses": statuses, "query_overlap": overlap, "required_literals": required_literals},
        )
    if refs and resolved and all(status in {"missing", "unknown_type", "unknown"} for status in statuses):
        return EvidenceSupportDecision(
            "unsupported", ("evidence_missing",), refs,
            {"evidence_statuses": statuses, "query_overlap": overlap, "required_literals": required_literals},
        )
    if refs and any(status == "ok" for status in statuses) and overlap:
        return EvidenceSupportDecision(
            "supported", ("eligible_evidence", "query_candidate_grounded"), refs,
            {"evidence_statuses": statuses, "query_overlap": overlap, "required_literals": required_literals},
        )
    if refs and any(status == "ok" for status in statuses) and not overlap:
        return EvidenceSupportDecision(
            "unsupported", ("query_candidate_ungrounded",), refs,
            {"evidence_statuses": statuses, "query_overlap": overlap, "required_literals": required_literals},
        )

    reasons: list[str] = []
    if not refs:
        reasons.append("evidence_reference_absent")
    elif not resolved:
        reasons.append("evidence_unresolved")
    if not overlap:
        reasons.append("query_candidate_ungrounded")
    if not reasons:
        reasons.append("evidence_support_indeterminate")
    return EvidenceSupportDecision(
        "uncertain", tuple(sorted(reasons)), refs,
        {"evidence_statuses": statuses, "query_overlap": overlap, "required_literals": required_literals},
    )


def annotate_candidate_support(
    query: str,
    candidate: dict[str, Any],
    *,
    resolve: Callable[[str], Mapping[str, Any]] | None = None,
) -> EvidenceSupportDecision:
    decision = decide_evidence_support(query, candidate, resolve=resolve)
    candidate["support_state"] = decision.state
    candidate["support_reason_codes"] = list(decision.reason_codes)
    candidate["support_evidence_refs"] = list(decision.evidence_refs)
    return decision
