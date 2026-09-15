"""Replayable end-to-end answer evaluation (rules first; judge optional)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from personal_knowledge.evaluation.knowledge_eval_metrics import RankedHit


@dataclass
class AnswerResult:
    query_id: str
    mode: str
    answer: str
    cited_ids: list[str]
    model: str
    prompt_version: str
    cache_key: str
    abstained: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnswerScore:
    query_id: str
    mode: str
    citation_resolvable: float
    citation_precision: float
    citation_coverage: float
    abstain_correct: bool | None
    privacy_hit: bool
    rule_correctness: float | None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROMPT_VERSION = "answer_eval_v1"
DEFAULT_MODEL = "deterministic_extractive_v1"

_CITE_RE = re.compile(r"\[\[([^\]]+)\]\]")


def build_prompt(
    query: str,
    contexts: Sequence[RankedHit],
    *,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    lines = [
        f"prompt_version={prompt_version}",
        "Answer using only the numbered contexts. Cite as [[id]]. Abstain if insufficient.",
        f"Question: {query}",
        "Contexts:",
    ]
    for i, c in enumerate(contexts, 1):
        lines.append(f"{i}. id={c.id} subject={c.subject} text={c.snippet[:400]}")
    return "\n".join(lines)


def cache_key(prompt: str, model: str, prompt_version: str) -> str:
    raw = f"{prompt_version}|{model}|{prompt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def generate_answer(
    query: str,
    contexts: Sequence[RankedHit],
    *,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    cache: Mapping[str, str] | None = None,
    expected_abstain: bool = False,
) -> AnswerResult:
    """Deterministic extractive generator (no network). Cache-replayable."""
    prompt = build_prompt(query, contexts, prompt_version=prompt_version)
    key = cache_key(prompt, model, prompt_version)
    if cache and key in cache:
        text = cache[key]
        cites = _CITE_RE.findall(text)
        return AnswerResult(
            query_id="",
            mode="",
            answer=text,
            cited_ids=cites,
            model=model,
            prompt_version=prompt_version,
            cache_key=key,
            abstained="ABSTAIN" in text.upper(),
            meta={"replay": True},
        )

    if expected_abstain or not contexts:
        text = "ABSTAIN: insufficient grounded evidence."
        return AnswerResult(
            query_id="",
            mode="",
            answer=text,
            cited_ids=[],
            model=model,
            prompt_version=prompt_version,
            cache_key=key,
            abstained=True,
        )

    # Extractive: top context sentence + citation
    top = contexts[0]
    snippet = (top.snippet or top.subject or "").strip()
    if not snippet:
        text = "ABSTAIN: empty context."
        return AnswerResult(
            query_id="",
            mode="",
            answer=text,
            cited_ids=[],
            model=model,
            prompt_version=prompt_version,
            cache_key=key,
            abstained=True,
        )
    text = f"{snippet[:280]} [[{top.id}]]"
    return AnswerResult(
        query_id="",
        mode="",
        answer=text,
        cited_ids=[top.id],
        model=model,
        prompt_version=prompt_version,
        cache_key=key,
        abstained=False,
    )


def score_answer(
    result: AnswerResult,
    *,
    ranked_ids: Sequence[str],
    gold_refs: Sequence[str] | None = None,
    expected_abstain: bool = False,
    forbid_substrings: Sequence[str] | None = None,
) -> AnswerScore:
    allowed = set(ranked_ids)
    cites = result.cited_ids
    if not cites:
        resolvable = 1.0 if result.abstained else 0.0
        precision = 1.0 if result.abstained else 0.0
    else:
        ok = sum(1 for c in cites if c in allowed)
        resolvable = ok / len(cites)
        precision = ok / len(cites)

    gold = set(gold_refs or [])
    if expected_abstain:
        coverage = 1.0 if result.abstained else 0.0
        abstain_correct = result.abstained
        rule_corr = 1.0 if result.abstained else 0.0
    else:
        if not gold:
            coverage = None  # type: ignore[assignment]
            rule_corr = None
        else:
            # coverage: any gold id cited or present in answer path via ranked
            hit = any(c in gold for c in cites) or any(
                rid in gold for rid in ranked_ids[:3]
            )
            coverage = 1.0 if hit else 0.0
            rule_corr = coverage if resolvable >= 1.0 else 0.0
        abstain_correct = (not result.abstained) if not expected_abstain else result.abstained

    privacy = False
    blob = result.answer or ""
    for s in forbid_substrings or []:
        if s and s in blob:
            privacy = True

    return AnswerScore(
        query_id=result.query_id,
        mode=result.mode,
        citation_resolvable=float(resolvable),
        citation_precision=float(precision),
        citation_coverage=float(coverage) if coverage is not None else 0.0,
        abstain_correct=abstain_correct,
        privacy_hit=privacy,
        rule_correctness=rule_corr,
    )


def aggregate_answer_scores(scores: Sequence[AnswerScore]) -> dict[str, Any]:
    if not scores:
        return {"n": 0, "insufficient_evidence": True}
    n = len(scores)
    return {
        "n": n,
        "citation_resolvable": sum(s.citation_resolvable for s in scores) / n,
        "citation_precision": sum(s.citation_precision for s in scores) / n,
        "citation_coverage": sum(s.citation_coverage for s in scores) / n,
        "abstain_accuracy": sum(1 for s in scores if s.abstain_correct) / n,
        "privacy_hit": sum(1 for s in scores if s.privacy_hit),
        "rule_correctness": sum(
            (s.rule_correctness or 0.0) for s in scores if s.rule_correctness is not None
        )
        / max(1, sum(1 for s in scores if s.rule_correctness is not None)),
        "insufficient_evidence": n < 5,
    }


def load_cache(path) -> dict[str, str]:
    p = path
    if not p or not hasattr(p, "exists") or not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}
