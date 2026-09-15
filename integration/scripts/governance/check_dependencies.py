"""Validate reproducible dependency contracts without installing packages."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
REQ = re.compile(r"^([A-Za-z0-9_.-]+)\s*(?:\[[^]]+\])?\s*([<>=!~].*)?$")


def _requirements(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "-c")):
            continue
        match = REQ.match(line)
        if not match:
            raise ValueError(f"unsupported requirement syntax in {path.name}: {line}")
        result[match.group(1).lower()] = (match.group(2) or "").strip()
    return result


def check(root: Path = ROOT) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    policy_path = root / "governance/policies/dependencies.yaml"
    policy: dict[str, Any] = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    py = policy["python"]
    required_files = [py["core"], py["dev"], py["optional"], py["constraints"]]
    for name in required_files:
        if not (root / name).is_file():
            findings.append({"id": f"dependency:missing:{name}", "severity": "P0", "owner": policy["owner"], "policy": policy["policy_id"]})
    if findings:
        return findings
    declared = _requirements(root / py["core"]) | _requirements(root / py["dev"]) | _requirements(root / py["optional"])
    constrained = _requirements(root / py["constraints"])
    for package in sorted(declared):
        pin = constrained.get(package, "")
        if not re.fullmatch(r"==[^,;\s]+", pin):
            findings.append({"id": f"dependency:unpinned:{package}", "severity": "P1", "owner": policy["owner"], "policy": policy["policy_id"]})
    for workspace in policy["node"]["workspaces"]:
        directory = root / workspace["path"]
        manifest = json.loads((directory / workspace["manifest"]).read_text(encoding="utf-8"))
        lock = json.loads((directory / workspace["lock"]).read_text(encoding="utf-8"))
        root_lock = lock.get("packages", {}).get("", {})
        for key in ("name", "version"):
            if manifest.get(key) != root_lock.get(key):
                findings.append({"id": f"dependency:node-lock-drift:{workspace['path']}:{key}", "severity": "P0", "owner": policy["owner"], "policy": policy["policy_id"]})
        if lock.get("lockfileVersion") != 3:
            findings.append({"id": f"dependency:node-lock-version:{workspace['path']}", "severity": "P1", "owner": policy["owner"], "policy": policy["policy_id"]})
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    findings = check(args.root.resolve())
    result = {"ok": not findings, "policy": "dependency-reproducibility-v1", "findings": findings}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"dependency-lock {'PASS' if result['ok'] else 'FAIL'}: {len(findings)} findings")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
