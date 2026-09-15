from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest
import yaml

from integration.scripts.governance import audit_artifacts
from integration.scripts.governance import build_project_inventory


ROOT = Path(__file__).resolve().parents[2]
PATH_POLICY = ROOT / "governance/policies/paths.yaml"
PRIVACY = ROOT / "governance/policies/privacy.yaml"
BUDGETS = ROOT / "governance/baselines/storage_budgets.yaml"


def _tree(root: Path) -> None:
    # Phase 20: live private roots are var/data; residual integration/* is legacy.
    (root / "integration/runtime").mkdir(parents=True)
    (root / "integration/runtime/private.db").write_bytes(b"DO-NOT-READ")
    (root / "integration/runtime/private.db-wal").write_bytes(b"DO-NOT-READ")
    (root / "integration/analysis").mkdir(parents=True)
    (root / "integration/analysis/report.json").write_text("SECRET", encoding="utf-8")
    # Quarantine cohort lives under archive/quarantine (not legacy _recycle).
    (root / "archive/quarantine/old").mkdir(parents=True)
    (root / "archive/quarantine/old/private.bin").write_bytes(b"SECRET")
    (root / "tests").mkdir()
    (root / "tests/test_ok.py").write_text("assert True", encoding="utf-8")


def _policy(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_audit_is_metadata_only_and_never_opens_private_bodies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path)
    original_open = builtins.open
    original_path_open = Path.open

    def guarded_open(file, *args, **kwargs):
        text = os.fspath(file).replace("\\", "/")
        if any(
            part in text
            for part in (
                "/integration/runtime/",
                "/integration/analysis/",
                "/archive/quarantine/",
                "/_recycle/",
            )
        ):
            raise AssertionError(f"private body read: {text}")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", lambda self, *a, **kw: guarded_open(self, *a, **kw))
    inventory = build_project_inventory.build_inventory(tmp_path, PATH_POLICY)
    result = audit_artifacts.audit(inventory, _policy(PRIVACY), _policy(BUDGETS))
    assert result["mode"] == "metadata-only"
    assert result["content_opened"] is False
    assert result["actions_executed"] == 0
    assert result["sidecar_nodes"] == 1
    assert result["orphaned_nodes"] == 0
    assert result["artifact_states"]["authoritative_mutable_private_store"] == 1
    assert result["lineage_findings"]["derived_rebuildability_unverified"] >= 1


def test_preview_has_approval_owner_reason_size_privacy_and_rollback(tmp_path: Path) -> None:
    _tree(tmp_path)
    inventory = build_project_inventory.build_inventory(tmp_path, PATH_POLICY)
    result = audit_artifacts.audit(inventory, _policy(PRIVACY), _policy(BUDGETS))
    assert result["privacy_violations"] == []
    assert result["cohorts"]
    for row in result["cohorts"]:
        assert row["approval_required"] is True
        assert row["owner"]
        assert row["reason"]
        assert row["bytes"] >= 0
        assert row["privacy_classes"]
        assert row["rollback"]
    dispositions = {row["cohort"]: row["proposed_disposition"] for row in result["cohorts"]}
    assert dispositions["recycle-quarantine"] == "archive"
    assert dispositions["private-databases"] == "keep"
    assert dispositions["derived-reports"] == "archive"


def test_over_budget_is_report_only_and_never_becomes_an_action(tmp_path: Path) -> None:
    _tree(tmp_path)
    inventory = build_project_inventory.build_inventory(tmp_path, PATH_POLICY)
    budgets = _policy(BUDGETS)
    budgets["budgets"] = {key: 1 for key in budgets["budgets"]}
    result = audit_artifacts.audit(inventory, _policy(PRIVACY), budgets)
    assert any(row["over"] for row in result["storage_budgets"].values())
    assert result["actions_executed"] == 0
