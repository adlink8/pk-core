from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> dict:
    return yaml.safe_load((ROOT / "governance/policies" / name).read_text(encoding="utf-8"))


def test_privacy_policy_is_fail_closed_and_covers_all_classes() -> None:
    policy = _load("privacy.yaml")
    assert policy["default_class"] == "R4"
    assert policy["unknown_behavior"] == "fail_closed"
    assert set(policy["classes"]) == {"R1", "R2", "R3", "R4"}
    assert policy["classes"]["R3"]["content_audit"] == "prohibited"
    assert policy["classes"]["R4"]["content_audit"] == "prohibited"
    assert policy["classes"]["R4"]["package_allowed"] is False
    assert policy["classes"]["R4"]["public_report_allowed"] is False


def test_r3_r4_cannot_be_tracked_or_packaged() -> None:
    policy = _load("privacy.yaml")
    for klass in ("R3", "R4"):
        row = policy["classes"][klass]
        assert "track" not in row["allowed_git_policies"]
        assert row["package_allowed"] is False


def test_retention_covers_every_logical_zone_and_deletion_lineage() -> None:
    policy = _load("retention.yaml")
    covered = {zone for row in policy["policies"].values() for zone in row.get("zones", [])}
    assert covered == {"src", "tests", "assets", "docs", "governance", "planning", "tooling", "data", "var", "archive", "vendor"}
    assert policy["sidecars"]["rule"] == "inherit_parent_store_policy"
    assert policy["deletion_lineage"]["stages"] == ["raw", "normalized", "canonical", "candidate", "vector", "report", "backup", "archive"]
    assert "approval" in policy["deletion_lineage"]["required_evidence"]
    assert "rollback_or_restore" in policy["deletion_lineage"]["required_evidence"]

