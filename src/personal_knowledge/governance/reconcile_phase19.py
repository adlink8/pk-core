"""Build Phase 19 final inventory and the exhaustive Phase 20 disposition input."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INVENTORY = ROOT / "integration/runtime/governance/phase19_final_inventory.json"
PHASE18_INVENTORY = ROOT / "integration/runtime/governance/final_inventory.json"
DISPOSITIONS = ROOT / "governance/manifests/phase20_pending.json"
TREE = ROOT / "governance/baselines/phase19_before_after_tree.json"
ACTIVE_SNAPSHOT = ROOT / "governance/baselines/phase19_active_data_snapshot.json"
VERIFICATION = ROOT / ".planning/phases/19-physical-source-consolidation/19-VERIFICATION.md"
SUMMARY = ROOT / ".planning/phases/19-physical-source-consolidation/19-05-SUMMARY.md"

PHASE20_PREFIXES = (
    ".migration-backup",
    ".migration-backup-recovery",
    ".ai-bridge",
    ".gsd",
    ".pytest_cache",
    "Agent",
    "Google",
    "imports",
    "_recycle",
    "logs",
    "archive",
    "data",
    "var",
    "integration/analysis",
    "integration/db",
    "integration/evals",
    "integration/raw_index",
    "integration/runtime",
    "integration/structured",
)
RETAINED_PREFIXES = (
    ".agents",
    ".codex",
    ".github",
    ".planning",
    ".workbuddy",
    "apps",
    "assets",
    "docs",
    "governance",
    "src",
    "tests",
    "tools",
    "integration/scripts",
    "integration/README.md",
)
APPROVED_ROOT_CONFIG = {
    ".gitignore",
    "constraints.txt",
    "pyproject.toml",
    "pytest.ini",
    "requirements-dev.txt",
    "requirements-optional.txt",
    "requirements.txt",
}
PROTECTED_POLICIES = {
    "private-runtime",
    "private-agent",
    "private-google-imports",
    "private-analysis",
    "quarantine",
}
PHASE19_RUNTIME_EXCLUSIONS = (
    "integration/runtime/governance",
    "integration/runtime/migration",
)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def disposition(path: str) -> str:
    if path in APPROVED_ROOT_CONFIG:
        return "approved-root-config"
    if path == "README.md" or _under(path, RETAINED_PREFIXES):
        return "retained-tooling"
    if path == "个人数据系统-结构与流程.html" or _under(path, PHASE20_PREFIXES) or path == "integration":
        return "phase20-pending"
    if path.startswith("integration/"):
        return "phase20-pending"
    raise ValueError(f"unknown residual path: {path}")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _protected_state(nodes: list[dict]) -> dict[str, tuple]:
    state = {}
    for node in nodes:
        path = node["path"]
        if node["policy_id"] not in PROTECTED_POLICIES or _under(path, PHASE19_RUNTIME_EXCLUSIONS):
            continue
        state[path] = (node["node_type"], node["size"], node["mtime"])
    return state


def _source_tree() -> dict:
    manifests = sorted((ROOT / "governance/manifests/source").glob("*.json"))
    before = Counter()
    after = Counter()
    operations = 0
    for path in manifests:
        payload = _load(path)
        candidates = payload.get("moves")
        if candidates is None:
            candidates = [
                entry for entry in payload.get("entries", [])
                if "source" in entry and "target" in entry
            ]
        for operation in candidates:
            source = operation["source"]
            target = operation["target"]
            before[source.split("/", 1)[0]] += 1
            after[target.split("/", 1)[0]] += 1
            operations += 1
    return {
        "schema_version": "1.0",
        "source": "signed Phase 19 historical and consolidated recovery manifests",
        "operations": operations,
        "before_top_level_file_counts": dict(sorted(before.items())),
        "after_top_level_file_counts": dict(sorted(after.items())),
        "final_roots": sorted(p.name for p in ROOT.iterdir() if p.name != ".git"),
    }


def main() -> int:
    # Establish all final artifact paths before the authoritative inventory scan.
    for path in (INVENTORY, DISPOSITIONS, TREE, ACTIVE_SNAPSHOT, VERIFICATION, SUMMARY):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("{}\n" if path.suffix == ".json" else "# Pending\n", encoding="utf-8")

    from integration.scripts.governance.build_project_inventory import build_inventory

    inventory = build_inventory(ROOT, ROOT / "governance/policies/paths.yaml")
    INVENTORY.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    counts = Counter()
    for node in inventory["nodes"]:
        if node["path"] == ".git":
            continue
        value = disposition(node["path"])
        counts[value] += 1
        rows.append({"path": node["path"], "node_type": node["node_type"], "disposition": value})
    payload = {
        "schema_version": "1.0",
        "privacy": "R4-local-metadata; never publish without redaction",
        "source_inventory": INVENTORY.relative_to(ROOT).as_posix(),
        "node_count": len(rows),
        "coverage_percent": 100.0,
        "unknown": 0,
        "conflict": 0,
        "counts": dict(sorted(counts.items())),
        "entries": rows,
    }
    DISPOSITIONS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    TREE.write_text(json.dumps(_source_tree(), ensure_ascii=False, indent=2), encoding="utf-8")

    previous = _load(PHASE18_INVENTORY)
    before = _protected_state(previous["nodes"])
    after = _protected_state(inventory["nodes"])
    common_mismatch = sorted(path for path in before.keys() & after.keys() if before[path] != after[path])
    missing = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    pointer = ROOT / "integration/db/knowledge_index_active.txt"
    active = {
        "path": pointer.relative_to(ROOT).as_posix(),
        "sha256": _sha256(pointer),
        "value": pointer.read_text(encoding="utf-8").strip() if pointer.exists() else None,
    }
    snapshot = {
        "schema_version": "1.0",
        "comparison": "Phase 18 final inventory vs Phase 19 final inventory; size+mtime, no private content read",
        "protected_before": len(before),
        "protected_after": len(after),
        "missing": missing,
        "added": added,
        "mismatch": common_mismatch,
        "active_pointer": active,
    }
    ACTIVE_SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"inventory": inventory["summary"], "dispositions": payload["counts"], "protected": {"missing": len(missing), "added": len(added), "mismatch": len(common_mismatch)}, "active": active}, ensure_ascii=False))
    # Phase 18 is the architectural baseline, not the start-of-19-05 byte guard.
    # Historical mutable-store drift is recorded for Phase 20; final tests use a
    # separate start/end SHA-256 guard for authoritative stores and pointers.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
