"""Phase 17-01: eval contracts and dataset audit."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.evaluation.eval_contracts import (  # noqa: E402
    ContractError,
    DatasetManifest,
    EvalCase,
    EvalRunManifest,
    EvalTarget,
    audit_dataset,
    build_dataset_manifest,
    cases_checksum,
    compute_run_id,
    content_checksum,
    load_cases_jsonl,
)
from personal_knowledge.evaluation.run_knowledge_eval import (  # noqa: E402
    _is_real_gold_case,
    load_config,
    resolve_cases_path,
    stage_dataset_audit,
    stage_extraction,
)
from personal_knowledge.evaluation.build_private_suite import HOLDOUT, OUT_DIR, SYN  # noqa: E402
from personal_knowledge.evaluation.knowledge_eval_metrics import SCORER_VERSION  # noqa: E402
from personal_knowledge.evaluation.review_packets import (  # noqa: E402
    ReviewError,
    _is_reviewable_user_text,
    _pair_score,
    build_packet,
    import_gold,
)


def test_gold_candidate_filter_rejects_ide_control_payloads() -> None:
    assert _is_reviewable_user_text("查看项目状态")
    assert not _is_reviewable_user_text("<system-reminder data-role=\"user-context\">x")
    assert not _is_reviewable_user_text("The TodoWrite tool hasn't been used recently. x")
    assert not _is_reviewable_user_text("This session was forked from a previous session message. x")
    assert not _is_reviewable_user_text("[Assistant Rules - You MUST follow these instructions]")
    assert not _is_reviewable_user_text("<local-command-stdout>Set model</local-command-stdout>")
    assert not _is_reviewable_user_text("Warning: apply_patch was requested via shell. x")
    assert not _is_reviewable_user_text("<cb_summary>prior context</cb_summary>")
    assert _pair_score("向量模型支持中文", "中文向量模型升级") > _pair_score("向量模型支持中文", "清灰多少钱")


def test_contract_roundtrip_eval_case() -> None:
    c = EvalCase(
        id="t1",
        split="synthetic",
        query="用户用什么 shell？",
        gold_evidence_refs=["cm|1"],
        expected_abstain=False,
        scenario="preference",
    )
    d = c.to_dict()
    c2 = EvalCase.from_dict(d)
    assert c2.id == "t1"
    assert c2.gold_evidence_refs == ["cm|1"]
    assert cases_checksum([c]) == cases_checksum([c2])


def test_contract_missing_required_fails() -> None:
    with pytest.raises(ContractError):
        EvalCase(id="", split="x", query="q")
    with pytest.raises(ContractError):
        EvalCase(id="a", split="x", query="")
    with pytest.raises(ContractError):
        EvalTarget(mode="not_a_mode")


def test_contract_checksum_changes_with_case() -> None:
    a = EvalCase(id="1", split="s", query="alpha")
    b = EvalCase(id="1", split="s", query="beta")
    assert cases_checksum([a]) != cases_checksum([b])
    assert content_checksum({"a": 1}) != content_checksum({"a": 2})


def test_dataset_manifest_and_run_manifest() -> None:
    path = (
        _ROOT
        / "assets"
        / "evals"
        / "knowledge_units"
        / "comprehensive_v1.synthetic.jsonl"
    )
    assert path.exists()
    m = build_dataset_manifest("comprehensive_v1", "v1", path)
    assert m.case_count >= 100
    assert m.checksum
    d = m.to_dict()
    m2 = DatasetManifest.from_dict(d)
    assert m2.checksum == m.checksum

    run = EvalRunManifest(
        run_id="a" * 64,
        dataset_checksum=m.checksum,
        config_checksum="b" * 64,
        scorer_version="v1",
        top_k=5,
        modes=["raw", "l1", "l2_only", "l1_l2", "hybrid"],
    )
    run.validate()
    rid = compute_run_id(m.checksum, "b" * 64, "v1", run.modes, 5)
    assert len(rid) == 64


def test_audit_synthetic_suite() -> None:
    path = (
        _ROOT
        / "assets"
        / "evals"
        / "knowledge_units"
        / "comprehensive_v1.synthetic.jsonl"
    )
    cases = load_cases_jsonl(path)
    audit = audit_dataset(cases)
    assert audit["ok"] is True
    assert audit["case_count"] == len(cases)
    scenarios = {c.scenario for c in cases}
    assert "cross_turn" in scenarios
    assert "privacy" in scenarios
    cross = [c for c in cases if c.requires_cross_turn]
    assert len(cross) >= 30


def test_contract_load_holdout_schema() -> None:
    path = (
        _ROOT
        / "assets"
        / "evals"
        / "knowledge_units"
        / "holdout_15_02.synthetic.jsonl"
    )
    cases = load_cases_jsonl(path)
    assert len(cases) >= 8
    assert all(c.query for c in cases)


def test_eval_config_tracks_relocated_private_suite_and_runtime_active() -> None:
    cfg = load_config(_ROOT / "assets" / "evals" / "knowledge_units" / "eval_v1.yaml")
    assert cfg["scorer_version"] == SCORER_VERSION
    assert cfg["policy_path"].endswith("eval_policy_v2.yaml")
    assert resolve_cases_path(cfg) == _ROOT / "var" / "runtime" / "private_evals" / "comprehensive_v1.private.jsonl"
    targets = cfg["targets"]
    assert targets.get("l1_l2_collection") is None
    assert targets.get("candidate_collection") is None
    assert targets.get("l2_only_collection") == "knowledge_units_eval_l2_894985b38fe5"
    assert targets.get("l2_only_purified") is True
    assert SYN.exists() and HOLDOUT.exists()
    assert OUT_DIR == _ROOT / "var" / "runtime" / "private_evals"


def test_private_suite_fails_closed_without_real_cross_turn_gold(tmp_path: Path) -> None:
    cfg = load_config(_ROOT / "assets" / "evals" / "knowledge_units" / "eval_v1.yaml")
    cases = load_cases_jsonl(resolve_cases_path(cfg))
    cases_without_reviewed_gold = [
        case
        for case in cases
        if case.gold_provenance not in {"human_reviewed_v1", "llm_reviewed_v1"}
    ]
    audit = stage_dataset_audit(
        cases_without_reviewed_gold, tmp_path, require_private_gold=True
    )
    assert audit["ok"] is False
    assert audit["real_gold_cases"] == 22
    assert audit["real_cross_turn_gold_cases"] < 30


def test_extraction_stage_binds_reviewed_grounded_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_knowledge.evaluation.extraction_quality_eval as extraction
    import personal_knowledge.evaluation.reconcile_l2_lineage as lineage
    import personal_knowledge.evaluation.review_packets as packets

    labels = [{"unit_id": "l2|one", "grounded": True}]
    labels_path = tmp_path / "grounded.private.jsonl"
    labels_path.write_text(json.dumps(labels[0]) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "grounded.manifest.json"
    manifest_path.write_text(
        json.dumps({"import_checksum": packets.checksum(labels)}), encoding="utf-8"
    )
    monkeypatch.setattr(packets, "GROUNDED_IMPORT", labels_path)
    monkeypatch.setattr(packets, "GROUNDED_MANIFEST", manifest_path)
    monkeypatch.setattr(lineage, "reconcile", lambda _db: {"ok": True})
    captured: dict = {}

    def fake_evaluate(_db, *, sample_limit, human_labels):
        captured.update(sample_limit=sample_limit, labels=human_labels)
        return {"ok": True}

    monkeypatch.setattr(extraction, "evaluate_extraction", fake_evaluate)
    result = stage_extraction(tmp_path, enabled=True)
    assert result["extraction_quality"]["ok"] is True
    assert captured == {"sample_limit": 50, "labels": labels}


def test_real_gold_case_excludes_synthetic_abstain_and_unlabelled() -> None:
    base = {"id": "q", "query": "query", "split": "test", "gold_unit_ids": ["gold"]}
    assert _is_real_gold_case(EvalCase.from_dict({**base, "gold_provenance": "human"}))
    assert not _is_real_gold_case(
        EvalCase.from_dict({**base, "gold_provenance": "synthetic_v1"})
    )
    assert not _is_real_gold_case(
        EvalCase.from_dict({**base, "expected_abstain": True})
    )
    assert not _is_real_gold_case(
        EvalCase.from_dict({"id": "q", "query": "query", "split": "test"})
    )


class _Evidence:
    def __init__(self, status: str = "ok") -> None:
        self.status = status

    def resolve(self, ref, *, artifact_type=None, **kwargs):
        return {"ref": ref, "status": self.status}


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_human_gold_import_requires_provenance_checksum_and_resolved_refs(tmp_path: Path) -> None:
    packet = build_packet(
        "gold",
        [{
            "case_id": "real-1", "id": "real-1", "query": "reviewed query",
            "split": "human_review_candidate", "gold_evidence_refs": ["cm|1"],
            "gold_unit_ids": ["cu|1"], "requires_cross_turn": True,
        }],
    )
    packet_path, labels_path = tmp_path / "packet.json", tmp_path / "labels.json"
    _write_json(packet_path, packet)
    labels = {
        "packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"],
        "reviewer_id": "human-reviewer-01", "reviewed_at": "2026-07-17T12:00:00Z",
        "labels": [{"case_id": "real-1", "decision": "accept"}],
    }
    _write_json(labels_path, labels)
    result = import_gold(
        packet_path, labels_path, out_path=tmp_path / "gold.jsonl",
        manifest_path=tmp_path / "manifest.json", resolver=_Evidence(),
    )
    assert result["count"] == 1 and result["cross_turn_count"] == 1
    imported = json.loads((tmp_path / "gold.jsonl").read_text(encoding="utf-8"))
    assert imported["gold_provenance"] == "human_reviewed_v1"

    labels["reviewer_id"] = "codex-agent"
    _write_json(labels_path, labels)
    with pytest.raises(ReviewError):
        import_gold(packet_path, labels_path, out_path=tmp_path / "x", manifest_path=tmp_path / "y", resolver=_Evidence())


def test_human_gold_import_rejects_ineligible_and_synthetic(tmp_path: Path) -> None:
    row = {"case_id": "x", "id": "x", "query": "q", "split": "synthetic", "gold_evidence_refs": ["cm|1"]}
    packet = build_packet("gold", [row])
    labels = {"packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"], "reviewer_id": "human-01", "reviewed_at": "2026-07-17T12:00:00Z", "labels": [{"case_id": "x", "decision": "accept"}]}
    pp, lp = tmp_path / "p", tmp_path / "l"
    _write_json(pp, packet); _write_json(lp, labels)
    with pytest.raises(ReviewError):
        import_gold(pp, lp, out_path=tmp_path / "o", manifest_path=tmp_path / "m", resolver=_Evidence("ineligible"))


def test_llm_gold_import_requires_auditable_provenance_and_confidence(tmp_path: Path) -> None:
    packet = build_packet(
        "gold",
        [{
            "case_id": "llm-1", "id": "llm-1", "query": "reviewed query",
            "split": "llm_review_candidate", "gold_evidence_refs": ["cm|1"],
            "requires_cross_turn": True,
        }],
    )
    labels = {
        "packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"],
        "reviewer_type": "llm", "reviewer_id": "openai-gpt-5.6-luna",
        "model_id": "gpt-5.6-luna", "review_run_id": "run-primary",
        "prompt_version": "phase24-llm-review-v1", "reviewed_at": "2026-07-18T12:00:00Z",
        "labels": [{"case_id": "llm-1", "decision": "accept", "confidence": 0.91}],
    }
    pp, lp = tmp_path / "p.json", tmp_path / "l.json"
    _write_json(pp, packet); _write_json(lp, labels)
    result = import_gold(
        pp, lp, out_path=tmp_path / "gold.jsonl",
        manifest_path=tmp_path / "manifest.json", resolver=_Evidence(),
    )
    assert result["reviewer_type"] == "llm"
    imported = json.loads((tmp_path / "gold.jsonl").read_text(encoding="utf-8"))
    assert imported["gold_provenance"] == "llm_reviewed_v1"
    assert imported["model_id"] == "gpt-5.6-luna"

    labels["labels"][0].pop("confidence")
    _write_json(lp, labels)
    with pytest.raises(ReviewError, match="confidence"):
        import_gold(pp, lp, out_path=tmp_path / "x", manifest_path=tmp_path / "y", resolver=_Evidence())
