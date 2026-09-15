"""Phase 14 Wave 0.1 测试：eval dataset schema 和泄漏检查。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parents[1]
PUBLIC_EVAL_DIR = _ROOT / "assets" / "evals" / "knowledge_units"
PRIVATE_EVAL_DIR = _ROOT / "integration" / "evals" / "knowledge_units"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().split("\n") if line.strip()]


def test_synthetic_cases_schema() -> None:
    """synthetic cases 有完整 schema 字段。"""
    cases = _load_jsonl(PUBLIC_EVAL_DIR / "synthetic_cases.jsonl")
    assert len(cases) >= 10
    required = {"id", "split", "query", "gold_evidence_refs", "allowed_unit_types",
                "expected_abstain", "expected_conflict", "group"}
    for c in cases:
        assert required.issubset(c.keys()), f"missing fields in {c['id']}: {required - c.keys()}"


def test_synthetic_cases_cover_groups() -> None:
    """synthetic cases 覆盖 10 种场景。"""
    cases = _load_jsonl(PUBLIC_EVAL_DIR / "synthetic_cases.jsonl")
    groups = {c["group"] for c in cases}
    expected = {"preference", "project_decision", "capability", "time_conflict",
                "deprecated", "no_answer", "assistant_only", "subagent_only",
                "secret_ineligible", "cross_source_dup"}
    assert expected.issubset(groups), f"missing groups: {expected - groups}"


def test_dev_dataset_schema() -> None:
    """dev dataset 有 20 条且 schema 完整。"""
    cases = _load_jsonl(PRIVATE_EVAL_DIR / "dev_queries.private.jsonl")
    assert len(cases) == 20
    for c in cases:
        assert c["split"] == "dev"
        assert len(c["gold_evidence_refs"]) >= 1
        assert c["query"]


def test_frozen_dataset_schema() -> None:
    """frozen test dataset 有 20 条且 schema 完整。"""
    cases = _load_jsonl(PRIVATE_EVAL_DIR / "frozen_test_queries.private.jsonl")
    assert len(cases) == 20
    for c in cases:
        assert c["split"] == "frozen_test"
        assert len(c["gold_evidence_refs"]) >= 1


def test_no_dev_frozen_leak() -> None:
    """dev 和 frozen 之间无 evidence ref 泄漏。"""
    dev = _load_jsonl(PRIVATE_EVAL_DIR / "dev_queries.private.jsonl")
    frozen = _load_jsonl(PRIVATE_EVAL_DIR / "frozen_test_queries.private.jsonl")
    dev_refs = set()
    for c in dev:
        dev_refs.update(c["gold_evidence_refs"])
    frozen_refs = set()
    for c in frozen:
        frozen_refs.update(c["gold_evidence_refs"])
    leak = dev_refs & frozen_refs
    assert len(leak) == 0, f"泄漏: {leak}"


def test_merge_positive_pairs() -> None:
    """merge positive 有 20 对且 should_merge=True。"""
    pairs = _load_jsonl(PRIVATE_EVAL_DIR / "merge_positive_pairs.private.jsonl")
    assert len(pairs) == 20
    for p in pairs:
        assert p["should_merge"] is True
        assert p["unit_a_ref"] != p["unit_b_ref"]


def test_hard_negative_pairs() -> None:
    """hard negative 有 20 对且 should_merge=False。"""
    pairs = _load_jsonl(PRIVATE_EVAL_DIR / "hard_negative_pairs.private.jsonl")
    assert len(pairs) == 20
    for p in pairs:
        assert p["should_merge"] is False
        assert p["unit_a_ref"] != p["unit_b_ref"]


def test_readme_exists() -> None:
    """README 文档存在。"""
    assert (PUBLIC_EVAL_DIR / "README.md").exists()
