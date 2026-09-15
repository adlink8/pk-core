"""Single local/CI governance entrypoint; reports only sanitized findings."""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

import yaml

from personal_knowledge.governance.artifact_registry import registry_report

try:
    from integration.scripts.governance.check_dependencies import check as dependency_check
except ModuleNotFoundError:
    from check_dependencies import check as dependency_check

ROOT = Path(__file__).resolve().parents[3]
SAFE_TEXT_ROOTS = ("src", "integration/scripts", "apps", "tests", "docs", "governance", ".github")
SKIP = {"runtime", "db", "analysis", "node_modules", "__pycache__"}
SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
}


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    owner: str
    policy: str


def _run(name: str, command: list[str], root: Path) -> tuple[bool, str]:
    proc = subprocess.run(command, cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
    detail = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode == 0, detail[-1][:300] if detail else f"exit={proc.returncode}"


def _architecture(root: Path) -> list[Finding]:
    policy = yaml.safe_load((root / "governance/policies/architecture.yaml").read_text(encoding="utf-8"))
    modules = policy["modules"]
    result: list[Finding] = []
    for source_name, spec in modules.items():
        base = root / spec["path"]
        allowed = set(spec["may_import"])
        for path in base.glob("*.py"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            imported: set[str] = set()
            for node in ast.walk(tree):
                name = node.module if isinstance(node, ast.ImportFrom) else None
                if name and name.startswith("integration.scripts."):
                    imported.add(name.split(".")[2])
            for target in sorted(imported - allowed):
                result.append(Finding(f"architecture:{source_name}-to-{target}", "P1", source_name, "architecture-boundary-v1"))
    return result


def _secrets(root: Path) -> list[Finding]:
    result: list[Finding] = []
    for name in SAFE_TEXT_ROOTS:
        base = root / name
        paths = base.rglob("*") if base.is_dir() else [base]
        for path in paths:
            if not path.is_file() or any(part in SKIP for part in path.relative_to(root).parts):
                continue
            if path.suffix.lower() not in {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".mjs", ".html", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(root).as_posix()
            for line in text.splitlines():
                # Synthetic leak fixtures must opt in on the exact source line.
                if "governance: synthetic-secret-fixture" in line:
                    continue
                # Do not flag the scanner's own signatures as credentials.
                if rel.endswith("src/personal_knowledge/governance/preflight.py") and "re.compile" in line:
                    continue
                if rel.endswith("src/personal_knowledge/core/privacy_guard.py") and "re.compile" in line:
                    continue
                for kind, pattern in SECRET_PATTERNS.items():
                    if pattern.search(line):
                        result.append(Finding(f"secret:{kind}:{rel}", "P0", "security", "secret-scan-v1"))
                        break
    return result


def evaluate(root: Path = ROOT, *, run_tests: bool = False) -> dict[str, object]:
    python = sys.executable
    with tempfile.TemporaryDirectory(prefix="pda-governance-") as temp:
        inventory = str(Path(temp) / "inventory.json")
        commands = [
            ("inventory-check", [python, "integration/scripts/governance/build_project_inventory.py", "--check", "--private-output", inventory]),
            ("privacy-check", [python, "integration/scripts/governance/audit_artifacts.py", "--inventory", inventory, "--check", "--no-content"]),
            ("path-policy", [python, "integration/scripts/governance/check_path_policy.py", "--check"]),
            ("shim-budget", [python, "integration/scripts/governance/check_shim_budget.py", "--check"]),
            ("docs-coverage", [python, "integration/scripts/governance/check_docs_coverage.py", "--check"]),
            ("planning-consistency", [python, "integration/scripts/governance/check_planning_consistency.py", "--check"]),
        ]
        gates = [{"gate": name, "ok": ok, "detail": detail, "owner": "engineering-governance", "policy": name + "-v1"}
                 for name, command in commands for ok, detail in [_run(name, command, root)]]
    findings = [Finding(**item) for item in dependency_check(root)] + _architecture(root) + _secrets(root)
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    matrix_ok = all(token in workflow for token in ('"3.12"', '"3.14"', "node-version: 20", "preflight.py --ci"))
    privacy_ok = next(row["ok"] for row in gates if row["gate"] == "privacy-check")
    gates.extend([
        {"gate": "dependency-lock", "ok": not any(f.id.startswith("dependency:") for f in findings), "detail": "contract-only; no install performed", "owner": "engineering-governance", "policy": "dependency-reproducibility-v1"},
        {"gate": "architecture-boundary", "ok": not any(f.id.startswith("architecture:") for f in findings), "detail": "AST import boundary scan", "owner": "engineering-governance", "policy": "architecture-boundary-v1"},
        {"gate": "secret-scan", "ok": not any(f.id.startswith("secret:") for f in findings), "detail": "safe source roots only; private zones excluded", "owner": "security", "policy": "secret-scan-v1"},
        {"gate": "artifact-lineage", "ok": privacy_ok, "detail": "metadata-only audit; legacy rebuildability debt is report-only baseline", "owner": "data-platform", "policy": "retention-v1"},
        {"gate": "storage-retention", "ok": privacy_ok, "detail": "budgets checked; no disposition action executed", "owner": "data-platform", "policy": "retention-v1"},
        {"gate": "test-matrix", "ok": matrix_ok, "detail": "Python 3.12/3.14 and Node 20 declared in CI", "owner": "engineering-governance", "policy": "test-matrix-v1"},
    ])
    registry = registry_report(root / "governance" / "policies" / "artifact_layers.yaml")
    gates.append({
        "gate": "artifact-layer-registry",
        "ok": bool(registry["ok"]),
        "detail": (
            f"{registry['artifacts']} typed artifacts; authority uniqueness and dependency direction checked"
            if registry["ok"]
            else json.dumps(registry["issues"], ensure_ascii=False, sort_keys=True)
        ),
        "owner": "data-platform",
        "policy": "artifact-layer-registry-v1",
    })
    if run_tests:
        ok, detail = _run("tests", [python, "-m", "pytest", "-q", "tests/test_governance_*.py"], root)
        gates.append({"gate": "governance-tests", "ok": ok, "detail": detail, "owner": "engineering-governance", "policy": "test-matrix-v1"})
    baseline = json.loads((root / "governance/baselines/preflight.json").read_text(encoding="utf-8"))
    allowed = set(baseline["allowed_non_p0_findings"])
    blocking = [f for f in findings if f.severity == "P0" or f.id not in allowed]
    failed_gates = [g for g in gates if not g["ok"]]
    return {"schema_version": "1.0", "policy_id": baseline["policy_id"], "ok": not blocking and not failed_gates,
            "gates": gates, "findings": [asdict(f) for f in findings], "blocking_findings": [asdict(f) for f in blocking],
            "baseline_rule": "P0 never exempt; only named non-P0 IDs may be grandfathered."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--json-output", type=Path, default=Path("integration/runtime/governance/preflight.json"))
    args = parser.parse_args(argv)
    result = evaluate(ROOT, run_tests=False)
    output = ROOT / args.json_output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for gate in result["gates"]:
        print(f"{'PASS' if gate['ok'] else 'FAIL'} {gate['gate']}: {gate['detail']} [owner={gate['owner']} policy={gate['policy']}]")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
