"""Controlled migration executor skeleton.

Dry-run is always safe.  Apply/rollback are intentionally unavailable until an
exact manifest receives a later, cohort-specific approval record.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from integration.scripts.governance.plan_repository_migration import snapshot
except ModuleNotFoundError:
    from plan_repository_migration import snapshot


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema_version", "mode", "operations", "inverse_operations", "blocked_operations"}
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"manifest missing fields: {missing}")
    if payload.get("unauthorized_delete_operations") != 0:
        raise ValueError("manifest contains unauthorized delete operations")
    return payload


def dry_run(manifest: dict[str, Any], cohort: str | None = None) -> dict[str, Any]:
    operations = [op for op in manifest["operations"] if cohort is None or op["cohort"] == cohort]
    blocked = [op["id"] for op in operations if op.get("status", "").startswith("blocked-")]
    root = Path(manifest["root"]).resolve()
    drifted = []
    for operation in operations:
        expected = operation.get("prestate", {})
        actual = {
            "source": snapshot(root, operation["source"]),
            "target": snapshot(root, operation["target"]),
        }
        if expected != actual:
            drifted.append(operation["id"])
    return {
        "mode": "dry-run",
        "cohort": cohort,
        "operations_selected": len(operations),
        "blocked_operations": blocked,
        "prestate_drift": drifted,
        "actions_executed": 0,
        "result": "PASS" if not blocked and not drifted else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a repository migration manifest")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cohort")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.apply or args.rollback:
        parser.error("physical apply/rollback is disabled pending a new exact cohort approval")
    result = dry_run(manifest, args.cohort)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
