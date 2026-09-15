"""Phase 17-02: adapters, metrics, registry."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.evaluation.eval_contracts import EvalCase, EvalTarget  # noqa: E402
from personal_knowledge.evaluation.eval_registry import EvalRegistry  # noqa: E402
from personal_knowledge.evaluation.knowledge_eval_metrics import (  # noqa: E402
    BOOTSTRAP_B,
    BOOTSTRAP_SEED,
    RankedHit,
    aggregate_scores,
    compare_modes,
    paired_bootstrap_ci,
    primary_rank,
    score_case,
)
from personal_knowledge.evaluation.retrieval_adapters import (  # noqa: E402
    load_l2_unit_ids,
    resolve_targets,
    validate_eval_binding,
)


def _binding(collection: str = "active_ku") -> dict:
    roles = {
        role: {"location_ref": collection if role == "knowledge_retrieval" else role, "version": "v1", "checksum": "c1"}
        for role in (
            "canonical_knowledge", "canonical_message", "knowledge_retrieval",
            "turn_retrieval", "google_normalized",
        )
    }
    return {"ok": True, "snapshot_id": "ss_test", "manifest_hash": "h1", "members": roles}


def test_eval_binding_accepts_one_snapshot_and_rejects_mixed_collection() -> None:
    good = validate_eval_binding(_binding(), {"l1_l2_collection": "active_ku"})
    mixed = validate_eval_binding(_binding(), {"l1_l2_collection": "other_ku"})
    assert good["ok"] is True
    assert mixed["ok"] is False
    assert "l1_l2_collection_not_in_snapshot" in mixed["errors"]


def test_eval_binding_rejects_l2_from_other_source() -> None:
    result = validate_eval_binding(
        _binding(),
        {"l1_l2_collection": "active_ku"},
        l2_audit={"source_collection": "old_ku", "source_binding_ok": True},
    )
    assert result["ok"] is False
    assert "l2_source_not_in_snapshot" in result["errors"]


def test_adapter_targets_no_hardcode_required() -> None:
    targets = resolve_targets(
        l1_collection="coll_l1",
        l1_l2_collection="coll_l12",
        raw_collection="personal_events",
        top_k=5,
        l2_filter_ids=None,
    )
    modes = {t.mode for t in targets}
    assert modes == {"raw", "l1", "l2_only", "l1_l2", "hybrid"}
    l2 = next(t for t in targets if t.mode == "l2_only")
    assert l2.blocked is True
    assert "purif" in l2.blocked_reason.lower() or "lineage" in l2.blocked_reason.lower()

    targets2 = resolve_targets(
        l1_collection="a",
        l1_l2_collection="b",
        l2_filter_ids={"l2|1", "l2|2"},
        l2_only_collection="b",
    )
    l2b = next(t for t in targets2 if t.mode == "l2_only")
    assert l2b.blocked is False


def test_metric_primary_id_not_snippet() -> None:
    ranked = [
        RankedHit(id="u1", snippet="这段文字包含金标片段足够长啦啦啦", source_ref=""),
        RankedHit(id="gold-unit", snippet="other", source_ref="cm|x"),
    ]
    # primary: only stable id
    assert primary_rank(ranked, set(), {"gold-unit"}) == 2
    # snippet diagnostic separate
    sc = score_case(
        "q1",
        "l1",
        ranked,
        gold_unit_ids=["missing"],
        gold_snippets=["这段文字包含金标片段足够长啦啦啦"],
    )
    assert sc.hit_primary is False
    assert sc.hit_diagnostic_snippet is True


def test_metric_aggregate_and_na() -> None:
    scores = [
        score_case("a", "m", [RankedHit(id="g")], gold_unit_ids=["g"]),
        score_case("b", "m", [RankedHit(id="x")], gold_unit_ids=["g"]),
        score_case("c", "m", [], expected_abstain=True),
    ]
    # pad to avoid insufficient for structure
    for i in range(5):
        scores.append(
            score_case(f"p{i}", "m", [RankedHit(id="g")], gold_unit_ids=["g"])
        )
    agg = aggregate_scores(scores)
    assert agg["recall_at"]["5"] is not None
    assert agg["n_abstain"] == 1
    empty = aggregate_scores([])
    assert empty["recall_at"]["5"] is None  # N/A not 0
    assert empty["insufficient_evidence"] is True


def test_synthetic_shell_can_exercise_safety_without_polluting_recall() -> None:
    real = score_case("real", "m", [RankedHit(id="g")], gold_unit_ids=["g"])
    shell = score_case(
        "shell",
        "m",
        [RankedHit(id="x")],
        gold_unit_ids=["syn-unit"],
        score_retrieval=False,
    )
    agg = aggregate_scores([real, shell])
    assert agg["n"] == 2
    assert agg["n_scored_retrieval"] == 1
    assert agg["recall_at"]["5"] == 1.0


def test_secret_gate_uses_provenance_not_technical_words() -> None:
    lexical_only = score_case(
        "secret-query",
        "l1",
        [RankedHit(id="safe", subject="API client", snippet="secret naming")],
        secret_ineligible=True,
    )
    actual_ineligible = score_case(
        "secret-query",
        "l1",
        [
            RankedHit(
                id="blocked",
                meta={"source_evidence_ineligible": True},
            )
        ],
        secret_ineligible=True,
    )
    assert lexical_only.secret_hit is False
    assert actual_ineligible.secret_hit is True


def test_case_score_records_threshold_diagnostics() -> None:
    score = score_case(
        "q",
        "l1",
        [RankedHit(id="a", score=0.8), RankedHit(id="b", score=0.7)],
    )
    assert score.top_score == pytest.approx(0.8)
    assert score.score_margin == pytest.approx(0.1)


def test_metric_paired_bootstrap_seed() -> None:
    base = [
        score_case(f"q{i}", "raw", [RankedHit(id="x")], gold_unit_ids=["g"])
        for i in range(10)
    ]
    treat = [
        score_case(f"q{i}", "l1", [RankedHit(id="g")], gold_unit_ids=["g"])
        for i in range(10)
    ]
    # make half baseline hits
    for i in range(5):
        base[i] = score_case(f"q{i}", "raw", [RankedHit(id="g")], gold_unit_ids=["g"])
    r1 = paired_bootstrap_ci(base, treat, seed=BOOTSTRAP_SEED, B=200)
    r2 = paired_bootstrap_ci(base, treat, seed=BOOTSTRAP_SEED, B=200)
    assert r1["delta"] == r2["delta"]
    assert r1["ci_low"] == r2["ci_low"]
    assert r1["insufficient_evidence"] is False
    # full B constant documented
    assert BOOTSTRAP_B == 10_000
    assert BOOTSTRAP_SEED == 1701


def test_registry_immutable(tmp_path: Path) -> None:
    db = tmp_path / "eval_reg.sqlite"
    reg = EvalRegistry(db)
    reg.create_run(
        "run1",
        dataset_checksum="a" * 64,
        config_checksum="b" * 64,
        scorer_version="v1",
        top_k=5,
        modes=["raw", "l1"],
    )
    reg.add_metrics("run1", "raw", {"recall_at": {"5": 0.5}})
    with pytest.raises(FileExistsError):
        reg.create_run(
            "run1",
            dataset_checksum="a" * 64,
            config_checksum="b" * 64,
            scorer_version="v1",
            top_k=5,
            modes=["raw"],
        )
    with pytest.raises(FileExistsError):
        reg.add_metrics("run1", "raw", {"recall_at": {"5": 0.9}})
    got = reg.get_run("run1")
    assert got is not None
    assert got["metrics"]["raw"]["recall_at"]["5"] == 0.5


def test_compare_modes_structure() -> None:
    def mk(mode: str, hit: bool):
        return [
            score_case(
                f"q{i}",
                mode,
                [RankedHit(id="g" if hit else "x")],
                gold_unit_ids=["g"],
            )
            for i in range(8)
        ]

    out = compare_modes({"raw": mk("raw", False), "l1": mk("l1", True)}, baseline="raw")
    assert "l1" in out["comparisons"]
    assert out["comparisons"]["l1"]["delta"] is not None


def test_l2_filter_resolves_canonical_ids(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "l2.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE knowledge_units(unit_id TEXT, run_id TEXT);"
        "CREATE TABLE canonical_unit_members(canonical_unit_id TEXT, member_unit_id TEXT);"
        "INSERT INTO knowledge_units VALUES ('l2|member','run-l2');"
        "INSERT INTO canonical_unit_members VALUES ('cu|canonical','l2|member');"
    )
    con.commit()
    con.close()
    assert load_l2_unit_ids(db, ["run-l2"]) == {"cu|canonical"}
