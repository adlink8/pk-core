"""Resolve the compatibility surface and enforce baseline-only-down."""
from __future__ import annotations

import argparse
import ast
import json
import re
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "tools" / "compat" / "v1_1"
MANIFEST = ROOT / "governance" / "manifests" / "entrypoints.yaml"
TARGET = re.compile(r"Compatibility shim ->\s*([A-Za-z0-9_.]+)")


def _baseline_errors(actual: int, expected: int, label: str, *, only_down: bool) -> list[str]:
    if actual > expected:
        return [f"{label} budget increased: expected at most {expected}, found {actual}"]
    if not only_down and actual != expected:
        return [f"{label} baseline drift: expected {expected}, found {actual}"]
    return []


def discover_shims() -> list[dict[str, object]]:
    result = []
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        match = TARGET.search(text[:500])
        if not match:
            continue
        tree = ast.parse(text)
        imported = [node.args[0].value for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "import_module" and node.args
                    and isinstance(node.args[0], ast.Constant)]
        target = imported[0] if imported else match.group(1)
        target_path = importlib.util.find_spec(target)
        imports_target = bool(imported)
        result.append({"path": path.relative_to(ROOT).as_posix(), "target": target,
                       "target_exists": target_path is not None, "static_parity": imports_target,
                       "consumer": "legacy CLI callers (unknown until telemetry)",
                       "owner": target.split(".", 1)[0], "deprecated": True,
                       "remove_after": "human-approved cohort only"})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    shims = discover_shims()
    tools_manifest = json.loads((ROOT / "governance/manifests/source/tools.json").read_text(encoding="utf-8"))
    tools = tools_manifest["entries"]
    errors = []
    only_down = bool(manifest.get("baseline_only_down"))
    errors.extend(_baseline_errors(
        len(shims), int(manifest["shim_registry"]["expected_count"]), "shim", only_down=only_down
    ))
    errors.extend(_baseline_errors(
        len(tools), int(manifest["tool_registry"]["expected_count"]), "tool", only_down=only_down
    ))
    errors.extend(f"invalid target/parity: {s['path']}" for s in shims if not s["target_exists"] or not s["static_parity"])
    result = {"ok": not errors, "shim_count": len(shims), "tool_count": len(tools),
              "errors": errors, "resolved_shims": shims,
              "retirement_preview": manifest["retirement_cohorts"] if args.preview else []}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
          f"shim-budget {'PASS' if result['ok'] else 'FAIL'}: {len(shims)} shims, {len(tools)} tools")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
