"""Phase 20-01: data disposition coverage contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from personal_knowledge.governance import data_disposition as dd  # noqa: E402


def test_disposition_rules_cover_critical_prefixes() -> None:
    cases = {
        "Agent/structured/db/agent_data.sqlite": "relocate",
        "Google/raw/Takeout/x.json": "relocate",
        "Google/structured/db/google_data.sqlite": "relocate",
        "imports/batches/x": "relocate",
        "integration/db/personal_system.sqlite": "relocate",
        "integration/runtime/private_evals/x.jsonl": "relocate",
        "integration/analysis/ai_context/y.json": "relocate",
        "logs/worker_0.log": "relocate",
        "_recycle/README.md": "relocate",
        ".gsd/foo": "relocate",
        ".ai-bridge/x": "relocate",
        "src/personal_knowledge/cli.py": "retain-in-place",
        "governance/policies/paths.yaml": "retain-in-place",
        ".planning/STATE.md": "retain-in-place",
        "pyproject.toml": "retain-in-place",
        "tests/governance/test_physical_data_layout.py": "retain-in-place",
        "path/__pycache__/x.pyc": "cache-redirect",
    }
    for path, expected in cases.items():
        got = dd.decide(path)
        assert got.disposition == expected, f"{path}: {got.disposition} != {expected}"


def test_agentsview_is_protected_external() -> None:
    assert dd.PROTECTED_EXTERNAL["disposition"] == "protected-external"
    assert "agentsview" in dd.PROTECTED_EXTERNAL["path"].lower()


def test_disposition_build_from_inventory_coverage() -> None:
    inv = ROOT / "integration" / "runtime" / "governance" / "phase19_final_inventory.json"
    if not inv.exists():
        pytest.skip("phase19 inventory missing")
    payload = dd.build_from_inventory(inv)
    assert payload["coverage_percent"] == 100.0
    assert payload["conflict"] == 0
    assert payload["unknown"] == 0
    assert payload["node_count"] >= 1000
    dispositions = {e["disposition"] for e in payload["entries"]}
    assert "relocate" in dispositions
    assert "retain-in-place" in dispositions
    assert "protected-external" in dispositions
    # no bare phase20-pending leftovers
    assert "phase20-pending" not in dispositions
    # root allowlist present
    assert "data/" in payload["root_final_allowlist"]
    assert "var/" in payload["root_final_allowlist"]


def test_disposition_artifact_roundtrip_if_written() -> None:
    path = ROOT / "governance" / "manifests" / "data_disposition.json"
    if not path.exists():
        pytest.skip("artifact not generated yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["manifest_sha256"]
    assert payload["conflict"] == 0
