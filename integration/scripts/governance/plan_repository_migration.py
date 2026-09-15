"""Build an auditable repository migration preview.

The planner is deliberately non-mutating.  A mapping becomes executable only
when its exact cohort is explicitly approved; keep/deferred decisions produce
no operation.  Existing worktree changes make an overlapping mapping fail
closed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DECISIONS = {
    "active-or-source": "keep",
    "private-databases": "keep",
    "raw-and-imports": "keep",
    "derived-reports": "deferred",
    "ephemeral-caches": "deferred",
    "recycle-quarantine": "deferred",
    "shim-cohort-01-leaf-libraries": "deferred",
}


def _normal(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def paths_overlap(left: str, right: str) -> bool:
    left, right = _normal(left), _normal(right)
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def git_dirty_paths(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    dirty: set[str] = set()
    records = result.stdout.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        path = record[3:]
        if record[:2] in {"R ", "C ", "RM", "CM"} and index < len(records):
            dirty.add(_normal(path))
            dirty.add(_normal(records[index]))
            index += 1
        else:
            dirty.add(_normal(path))
    return dirty


def snapshot(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "node_type": "directory" if path.is_dir() else "file",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _operation(root: Path, row: dict[str, Any], dirty: Iterable[str]) -> dict[str, Any]:
    source = _normal(str(row["source"]))
    target = _normal(str(row["target"]))
    overlap = sorted(path for path in dirty if paths_overlap(path, source) or paths_overlap(path, target))
    operation = str(row.get("operation", "move"))
    if operation not in {"mkdir", "move", "shim", "update-import", "update-doc"}:
        raise ValueError(f"unsupported or destructive operation: {operation}")
    inverse = {
        "operation": "move" if operation == "move" else "restore-prestate",
        "source": target,
        "target": source,
    }
    return {
        "id": str(row["id"]),
        "cohort": str(row["cohort"]),
        "operation": operation,
        "source": source,
        "target": target,
        "reason": str(row["reason"]),
        "owner": str(row["owner"]),
        "deps": list(row.get("deps", [])),
        "precheck": ["source-state-matches-preview", "target-state-matches-preview", "dirty-overlap=0"],
        "postcheck": ["target-state-matches-expected", "source-state-matches-expected", "governance-preflight"],
        "rollback": inverse,
        "inverse": inverse,
        "dirty_overlap": overlap,
        "prestate": {"source": snapshot(root, source), "target": snapshot(root, target)},
        "status": "blocked-dirty-overlap" if overlap else "ready-for-explicit-approval",
    }


def build_manifest(root: Path, mappings: list[dict[str, Any]], dirty: Iterable[str]) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for row in mappings:
        cohort = str(row["cohort"])
        decision = DECISIONS.get(cohort, "unapproved")
        if decision != "approved-execute":
            excluded.append({"id": str(row["id"]), "cohort": cohort, "decision": decision})
            continue
        operations.append(_operation(root, row, dirty))
    blocked = [op["id"] for op in operations if op["status"].startswith("blocked-")]
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root.resolve()),
        "mode": "preview-only",
        "authorization": "exact-manifest-human-checkpoint-required",
        "decisions": DECISIONS,
        "cohorts": [
            {"id": name, "decision": decision, "executable": decision == "approved-execute"}
            for name, decision in DECISIONS.items()
        ],
        "operations": operations,
        "inverse_operations": [op["inverse"] | {"operation_id": op["id"]} for op in reversed(operations)],
        "excluded_mappings": excluded,
        "blocked_operations": blocked,
        "unauthorized_delete_operations": 0,
        "actions_executed": 0,
        "shadow_verification": {
            "result": "PASS" if not blocked else "FAIL",
            "operation_count": len(operations),
            "note": "Logical target tree only; no filesystem mutation was performed.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a non-mutating repository migration preview")
    default_root = Path(__file__).resolve().parents[3]
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--mappings", type=Path)
    parser.add_argument("--output", type=Path, default=Path("integration/runtime/governance/migration_preview.json"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    mappings: list[dict[str, Any]] = []
    if args.mappings:
        payload = json.loads(args.mappings.read_text(encoding="utf-8"))
        mappings = payload["mappings"] if isinstance(payload, dict) else payload
    manifest = build_manifest(root, mappings, git_dirty_paths(root))
    # Keep the persisted manifest portable and avoid embedding a user-specific path.
    manifest["root"] = "."
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "mode": manifest["mode"],
        "operations": len(manifest["operations"]),
        "blocked": len(manifest["blocked_operations"]),
        "actions_executed": 0,
        "output": str(output),
    }, ensure_ascii=False))
    return 1 if manifest["blocked_operations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
