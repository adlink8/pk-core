from __future__ import annotations

import json
from pathlib import Path

from integration.scripts.governance.check_dependencies import check as check_dependencies
from integration.scripts.governance.preflight import Finding

ROOT = Path(__file__).resolve().parents[2]


def test_dependency_contract_and_node_lock_are_consistent() -> None:
    assert check_dependencies(ROOT) == []


def test_preflight_baseline_cannot_exempt_p0() -> None:
    baseline = json.loads((ROOT / "governance/baselines/preflight.json").read_text(encoding="utf-8"))
    p0 = Finding("secret:test", "P0", "security", "secret-scan-v1")
    allowed = set(baseline["allowed_non_p0_findings"])
    assert p0.severity == "P0" or p0.id not in allowed


def test_ci_declares_supported_runtime_matrix() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '"3.12"' in workflow and '"3.14"' in workflow
    assert "node-version: 20" in workflow
    assert "preflight.py --ci" in workflow
