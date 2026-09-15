from __future__ import annotations

import json
from pathlib import Path

import pytest

from integration.scripts.governance import apply_repository_migration as executor
from integration.scripts.governance import plan_repository_migration as planner


def mapping(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "docs-01", "cohort": "approved-test", "operation": "move",
        "source": "old/readme.md", "target": "docs/readme.md",
        "reason": "test architecture mapping", "owner": "docs", "deps": [],
    }
    row.update(overrides)
    return row


def test_recorded_keep_and_defer_decisions_generate_zero_operations(tmp_path: Path) -> None:
    rows = [mapping(id=name, cohort=name) for name in planner.DECISIONS]
    result = planner.build_manifest(tmp_path, rows, dirty=[])
    assert result["operations"] == []
    assert result["inverse_operations"] == []
    assert result["actions_executed"] == 0
    assert result["unauthorized_delete_operations"] == 0
    assert {row["decision"] for row in result["excluded_mappings"]} == {"keep", "deferred"}


def test_approved_mapping_has_complete_audit_and_inverse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(planner.DECISIONS, "approved-test", "approved-execute")
    result = planner.build_manifest(tmp_path, [mapping()], dirty=[])
    operation = result["operations"][0]
    for field in ("reason", "owner", "precheck", "postcheck", "rollback", "inverse", "prestate"):
        assert operation[field]
    assert "deps" in operation
    assert operation["inverse"] == {"operation": "move", "source": "docs/readme.md", "target": "old/readme.md"}
    assert result["inverse_operations"][0]["operation_id"] == "docs-01"


def test_dirty_source_or_parent_overlap_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(planner.DECISIONS, "approved-test", "approved-execute")
    result = planner.build_manifest(tmp_path, [mapping()], dirty=["old/readme.md", "docs"])
    assert result["blocked_operations"] == ["docs-01"]
    assert result["shadow_verification"]["result"] == "FAIL"
    assert result["operations"][0]["dirty_overlap"] == ["docs", "old/readme.md"]


def test_delete_operation_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(planner.DECISIONS, "approved-test", "approved-execute")
    with pytest.raises(ValueError, match="destructive"):
        planner.build_manifest(tmp_path, [mapping(operation="delete")], dirty=[])


def test_executor_dry_run_never_mutates(tmp_path: Path) -> None:
    manifest = planner.build_manifest(tmp_path, [], dirty=[])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = sorted(tmp_path.iterdir())
    loaded = executor.load_manifest(path)
    result = executor.dry_run(loaded)
    assert result == {"mode": "dry-run", "cohort": None, "operations_selected": 0,
                      "blocked_operations": [], "prestate_drift": [],
                      "actions_executed": 0, "result": "PASS"}
    assert sorted(tmp_path.iterdir()) == before


def test_executor_rejects_manifest_with_delete_count(tmp_path: Path) -> None:
    manifest = planner.build_manifest(tmp_path, [], dirty=[])
    manifest["unauthorized_delete_operations"] = 1
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="delete"):
        executor.load_manifest(path)


def test_executor_detects_prestate_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(planner.DECISIONS, "approved-test", "approved-execute")
    source = tmp_path / "old" / "readme.md"
    source.parent.mkdir()
    source.write_text("before", encoding="utf-8")
    manifest = planner.build_manifest(tmp_path, [mapping()], dirty=[])
    source.write_text("changed after preview", encoding="utf-8")
    result = executor.dry_run(manifest)
    assert result["result"] == "FAIL"
    assert result["prestate_drift"] == ["docs-01"]
