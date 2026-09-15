"""Classify machine-specific path literals and fail on production debt."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "governance" / "baselines" / "path_hits.yaml"
SCAN_ROOTS = ("integration/scripts", "apps", "tests", "docs", "README.md")
SKIP_PARTS = {".git", "__pycache__", "node_modules", "runtime", "db"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".ps1", ".sh", ".txt"}
MACHINE_PATH = re.compile(r"(?i)(?:[a-z]:[\\/](?:users|models|program files)[\\/]|[a-z]:[\\/][^\s'\"`]+desktop[\\/])")
SYS_PATH_LITERAL = re.compile(r"sys\.path\.(?:insert|append)\([^\n]*(?:['\"](?:integration/scripts|[a-z]:[\\/]))", re.I)


def classify(path: Path, line: str) -> str:
    rel = path.relative_to(ROOT).as_posix()
    if rel.startswith("tests/"):
        return "test_fixture"
    if "/_tools/" in rel:
        return "migration_tool"
    if rel.startswith("integration/analysis/") or rel.startswith("_recycle/"):
        return "historical_report"
    if path.suffix.lower() == ".md" or rel.startswith("apps/"):
        return "documentation_template"
    if "<user>" in line.lower() or ".agentsview" in line.lower():
        return "private_source_locator"
    return "production_source"


def scan() -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for root_name in SCAN_ROOTS:
        root = ROOT / root_name
        paths = [root] if root.is_file() else root.rglob("*") if root.exists() else []
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            rel_parts = path.relative_to(ROOT).parts
            if any(part in SKIP_PARTS for part in rel_parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                kinds = []
                if MACHINE_PATH.search(line):
                    kinds.append("machine_path")
                if SYS_PATH_LITERAL.search(line):
                    kinds.append("sys_path_literal")
                if kinds:
                    hits.append({"path": path.relative_to(ROOT).as_posix(), "line": number,
                                 "kinds": kinds, "category": classify(path, line)})
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    policy = json.loads(BASELINE.read_text(encoding="utf-8"))
    hits = scan()
    violations = [hit for hit in hits if hit["category"] == "production_source"]
    counts = {name: sum(h["category"] == name for h in hits) for name in policy["categories"]}
    result = {"ok": not violations, "counts": counts, "violations": violations, "classified_hits": len(hits)}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
          f"path-policy {'PASS' if result['ok'] else 'FAIL'}: {len(hits)} classified, {len(violations)} production violations")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
