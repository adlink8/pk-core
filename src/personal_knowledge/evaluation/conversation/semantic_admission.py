"""Phase 62-06: deterministic-first, abstention-capable semantic admission.

Phase 62 CONTEXT D-26/D-27/D-31: extraction uses two gates. Deterministic
structure/privacy/secret/injection/evidence checks run FIRST and can never be
overridden by a model; the semantic gate then admits, rejects or abstains on
the durable/useful-information question. This module owns ONLY the admission
seam:

  - :class:`SemanticVerdict` — ``admit|reject|abstain``
  - :class:`ReasonCode` — deterministic rejection / abstention codes that are
    preserved on every decision (D-27)
  - :class:`Assessment` / :class:`AssessedDimension` — novelty / durability /
    specificity / future usefulness / contamination / contradiction (D-27)
  - :class:`JudgeInput` — the ONLY thing a judge sees: bounded redacted view
    content plus authorized event handles. A judge can never add evidence
    outside the allowlist (D-27 / Pitfall 5).
  - :class:`JudgeResult` — structured judge output
  - :class:`SemanticGate` / :func:`evaluate_view` — deterministic-first flow
  - :class:`ReplayJudge` — a deterministic replay provider (zero paid calls)

Hard rules:
  - Deterministic rejection short-circuits before the judge is invoked.
  - The judge may abstain; abstention is first-class, never coerced to admit.
  - Decisions carry reason codes, claims and handles only; sensitive prompt
    bodies are never part of a decision record (D-27).
  - This module performs no I/O, no network, and can never call a paid
    provider (D-31). Only an injected judge is ever invoked.

No I/O, no network, no provider calls (D-31).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from personal_knowledge.core.conversation_events import (
    FidelityDimension,
    FidelityLevel,
    FieldDisposition,
    TypedEvent,
)
from personal_knowledge.application.conversation.extraction_views import (
    DerivedView,
)

# Bounded redaction: a summary snippet is truncated before it can reach a
# judge, and every payload line is capped.
_SUMMARY_BOUND = 240
_PAYLOAD_LINE_BOUND = 512

# Deterministic secret markers (fail closed: any hit rejects before a model
# sees evidence).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bsecret\b", re.IGNORECASE),
    re.compile(r"\bpassw(or)?d\b", re.IGNORECASE),
    re.compile(r"\btoken\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
)

# Deterministic injection/system-prompt markers.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<system-reminder", re.IGNORECASE),
    re.compile(r"<recommended_plugins", re.IGNORECASE),
    re.compile(r"<environment_context", re.IGNORECASE),
    re.compile(r"<system\b", re.IGNORECASE),
    re.compile(r"ignore previous instructions", re.IGNORECASE),
    re.compile(r"disregard (?:all )?(?:previous|prior) instructions",
               re.IGNORECASE),
)


class SemanticVerdict(str, Enum):
    """The three possible outcomes of the semantic admission gate (D-26)."""

    ADMIT = "admit"
    REJECT = "reject"
    ABSTAIN = "abstain"


class AssessedDimension(str, Enum):
    """Semantic dimensions the gate evaluates (D-27)."""

    NOVELTY = "novelty"
    DURABILITY = "durability"
    SPECIFICITY = "specificity"
    FUTURE_USEFULNESS = "future_usefulness"
    CONTAMINATION = "contamination"
    CONTRADICTION = "contradiction"


class ReasonCode(str, Enum):
    """Preserved reason codes. Deterministic codes can never be overridden."""

    # deterministic rejections (first gate)
    STRUCTURE_INCOMPLETE = "reject:structure_incomplete"
    PRIVACY_RESTRICTED = "reject:privacy_restricted"
    SECRET_DETECTED = "reject:secret_detected"
    INJECTION_DETECTED = "reject:injection_detected"
    NO_EVIDENCE = "reject:no_evidence"
    UNSUPPORTED_SOURCE = "reject:unsupported_source"
    INVALID_LINEAGE = "reject:invalid_lineage"
    # post-judge safety (the judge cannot escape the allowlist)
    EVIDENCE_OUTSIDE_ALLOWLIST = "reject:evidence_outside_allowlist"
    MALFORMED_JUDGE_RESULT = "reject:malformed_judge_result"
    # abstentions
    ABSTAIN_NO_VERDICT = "abstain:no_verdict"
    ABSTAIN_UNCERTAIN_EVIDENCE = "abstain:uncertain_evidence"
    # admit marker
    ADMIT_SUPPORTED = "admit:supported"


class SemanticAdmissionError(ValueError):
    """The admission gate received malformed evidence/input and fails closed."""


@dataclass(frozen=True)
class Assessment:
    """One semantic assessment for an admitted candidate (D-27)."""

    dimension: AssessedDimension
    level: str
    score: float | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeInput:
    """Bounded, redacted view content plus authorized event handles.

    This is the ONLY payload a judge ever receives. It never contains a raw
    conversation body and it never names evidence outside the authorized set.
    """

    view_id: str
    view_type: str
    generation_id: str
    session_id: str | None
    redacted_content: tuple[str, ...]
    authorized_event_handles: tuple[str, ...]


@dataclass(frozen=True)
class JudgeResult:
    """Structured judge output (provider-neutral; replayable)."""

    verdict: str
    supported_claims: tuple[str, ...] = ()
    evidence_event_ids: tuple[str, ...] = ()
    assessments: tuple[Assessment, ...] = ()
    limitations: tuple[str, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True)
class DecisionRecord:
    """The public admission result (D-26/D-27).

    Only reason codes, supported claims, evidence handles, assessments and
    limitations are present — never a sensitive prompt body.
    """

    view_ref: str
    verdict: SemanticVerdict
    reason_code: str
    supported_claims: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    assessments: tuple[Assessment, ...]
    limitations: tuple[str, ...]
    replay_key: str
    judge_invoked: bool


def _truncate(text: str, bound: int) -> str:
    if len(text) <= bound:
        return text
    return text[:bound] + "...[truncated]"


def _clip(line: str) -> str:
    return line if len(line) <= _PAYLOAD_LINE_BOUND else line[:_PAYLOAD_LINE_BOUND]


def redact_view_content(
    view: DerivedView, event_index: Mapping[str, TypedEvent]
) -> tuple[str, ...]:
    """Deterministic bounded redaction of a view for a judge payload.

    Only view metadata and truncated ``summary`` snippets (an event's bounded
    navigation text) are exposed. Raw bodies live behind ``native_payload_ref``
    and are never part of the payload.
    """
    lines: list[str] = [
        f"view:{view.view_id}",
        f"view_type={view.view_type.value}",
        f"generation={view.generation_id}",
        f"session={view.session_id or ''}",
        f"evidence={','.join(str(e) for e in view.evidence_event_refs)}",
    ]
    for eid in sorted(str(e) for e in view.evidence_event_refs):
        event = event_index.get(eid)
        if event is None:
            lines.append(f"summary[{eid}]=<unresolved>")
            continue
        snippet = _truncate(event.summary or "", _SUMMARY_BOUND)
        lines.append(f"summary[{eid}]={snippet if snippet else '<none>'}")
        lines.append(f"kind[{eid}]={event.kind.value}")
    return tuple(_clip(line) for line in lines)


def make_replay_key(
    view: DerivedView,
    *,
    allowed_generation_ids: frozenset[str],
    allowed_event_ids: frozenset[str],
    judge_tag: str = "replay-v1",
) -> str:
    """Deterministic identity of one gate evaluation (same input, same key)."""
    payload = "|".join(
        [
            view.view_id,
            view.generation_id,
            view.builder_version,
            ",".join(sorted(allowed_generation_ids)),
            ",".join(sorted(allowed_event_ids)),
            judge_tag,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayJudge:
    """A deterministic, zero-cost judge for replay tests and dry runs.

    The production provider is never wired here; this class returns canned
    structured results so the gate is fully testable without any paid call
    (D-31). Cases are keyed by the deterministic ``view_id`` replay identity,
    so the same view always maps to the same canned outcome.
    """

    def __init__(
        self,
        cases: Mapping[str, JudgeResult],
        *,
        default: JudgeResult | None = None,
    ) -> None:
        self._cases = dict(cases)
        self._default = default
        self.calls = 0

    def __call__(self, payload: JudgeInput) -> JudgeResult:
        self.calls += 1
        if payload.view_id in self._cases:
            return self._cases[payload.view_id]
        if self._default is not None:
            return self._default
        return JudgeResult(
            verdict=SemanticVerdict.ABSTAIN.value,
            supported_claims=(),
            evidence_event_ids=(),
            assessments=(),
            limitations=(),
            reason_code=ReasonCode.ABSTAIN_NO_VERDICT.value,
        )


class SemanticGate:
    """Deterministic-first admission gate over one derived view (D-26)."""

    def __init__(self, judge, *, judge_tag: str = "replay-v1") -> None:
        self.judge = judge
        self.judge_tag = judge_tag

    def evaluate(
        self,
        view: DerivedView,
        event_index: Mapping[str, TypedEvent],
        *,
        allowed_event_ids: set[str],
        allowed_generation_ids: set[str],
    ) -> DecisionRecord:
        """Run the full admission flow for one view.

        Deterministic rejection short-circuits before the judge is invoked.
        """
        _validate_evidence(view)
        deterministic = _deterministic_rejection(
            view, event_index, allowed_event_ids, allowed_generation_ids
        )
        replay_key = make_replay_key(
            view,
            allowed_generation_ids=frozenset(allowed_generation_ids),
            allowed_event_ids=frozenset(allowed_event_ids),
            judge_tag=self.judge_tag,
        )
        if deterministic is not None:
            return _rejected(
                view, deterministic, replay_key=replay_key, judge_invoked=False
            )

        payload = JudgeInput(
            view_id=view.view_id,
            view_type=view.view_type.value,
            generation_id=view.generation_id,
            session_id=view.session_id,
            redacted_content=redact_view_content(view, event_index),
            authorized_event_handles=tuple(sorted(view.evidence_event_refs)),
        )
        result = self.judge(payload)
        return _post_process(view, result, allowed_event_ids, replay_key)


def evaluate_view(
    view: DerivedView,
    event_index: Mapping[str, TypedEvent],
    judge,
    *,
    allowed_event_ids: set[str],
    allowed_generation_ids: set[str],
    judge_tag: str = "replay-v1",
) -> DecisionRecord:
    """Convenience wrapper around :class:`SemanticGate`."""
    return SemanticGate(judge, judge_tag=judge_tag).evaluate(
        view,
        event_index,
        allowed_event_ids=allowed_event_ids,
        allowed_generation_ids=allowed_generation_ids,
    )


# ------------------------------------------------------------ deterministic

def _validate_evidence(view: DerivedView) -> None:
    for ref in view.evidence_event_refs:
        if not isinstance(ref, str) or not ref.strip():
            raise SemanticAdmissionError(
                f"evidence event refs must be non-empty strings, got {ref!r}"
            )


def _deterministic_rejection(
    view: DerivedView,
    event_index: Mapping[str, TypedEvent],
    allowed_event_ids: set[str],
    allowed_generation_ids: set[str],
) -> str | None:
    """First gate: structure/privacy/secret/injection/evidence checks.

    Returns a reason code on rejection or ``None`` to continue to the judge.
    """
    if view.generation_id not in allowed_generation_ids:
        return ReasonCode.UNSUPPORTED_SOURCE.value
    if not view.evidence_event_refs:
        return ReasonCode.NO_EVIDENCE.value
    for ref in view.evidence_event_refs:
        if ref not in allowed_event_ids or ref not in event_index:
            return ReasonCode.INVALID_LINEAGE.value
    if _structure_incomplete(view, event_index):
        return ReasonCode.STRUCTURE_INCOMPLETE.value
    if _privacy_restricted(view, event_index):
        return ReasonCode.PRIVACY_RESTRICTED.value
    content = _bounded_content(view, event_index)
    if _secret_detected(content):
        return ReasonCode.SECRET_DETECTED.value
    if _injection_detected(content):
        return ReasonCode.INJECTION_DETECTED.value
    return None


def _structure_incomplete(
    view: DerivedView, event_index: Mapping[str, TypedEvent]
) -> bool:
    if view.members and not set(view.members) <= set(view.evidence_event_refs):
        return True
    if not view.members and view.evidence_event_refs:
        return True
    for ref in view.evidence_event_refs:
        level = event_index[ref].fidelity.level(
            FidelityDimension.STRUCTURE_COMPLETENESS
        )
        if level in (FidelityLevel.UNAVAILABLE, FidelityLevel.UNKNOWN):
            return True
    return False


def _privacy_restricted(
    view: DerivedView, event_index: Mapping[str, TypedEvent]
) -> bool:
    for ref in view.evidence_event_refs:
        event = event_index[ref]
        for record in event.field_dispositions:
            if (
                record.field_name == "content"
                and record.disposition is FieldDisposition.REDACTED
            ):
                return True
        if (
            event.fidelity.level(FidelityDimension.CONTENT_AVAILABILITY)
            is FidelityLevel.UNAVAILABLE
        ):
            return True
    return False


def _bounded_content(
    view: DerivedView, event_index: Mapping[str, TypedEvent]
) -> str:
    parts: list[str] = []
    for ref in view.evidence_event_refs:
        event = event_index[ref]
        if event.summary:
            parts.append(_truncate(event.summary, _SUMMARY_BOUND))
    return "\n".join(parts)


def _secret_detected(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _injection_detected(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


# --------------------------------------------------------------- post-judge

def _post_process(
    view: DerivedView,
    result: object,
    allowed_event_ids: set[str],
    replay_key: str,
) -> DecisionRecord:
    if not isinstance(result, JudgeResult):
        return _rejected(
            view, ReasonCode.MALFORMED_JUDGE_RESULT.value,
            replay_key=replay_key, judge_invoked=True,
        )
    for eid in result.evidence_event_ids:
        if not isinstance(eid, str):
            return _rejected(
                view, ReasonCode.MALFORMED_JUDGE_RESULT.value,
                replay_key=replay_key, judge_invoked=True,
            )
        if eid not in allowed_event_ids:
            return _rejected(
                view, ReasonCode.EVIDENCE_OUTSIDE_ALLOWLIST.value,
                replay_key=replay_key, judge_invoked=True,
            )
    verdict = result.verdict
    if verdict == SemanticVerdict.ABSTAIN.value:
        code = result.reason_code or ReasonCode.ABSTAIN_NO_VERDICT.value
        return DecisionRecord(
            view_ref=view.view_id,
            verdict=SemanticVerdict.ABSTAIN,
            reason_code=code,
            supported_claims=(),
            evidence_event_ids=(),
            assessments=result.assessments,
            limitations=result.limitations,
            replay_key=replay_key,
            judge_invoked=True,
        )
    if verdict == SemanticVerdict.REJECT.value:
        code = result.reason_code or "reject:semantic"
        return DecisionRecord(
            view_ref=view.view_id,
            verdict=SemanticVerdict.REJECT,
            reason_code=code,
            supported_claims=(),
            evidence_event_ids=(),
            assessments=result.assessments,
            limitations=result.limitations,
            replay_key=replay_key,
            judge_invoked=True,
        )
    if verdict == SemanticVerdict.ADMIT.value:
        evidence = (
            result.evidence_event_ids
            if result.evidence_event_ids
            else tuple(view.evidence_event_refs)
        )
        return DecisionRecord(
            view_ref=view.view_id,
            verdict=SemanticVerdict.ADMIT,
            reason_code=result.reason_code or ReasonCode.ADMIT_SUPPORTED.value,
            supported_claims=result.supported_claims,
            evidence_event_ids=evidence,
            assessments=result.assessments,
            limitations=result.limitations,
            replay_key=replay_key,
            judge_invoked=True,
        )
    return _rejected(
        view, ReasonCode.MALFORMED_JUDGE_RESULT.value,
        replay_key=replay_key, judge_invoked=True,
    )


def _rejected(
    view: DerivedView,
    reason_code: str,
    *,
    replay_key: str,
    judge_invoked: bool,
) -> DecisionRecord:
    return DecisionRecord(
        view_ref=view.view_id,
        verdict=SemanticVerdict.REJECT,
        reason_code=reason_code,
        supported_claims=(),
        evidence_event_ids=(),
        assessments=(),
        limitations=(),
        replay_key=replay_key,
        judge_invoked=judge_invoked,
    )


__all__ = [
    "AssessedDimension",
    "Assessment",
    "DecisionRecord",
    "JudgeInput",
    "JudgeResult",
    "ReasonCode",
    "ReplayJudge",
    "SemanticAdmissionError",
    "SemanticGate",
    "SemanticVerdict",
    "evaluate_view",
    "make_replay_key",
    "redact_view_content",
]
