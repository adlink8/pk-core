from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from personal_knowledge.governance.artifact_registry import load_registry, validate_registry
from personal_knowledge.intelligence.analysis.runs import AnalysisRunError, load_policy


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "governance" / "policies" / "decision_analysis.yaml"


def test_analysis_authority_is_unique_non_serving_and_dual_parented() -> None:
    registry = load_registry()
    assert validate_registry(registry) == []
    rows = [item for item in registry["artifacts"] if item["id"] == "a.decision_analysis"]
    assert len(rows) == 1
    row = rows[0]
    assert row["authority_role"] == "decision_analysis"
    assert row["layer"] == "A" and row["privacy"] == "R4" and row["lifecycle"] == "immutable"
    assert row["evidence_parents"] == ["a.personal_change", "s.external_fact"]
    assert row["authority_role"] not in registry["required_serving_roles"]


def test_policy_is_versioned_fail_closed_and_checksum_changes_on_drift(tmp_path: Path) -> None:
    policy, original_checksum = load_policy(POLICY)
    assert policy["domain"] == {"allow": ["project"], "deny": ["health", "medical", "finance", "investment", "relationship"]}
    assert policy["abstention"]["fail_closed"] is True
    assert policy["evidence"]["require_every_factual_claim"] is True
    drifted = deepcopy(policy)
    drifted["authority"]["evidence_parents"] = ["a.personal_change"]
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    try:
        load_policy(path)
    except AnalysisRunError as exc:
        assert exc.code == "policy_evidence_parent_mismatch"
    else:
        raise AssertionError("policy parent drift must fail closed")
    drifted = deepcopy(policy)
    drifted["sampling"]["max_candidates"] += 1
    path.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    _, drifted_checksum = load_policy(path)
    assert drifted_checksum != original_checksum
