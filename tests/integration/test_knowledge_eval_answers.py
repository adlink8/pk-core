"""Phase 17-03: deterministic answer evaluation."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.evaluation.answer_eval import (  # noqa: E402
    generate_answer,
    score_answer,
    cache_key,
    build_prompt,
)
from personal_knowledge.evaluation.knowledge_eval_metrics import RankedHit  # noqa: E402
from personal_knowledge.evaluation.knowledge_eval_metrics import score_case  # noqa: E402
from personal_knowledge.evaluation.run_knowledge_eval import stage_answer  # noqa: E402
from personal_knowledge.evaluation.eval_contracts import EvalCase  # noqa: E402
from personal_knowledge.evaluation.review_packets import (  # noqa: E402
    ReviewError,
    build_packet,
    calibrate_judge,
)
import json
import pytest


def test_deterministic_answer_replay() -> None:
    ctx = [RankedHit(id="u1", snippet="用户偏好 PowerShell 作为日常 shell。", subject="shell")]
    a1 = generate_answer("用户用什么 shell？", ctx)
    a2 = generate_answer("用户用什么 shell？", ctx)
    assert a1.cache_key == a2.cache_key
    assert a1.answer == a2.answer
    assert a1.cited_ids == ["u1"]

    # cache replay
    cache = {a1.cache_key: "cached reply [[u1]]"}
    a3 = generate_answer("用户用什么 shell？", ctx, cache=cache)
    assert a3.answer.startswith("cached")
    assert a3.meta.get("replay") is True


def test_deterministic_citation_and_abstain() -> None:
    ctx = [RankedHit(id="u1", snippet="fact", subject="s")]
    ar = generate_answer("q", ctx)
    sc = score_answer(ar, ranked_ids=["u1"], gold_refs=["u1"])
    assert sc.citation_resolvable == 1.0
    assert sc.citation_precision == 1.0

    # out-of-range citation
    ar.cited_ids = ["not-in-ranked"]
    ar.answer = "x [[not-in-ranked]]"
    sc2 = score_answer(ar, ranked_ids=["u1"])
    assert sc2.citation_resolvable == 0.0

    ar_abs = generate_answer("secret?", [], expected_abstain=True)
    assert ar_abs.abstained is True
    sc3 = score_answer(ar_abs, ranked_ids=[], expected_abstain=True)
    assert sc3.abstain_correct is True


def test_deterministic_prompt_stable() -> None:
    ctx = [RankedHit(id="a", snippet="s", subject="t")]
    p1 = build_prompt("q", ctx)
    p2 = build_prompt("q", ctx)
    assert p1 == p2
    assert cache_key(p1, "m", "v") == cache_key(p2, "m", "v")


def test_answer_stage_uses_ephemeral_ranked_content_without_persisting_it(tmp_path: Path) -> None:
    case = EvalCase(
        id="q1",
        split="synthetic",
        query="shell?",
        gold_unit_ids=["u1"],
    )
    hit = RankedHit(id="u1", snippet="用户偏好 PowerShell。", subject="shell")
    retrieval = {
        "mode_scores": {"l1_l2": [score_case("q1", "l1_l2", [hit], gold_unit_ids=["u1"])]},
        "mode_ranked": {"l1_l2": [[hit]]},
        "modes": {"l1_l2": {"aggregate": {}}},
    }
    result = stage_answer([case], retrieval, tmp_path, enabled=True)
    agg = result["modes"]["l1_l2"]["aggregate"]
    assert agg["citation_precision"] == 1.0
    assert agg["rule_correctness"] == 1.0
    persisted = (tmp_path / "answer.json").read_text(encoding="utf-8")
    assert "用户偏好 PowerShell" not in persisted


def test_judge_calibration_requires_complete_30x5_and_human_provenance(tmp_path: Path) -> None:
    rows = [
        {"case_id": f"c{i}", "mode": mode, "answer": "private"}
        for i in range(30) for mode in ("raw", "l1", "l2_only", "l1_l2", "hybrid")
    ]
    packet = build_packet("judge_30x5", rows)
    ratings = [
        {"case_id": row["case_id"], "mode": row["mode"], "score": (i % 5) + 1, "pass": i % 2 == 0, "privacy_violation": False}
        for i, row in enumerate(rows)
    ]
    human = {"packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"], "reviewer_id": "human-rater-01", "reviewed_at": "2026-07-17T12:00:00Z", "ratings": ratings}
    judge = {"ratings": ratings}
    pp, hp, jp = tmp_path / "p.json", tmp_path / "h.json", tmp_path / "j.json"
    for path, value in ((pp, packet), (hp, human), (jp, judge)):
        path.write_text(json.dumps(value), encoding="utf-8")
    report = calibrate_judge(pp, hp, jp, report_path=tmp_path / "report.json")
    assert report["judge_gate_enabled"] is True
    assert report["spearman_rho"] == 1.0
    assert report["network_used"] is False

    with pytest.raises(ReviewError):
        calibrate_judge(pp, hp, jp, allow_network_judge=True)


def test_llm_judge_calibration_requires_two_distinct_auditable_runs(tmp_path: Path) -> None:
    rows = [
        {"case_id": f"c{i}", "mode": mode, "answer": "private"}
        for i in range(30) for mode in ("raw", "l1", "l2_only", "l1_l2", "hybrid")
    ]
    packet = build_packet("judge_30x5", rows)
    ratings = [
        {"case_id": row["case_id"], "mode": row["mode"], "score": (i % 5) + 1,
         "pass": i % 2 == 0, "privacy_violation": False, "confidence": 0.9}
        for i, row in enumerate(rows)
    ]
    common = {
        "packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"],
        "reviewer_type": "llm", "reviewer_id": "openai-gpt-5.6-luna",
        "model_id": "gpt-5.6-luna", "prompt_version": "phase24-llm-review-v1",
        "reviewed_at": "2026-07-18T12:00:00Z", "ratings": ratings,
    }
    primary = {**common, "review_run_id": "run-primary"}
    judge = {**common, "review_run_id": "run-independent"}
    pp, hp, jp = tmp_path / "p.json", tmp_path / "h.json", tmp_path / "j.json"
    for path, value in ((pp, packet), (hp, primary), (jp, judge)):
        path.write_text(json.dumps(value), encoding="utf-8")
    report = calibrate_judge(pp, hp, jp, report_path=tmp_path / "report.json")
    assert report["reviewer_type"] == "llm"
    assert report["judge_review_run_id"] == "run-independent"

    judge["review_run_id"] = "run-primary"
    jp.write_text(json.dumps(judge), encoding="utf-8")
    with pytest.raises(ReviewError, match="distinct"):
        calibrate_judge(pp, hp, jp, report_path=tmp_path / "report2.json")
