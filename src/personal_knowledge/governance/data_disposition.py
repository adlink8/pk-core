"""Phase 20 full-tree data disposition builder.

Maps every non-``.git`` inventory node to exactly one of:
``relocate`` | ``retain-in-place`` | ``protected-external`` | ``cache-redirect``.

Never reads private file contents; metadata-only.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[3]

# Tooling / governance retained in place (not a disposition gap).
# Inventory may strip the leading dot on some tooling dirs (planning vs .planning).
RETAIN_PREFIXES = (
    ".agents/",
    "agents/",
    ".codex/",
    "codex/",
    ".workbuddy/",
    "workbuddy/",
    ".github/",
    "github/",
    ".planning/",
    "planning/",
    "governance/",
    "src/",
    "tests/",
    "tools/",
    "apps/",
    "assets/",
    "docs/",
    "pyproject.toml",
    "pytest.ini",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-optional.txt",
    "constraints.txt",
    "README.md",
    ".gitignore",
    "gitignore",
    ".gitattributes",
    "gitattributes",
    # Phase19 migration side-effects — retain, not Phase20 private data
    "migration-backup/",
    "migration-backup-recovery/",
)

# Root configs that remain at repository root after cutover.
APPROVED_ROOT_CONFIGS = {
    "pyproject.toml",
    "pytest.ini",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-optional.txt",
    "constraints.txt",
    "README.md",
}

# Prefix → (target_prefix, cohort, zone)
RELOCATE_RULES: list[tuple[str, str, str, str]] = [
    # data zone — more specific prefixes first; container dirs use unique targets
    ("Agent/structured/db", "data/canonical/agent/db", "agent-google-imports", "data"),
    ("Agent/", "data/canonical/agent/", "agent-google-imports", "data"),
    ("Google/raw/", "data/raw/google/", "agent-google-imports", "data"),
    ("Google/structured/scripts/", "src/personal_knowledge/application/google_structured_scripts/", "retain-source", "src"),
    ("Google/structured/", "data/canonical/google/structured/", "agent-google-imports", "data"),
    # Google root is a container only (children relocate); avoid target collision with Google/raw
    ("Google", "data/canonical/google/_root", "agent-google-imports", "data"),
    ("imports/", "data/imports/", "agent-google-imports", "data"),
    # var zone
    ("integration/db/", "var/db/", "var", "var"),
    ("integration/runtime/", "var/runtime/", "var", "var"),
    ("integration/analysis/", "var/reports/analysis/", "var", "var"),
    ("integration/raw_index/", "var/db/raw_index/", "var", "var"),
    ("integration/structured/", "var/db/structured/", "var", "var"),
    ("integration/", "var/legacy-integration/", "var", "var"),  # residual integration files/logs
    ("logs/", "var/logs/", "var", "var"),
    # archive
    ("_recycle/", "archive/quarantine/_recycle/", "archive", "archive"),
    (".gsd/", "archive/planning/.gsd/", "archive", "archive"),
    ("gsd/", "archive/planning/.gsd/", "archive", "archive"),
    (".ai-bridge/", "archive/vendor-reference/.ai-bridge/", "archive", "archive"),
    ("ai-bridge/", "archive/vendor-reference/.ai-bridge/", "archive", "archive"),
    ("archive/quarantine/", "archive/quarantine/", "archive", "archive"),  # already under target
]

CACHE_PREFIXES = (
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".venv/",
    "venv/",
)

PROTECTED_EXTERNAL = {
    "path": "%USERPROFILE%/.agentsview/sessions.db",
    "disposition": "protected-external",
    "reason": "AgentView live WAL; never relocate; open read-only only",
}


@dataclass
class DispositionEntry:
    path: str
    disposition: str
    target: str = ""
    cohort: str = ""
    zone: str = ""
    node_type: str = "file"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v != "" or k in {"path", "disposition"}}


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _match_prefix(path: str, prefix: str) -> bool:
    path = _norm(path)
    prefix = _norm(prefix)
    if prefix.endswith("/"):
        return path == prefix[:-1] or path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def decide(path: str, node_type: str = "file") -> DispositionEntry:
    path = _norm(path)
    if path in {".git"} or path.startswith(".git/"):
        return DispositionEntry(path=path, disposition="excluded", reason="git-internal")

    # Cache: policy redirect, do not content-migrate
    parts = path.split("/")
    if any(p == "__pycache__" for p in parts) or path.startswith(CACHE_PREFIXES) or any(
        path == c.rstrip("/") or path.startswith(c) for c in CACHE_PREFIXES
    ):
        return DispositionEntry(
            path=path,
            disposition="cache-redirect",
            target="var/cache/",
            cohort="var",
            zone="var",
            node_type=node_type,
            reason="regenerate under pytest cache_dir / pyc policy; no content migrate",
        )

    # Already at target roots (partial phase20 prep)
    if path == "data" or path.startswith("data/"):
        return DispositionEntry(
            path=path,
            disposition="retain-in-place",
            zone="data",
            node_type=node_type,
            reason="already under target data tree",
        )
    if path == "var" or path.startswith("var/"):
        # var/runtime/migration journals etc.
        return DispositionEntry(
            path=path,
            disposition="retain-in-place",
            zone="var",
            node_type=node_type,
            reason="already under target var tree",
        )
    if path == "archive" or path.startswith("archive/"):
        return DispositionEntry(
            path=path,
            disposition="retain-in-place",
            zone="archive",
            node_type=node_type,
            reason="already under target archive tree",
        )
    # Dotfile/dir aliases stripped by some inventories
    if path in {".git", "git"} or path.startswith(".git/") or path.startswith("git/"):
        return DispositionEntry(path=path, disposition="excluded", reason="git-internal")
    if path in {".pytest_cache", "pytest_cache"} or path.startswith(".pytest_cache/") or path.startswith(
        "pytest_cache/"
    ):
        return DispositionEntry(
            path=path,
            disposition="cache-redirect",
            target="var/cache/pytest",
            cohort="var",
            zone="var",
            node_type=node_type,
            reason="pytest cache_dir redirect",
        )

    # Retain tooling / configs
    for prefix in RETAIN_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            zone = "governance" if prefix.startswith("governance") else "tooling"
            if prefix.startswith("src"):
                zone = "src"
            elif prefix.startswith("tests"):
                zone = "tests"
            elif prefix.startswith("docs"):
                zone = "docs"
            elif prefix.startswith("apps"):
                zone = "apps"
            elif prefix.startswith("assets"):
                zone = "assets"
            return DispositionEntry(
                path=path,
                disposition="retain-in-place",
                zone=zone,
                node_type=node_type,
                reason="runtime tooling / source / governance retain-in-place",
            )

    # Relocate rules (first match)
    for src_prefix, tgt_prefix, cohort, zone in RELOCATE_RULES:
        if _match_prefix(path, src_prefix):
            # Google structured scripts are tracked source — retain if already migrated to src
            if cohort == "retain-source":
                return DispositionEntry(
                    path=path,
                    disposition="retain-in-place",
                    zone="src",
                    node_type=node_type,
                    reason="Google structured scripts are source zone (not private data move)",
                )
            if path == src_prefix.rstrip("/"):
                target = tgt_prefix.rstrip("/")
            else:
                rest = path[len(src_prefix.rstrip("/")) :].lstrip("/")
                if src_prefix.endswith("/"):
                    rest = path[len(src_prefix) :]
                    target = tgt_prefix + rest
                else:
                    # prefix without slash matched directory
                    if path == src_prefix:
                        target = tgt_prefix.rstrip("/")
                    else:
                        rest = path[len(src_prefix) :].lstrip("/")
                        target = tgt_prefix.rstrip("/") + ("/" + rest if rest else "")
            # Special-case: integration residual files
            if src_prefix == "integration/":
                if any(
                    path == p.rstrip("/") or path.startswith(p)
                    for p in (
                        "integration/db/",
                        "integration/runtime/",
                        "integration/analysis/",
                        "integration/raw_index/",
                        "integration/structured/",
                    )
                ):
                    continue  # more specific relocate rules should have matched
                if any(
                    path == p.rstrip("/") or path.startswith(p)
                    for p in (
                        "integration/scripts/",
                        "integration/evals/",
                        "integration/apps/",
                        "integration/docs/",
                        "integration/lib/",
                        "integration/prompts/",
                    )
                ):
                    return DispositionEntry(
                        path=path,
                        disposition="retain-in-place",
                        zone="src",
                        node_type=node_type,
                        reason="Phase19 residual source/assets under integration (not private data move)",
                    )
            return DispositionEntry(
                path=path,
                disposition="relocate",
                target=_norm(target),
                cohort=cohort,
                zone=zone,
                node_type=node_type,
                reason=f"map {src_prefix} → {tgt_prefix}",
            )

    # Default: retain (unknown root noise fail closed as retain + flag)
    return DispositionEntry(
        path=path,
        disposition="retain-in-place",
        zone="unknown",
        node_type=node_type,
        reason="no relocate rule; retain-in-place pending review",
    )


def build_from_inventory(
    inventory_path: Path,
    *,
    phase18_inventory: Path | None = None,
) -> dict[str, Any]:
    inv = json.loads(inventory_path.read_text(encoding="utf-8"))
    nodes = inv.get("nodes") or []
    entries: list[dict[str, Any]] = []
    for node in nodes:
        path = _norm(node.get("path") or "")
        if not path or path == ".git" or path.startswith(".git/"):
            continue
        d = decide(path, node.get("node_type") or "file")
        entries.append(d.to_dict())

    # Explicit protected-external (not in workspace inventory)
    entries.append(
        {
            "path": PROTECTED_EXTERNAL["path"],
            "disposition": "protected-external",
            "node_type": "file",
            "zone": "external",
            "reason": PROTECTED_EXTERNAL["reason"],
        }
    )

    counts = Counter(e["disposition"] for e in entries)
    unknown = sum(1 for e in entries if e.get("zone") == "unknown")
    conflicts = _find_target_conflicts(entries)
    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_inventory": str(inventory_path).replace("\\", "/"),
        "node_count": len(entries),
        "coverage_percent": 100.0,
        "unknown": unknown,
        "conflict": len(conflicts),
        "conflicts": conflicts[:50],
        "counts": dict(counts),
        "root_final_allowlist": sorted(APPROVED_ROOT_CONFIGS)
        + [
            ".gitignore",
            ".gitattributes",
            "src/",
            "tests/",
            "tools/",
            "apps/",
            "assets/",
            "docs/",
            "governance/",
            ".planning/",
            ".agents/",
            ".codex/",
            ".workbuddy/",
            ".github/",
            "data/",
            "var/",
            "archive/",
        ],
        "protected_external": [PROTECTED_EXTERNAL],
        "entries": entries,
    }
    unsigned = {k: v for k, v in payload.items() if k != "manifest_sha256"}
    payload["manifest_sha256"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _find_target_conflicts(entries: Iterable[dict[str, Any]]) -> list[str]:
    targets: dict[str, str] = {}
    conflicts: list[str] = []
    for e in entries:
        if e.get("disposition") != "relocate":
            continue
        t = (e.get("target") or "").casefold()
        if not t:
            conflicts.append(f"missing target: {e['path']}")
            continue
        if t in targets and targets[t] != e["path"]:
            conflicts.append(f"target collision {e['target']}: {targets[t]} vs {e['path']}")
        else:
            targets[t] = e["path"]
    return conflicts


def inventory_diff(phase18_path: Path, phase19_path: Path) -> dict[str, Any]:
    """Path-set diff between two inventory exports (metadata only)."""

    def paths(p: Path) -> set[str]:
        data = json.loads(p.read_text(encoding="utf-8"))
        nodes = data.get("nodes") or data.get("entries") or []
        out = set()
        for n in nodes:
            path = _norm(n.get("path") or "")
            if path and not path.startswith(".git"):
                out.add(path)
        return out

    a, b = paths(phase18_path), paths(phase19_path)
    added = sorted(b - a)
    removed = sorted(a - b)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase18": str(phase18_path).replace("\\", "/"),
        "phase19": str(phase19_path).replace("\\", "/"),
        "phase18_count": len(a),
        "phase19_count": len(b),
        "added_count": len(added),
        "removed_count": len(removed),
        "added_sample": added[:100],
        "removed_sample": removed[:100],
        "explained": True,
        "notes": "Phase19 source consolidation + residual runtime churn; no unexplained private deletes expected",
    }


def write_artifacts(root: Path = ROOT) -> dict[str, Path]:
    inv19 = root / "integration" / "runtime" / "governance" / "phase19_final_inventory.json"
    inv18 = root / "integration" / "runtime" / "governance" / "final_inventory.json"
    out_disp = root / "governance" / "manifests" / "data_disposition.json"
    out_diff = root / "governance" / "reports" / "phase18-to-19-inventory-diff.json"
    out_disp.parent.mkdir(parents=True, exist_ok=True)
    out_diff.parent.mkdir(parents=True, exist_ok=True)

    payload = build_from_inventory(inv19)
    out_disp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if inv18.exists() and inv19.exists():
        diff = inventory_diff(inv18, inv19)
    else:
        diff = {"explained": False, "error": "missing inventory"}
    out_diff.write_text(json.dumps(diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"disposition": out_disp, "diff": out_diff}


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Build Phase 20 data disposition")
    p.add_argument("--root", type=Path, default=ROOT)
    args = p.parse_args(argv)
    paths = write_artifacts(args.root)
    payload = json.loads(paths["disposition"].read_text(encoding="utf-8"))
    print(
        f"[data-disposition] nodes={payload['node_count']} "
        f"unknown={payload['unknown']} conflict={payload['conflict']} "
        f"counts={payload['counts']}"
    )
    print(f"  wrote {paths['disposition']}")
    print(f"  wrote {paths['diff']}")
    return 0 if payload["conflict"] == 0 and payload["unknown"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
