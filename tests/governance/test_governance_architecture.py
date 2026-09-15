from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_zones_are_unambiguous_and_complete():
    policy = yaml.safe_load((ROOT / "governance/policies/architecture.yaml").read_text(encoding="utf-8"))
    required = {"src", "tests", "assets", "docs", "governance", "data", "var", "archive", "planning"}
    assert set(policy["zones"]) == required
    paths = [path for zone in policy["zones"].values() for path in zone["paths"]]
    assert len(paths) == len(set(paths))


def test_foundation_cannot_depend_upward():
    policy = yaml.safe_load((ROOT / "governance/policies/architecture.yaml").read_text(encoding="utf-8"))
    core = policy["modules"]["core"]
    assert core["may_import"] == ["core"]
    forbidden = next(row for row in policy["forbidden"] if row["from"] == "core")
    assert "services" in forbidden["to"] and "pipeline" in forbidden["to"]


def test_all_stable_script_modules_have_architecture_mapping():
    architecture = yaml.safe_load((ROOT / "governance/policies/architecture.yaml").read_text(encoding="utf-8"))
    stable = yaml.safe_load((ROOT / "governance/stable_modules.yaml").read_text(encoding="utf-8"))
    expected = {Path(row["path"]).name for row in stable["modules"] if row["path"].startswith("integration/scripts/") and row["status"] == "supported"}
    assert expected <= set(architecture["modules"])

