from __future__ import annotations

import builtins
import json
import os
import stat
from pathlib import Path

import jsonschema
import pytest

from integration.scripts.governance import build_project_inventory as inventory


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "governance/policies/paths.yaml"
SCHEMA = ROOT / "governance/schema/file_inventory.schema.json"


def _fixture_tree(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "tests/example.py").write_text("assert True", encoding="utf-8")
    (root / "empty").mkdir()
    (root / "deep/a/b/c").mkdir(parents=True)
    (root / "deep/a/b/c/leaf.txt").write_text("leaf", encoding="utf-8")
    (root / "integration/runtime/private_evals").mkdir(parents=True)
    (root / "integration/runtime/private_evals/secret.txt").write_text("DO-NOT-READ", encoding="utf-8")
    # Phase 20 quarantine zone (replaces legacy _recycle policy id "quarantine").
    (root / "archive/quarantine/nested").mkdir(parents=True)
    (root / "archive/quarantine/nested/old.bin").write_bytes(b"private")
    (root / ".git/objects").mkdir(parents=True)
    (root / ".git/objects/ignored").write_bytes(b"git")


def test_schema_accepts_complete_inventory(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    result = inventory.build_inventory(tmp_path, POLICY)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result)
    assert result["summary"]["coverage_percent"] == 100.0
    assert result["summary"]["metadata_completeness_percent"] == 100.0


def test_schema_rejects_empty_required_metadata(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    policy = inventory.load_policy(POLICY)
    policy["rules"][-1]["owner_module"] = ""
    bad_policy = tmp_path / "bad.yaml"
    bad_policy.write_text(__import__("yaml").safe_dump(policy), encoding="utf-8")
    with pytest.raises(inventory.GovernanceError, match="empty"):
        inventory.build_inventory(tmp_path, bad_policy)


def test_schema_requires_reason_for_na(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    result = inventory.build_inventory(tmp_path, POLICY)
    row = result["nodes"][0]
    row["na_reasons"].pop("replacement")
    with pytest.raises(inventory.GovernanceError, match="lacks reason"):
        inventory._validate_na(row)


def test_ordered_policy_is_unique_for_all_fixture_types(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    rules = inventory.load_policy(POLICY)["rules"]
    expected = {
        "tests/example.py": "tests",
        "empty": "root-default",
        "deep/a/b/c/leaf.txt": "root-default",
        "integration/runtime/private_evals/secret.txt": "private-runtime-legacy",
        "archive/quarantine/nested/old.bin": "archive-quarantine",
        ".git": "git-internal",
    }
    for path, rule_id in expected.items():
        assert inventory.select_policy(path, rules)["id"] == rule_id
    result = inventory.build_inventory(tmp_path, POLICY)
    paths = [row["path"] for row in result["nodes"]]
    assert len(paths) == len(set(paths))
    assert ".git" in paths
    assert not any(path.startswith(".git/") for path in paths)
    assert "empty" in paths
    assert "archive/quarantine/nested/old.bin" in paths


def test_private_nodes_are_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_tree(tmp_path)
    original_open = builtins.open
    original_path_open = Path.open

    def guarded_open(file, *args, **kwargs):
        text = os.fspath(file).replace("\\", "/")
        if (
            "/integration/runtime/" in text
            or "/archive/quarantine/" in text
            or "/_recycle/" in text
        ):
            raise AssertionError(f"private body read attempted: {text}")
        return original_open(file, *args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        return guarded_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    result = inventory.build_inventory(tmp_path, POLICY)
    assert result["summary"]["by_privacy"]["R4"] >= 2


def test_symlink_or_reparse_is_not_traversed(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    result = inventory.build_inventory(tmp_path, POLICY)
    row = next(item for item in result["nodes"] if item["path"] == "link")
    assert row["node_type"] in {"symlink", "reparse"}
    assert not any(item["path"].startswith("link/") for item in result["nodes"])


def test_reparse_fixture_is_classified_without_following_target() -> None:
    class Info:
        st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    class Entry:
        def is_symlink(self):
            return False

        def stat(self, follow_symlinks=False):
            assert follow_symlinks is False
            return Info()

    assert inventory._node_type(Entry(), stat.S_IFDIR) == "reparse"


def test_ambiguous_equal_precedence_fails_closed() -> None:
    rules = [
        {"id": "a", "include": ["same/**"], "priority": 1},
        {"id": "b", "include": ["same/**"], "priority": 1},
    ]
    with pytest.raises(inventory.GovernanceError, match="ambiguous"):
        inventory.select_policy("same/file", rules)
