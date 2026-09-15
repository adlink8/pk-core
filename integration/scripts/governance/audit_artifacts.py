from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from integration.scripts.governance.build_project_inventory import build_inventory
except ModuleNotFoundError:  # Direct script execution keeps only this directory on sys.path.
    from build_project_inventory import build_inventory


PRIVATE = {"R3", "R4"}
SIDECARS = (".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".journal", ".bak", ".backup")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"invalid policy: {path}")
    return value


def _cohort(node: dict[str, Any]) -> tuple[str, str, str]:
    """Map inventory policy_id → disposition cohort (Phase 20 path renames)."""
    policy = node["policy_id"]
    path = str(node.get("path", "")).replace("\\", "/")
    # Quarantine: historical `_recycle` / `quarantine` and live `archive/quarantine`.
    if policy in {"quarantine", "archive-quarantine"}:
        return "recycle-quarantine", "archive", "Quarantined history requires owner review."
    if policy in {
        "private-agent",
        "private-google-imports",
        "private-agent-legacy",
        "private-google-imports-legacy",
        "private-data-tree",
    }:
        return "raw-and-imports", "keep", "Raw/linkable evidence is held; no automated disposition."
    runtime_like = policy in {"private-runtime", "private-runtime-legacy", "private-var"}
    if runtime_like and node["format"] in {"db", "sqlite", "sqlite3"}:
        return "private-databases", "keep", "Mutable private stores require backup and sandbox restore evidence."
    # Derived analysis reports (legacy private-analysis or runtime-like analysis paths).
    if policy == "private-analysis" or (
        runtime_like
        and node["format"] not in {"db", "sqlite", "sqlite3"}
        and ("/analysis/" in f"/{path}/" or path.endswith("/analysis") or "/analysis" in path)
    ):
        return "derived-reports", "archive", "Derived outputs may be archived only after rebuildability review."
    if node["kind"] == "runtime" and node["privacy_class"] in {"R1", "R2"}:
        return "ephemeral-caches", "delete-candidate", "Caches are candidates only after process/lock review."
    return "active-or-source", "keep", "Current policy requires retention."


def audit(inventory: dict[str, Any], privacy: dict[str, Any], budgets: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, str]] = []
    zone_bytes: Counter[str] = Counter()
    artifact_states: Counter[str] = Counter()
    cohorts: dict[str, dict[str, Any]] = {}
    sidecars = 0
    orphaned = 0
    unverified_rebuildability = 0
    oversized_files = 0
    single_file_budget = int(budgets["budgets"]["generated_single_file"])
    for node in inventory["nodes"]:
        if node["node_type"] == "file":
            zone_bytes[node["zone"]] += int(node["size"])
            if node["kind"] in {"generated", "runtime", "report", "database", "vector"} and int(node["size"]) > single_file_budget:
                oversized_files += 1
        klass = node.get("privacy_class", privacy["default_class"])
        allowed = set(privacy["classes"].get(klass, privacy["classes"]["R4"])["allowed_git_policies"])
        if node["git_policy"] not in allowed:
            violations.append({"code": "privacy_git_policy", "policy_id": node["policy_id"], "privacy_class": klass})
        if klass in PRIVATE and node["content_hash_policy"] not in {"none", "filesystem-metadata"}:
            violations.append({"code": "private_content_hash", "policy_id": node["policy_id"], "privacy_class": klass})
        if str(node["path"]).lower().endswith(SIDECARS):
            sidecars += 1
        if node["status"] == "orphaned":
            orphaned += 1
        if node["node_type"] == "file" and node["format"] in {"db", "sqlite", "sqlite3"} and klass == "R4":
            artifact_states["authoritative_mutable_private_store"] += 1
        elif node["node_type"] == "file" and node["kind"] == "generated":
            if str(node["producer"]).startswith("governance-policy:"):
                artifact_states["derived_rebuildability_unverified"] += 1
                unverified_rebuildability += 1
            else:
                artifact_states["derived_rebuildable"] += 1
        elif node["node_type"] == "file":
            artifact_states["source_or_non_generated"] += 1
        cohort_id, disposition, reason = _cohort(node)
        row = cohorts.setdefault(cohort_id, {
            "cohort": cohort_id,
            "proposed_disposition": disposition,
            "approval_required": True,
            "owner": node["owner_module"],
            "reason": reason,
            "privacy_classes": set(),
            "node_count": 0,
            "bytes": 0,
            "rollback": "No action executed; future action requires manifest journal and original-path restore.",
        })
        row["privacy_classes"].add(klass)
        row["node_count"] += 1
        row["bytes"] += int(node["size"])

    budget_values = budgets["budgets"]
    total = sum(zone_bytes.values())
    budget_status = {
        "repository_total": {"actual": total, "budget": int(budget_values["repository_total"]), "over": total > int(budget_values["repository_total"])},
    }
    for zone in ("archive", "data", "var"):
        actual = zone_bytes.get(zone, 0)
        budget_status[zone] = {"actual": actual, "budget": int(budget_values[zone]), "over": actual > int(budget_values[zone])}
    preview = []
    for item in sorted(cohorts.values(), key=lambda x: x["cohort"]):
        item["privacy_classes"] = sorted(item["privacy_classes"])
        preview.append(item)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "metadata-only",
        "content_opened": False,
        "actions_executed": 0,
        "privacy_violations": violations,
        "sidecar_nodes": sidecars,
        "orphaned_nodes": orphaned,
        "artifact_states": dict(sorted(artifact_states.items())),
        "lineage_findings": {
            "derived_rebuildability_unverified": unverified_rebuildability,
            "oversized_generated_files": oversized_files,
            "interpretation": "Report-only findings; no artifact is moved, rewritten, or deleted.",
        },
        "zone_bytes": dict(sorted(zone_bytes.items())),
        "storage_budgets": budget_status,
        "cohorts": preview,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metadata-only artifact, privacy, and retention audit")
    root_default = Path(__file__).resolve().parents[3]
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--policy", type=Path, default=Path("governance/policies/paths.yaml"))
    parser.add_argument("--privacy", type=Path, default=Path("governance/policies/privacy.yaml"))
    parser.add_argument("--budgets", type=Path, default=Path("governance/baselines/storage_budgets.yaml"))
    parser.add_argument("--output", type=Path, default=Path("integration/runtime/governance/artifact_audit.json"))
    parser.add_argument("--preview", type=Path, default=Path("integration/runtime/governance/archive_disposal_preview.json"))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-content", action="store_true", required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.inventory:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    else:
        inventory = build_inventory(root, root / args.policy)
    result = audit(inventory, _load_yaml(root / args.privacy), _load_yaml(root / args.budgets))
    for target, payload in ((root / args.output, result), (root / args.preview, {k: result[k] for k in ("schema_version", "generated_at", "mode", "content_opened", "actions_executed", "cohorts")})):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "mode": result["mode"],
        "actions_executed": result["actions_executed"],
        "privacy_violations": len(result["privacy_violations"]),
        "over_budget": [name for name, row in result["storage_budgets"].items() if row["over"]],
        "cohorts": len(result["cohorts"]),
    }, sort_keys=True))
    return 1 if args.check and result["privacy_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
