"""Build the exact tracked-source relocation manifest without mutating the tree."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import warnings
from pathlib import Path, PurePosixPath

PRIVATE_PREFIXES = ("Agent/", "Google/", "imports/", "integration/db/", "integration/runtime/", "integration/analysis/", "_recycle/")
DOMAIN_MAP = {
    "core": "core", "conversation": "domains/conversation", "knowledge": "domains/knowledge",
    "memory": "domains/memory", "graph": "domains/graph", "vector": "retrieval",
    "pipeline": "application", "services": "services", "evaluation": "evaluation",
    "source_adapters": "adapters", "governance": "governance",
}
IMPORT_MAP = {
    "core": "personal_knowledge.core",
    "conversation": "personal_knowledge.domains.conversation",
    "knowledge": "personal_knowledge.domains.knowledge",
    "memory": "personal_knowledge.domains.memory",
    "graph": "personal_knowledge.domains.graph",
    "vector": "personal_knowledge.retrieval",
    "pipeline": "personal_knowledge.application",
    "services": "personal_knowledge.services",
    "source_adapters": "personal_knowledge.adapters",
    "governance": "personal_knowledge.governance",
    "evaluation": "personal_knowledge.evaluation",
}
BARE_IMPORT_MAP = {
    "rules": "personal_knowledge.core.rules",
    "common": "personal_knowledge.core.common",
    "project_paths": "personal_knowledge.core.project_paths",
    "local_embed": "personal_knowledge.core.local_embed",
    "chroma_client": "personal_knowledge.core.chroma_client",
    "conversation_repository": "personal_knowledge.core.conversation_repository",
    "memory_governance": "personal_knowledge.core.memory_governance",
    "unified_search": "personal_knowledge.retrieval.unified_search",
}

PRIVATE_EVAL_SUFFIX = ".private.jsonl"


def _test_bucket(name: str) -> str:
    """Assign every test module to one stable, mutually exclusive test layer."""
    stem = Path(name).stem
    if stem.startswith("test_governance_") or stem == "test_physical_source_layout":
        return "governance"
    if any(token in stem for token in ("contract", "distribution", "data_access", "apps_sdk", "mcp_server")):
        return "contract"
    if stem in {"test_dashboard_smoke", "test_run_pipeline_contracts"}:
        return "e2e"
    if any(token in stem for token in (
        "normalization", "source_adapter", "import_pipeline", "google_light",
        "checkpoint", "rollback", "incremental", "index_", "knowledge_eval_",
    )):
        return "integration"
    return "unit"


def _layout_target(source: str) -> tuple[str, str]:
    path = PurePosixPath(source)
    if source.startswith("apps/"):
        return str(PurePosixPath("apps") / path.relative_to("apps")), "application-source"
    if source.startswith("assets/prompts/"):
        return str(PurePosixPath("assets/prompts") / path.relative_to("assets/prompts")), "prompt-contract"
    if source.startswith("integration/evals/"):
        return str(PurePosixPath("assets/evals") / path.relative_to("integration/evals")), "eval-contract"
    if source.startswith("assets/vendor/"):
        return str(PurePosixPath("assets/vendor") / path.relative_to("assets/vendor")), "vendor-runtime-dependency"
    if source == "docs/architecture/retrieval-ssot.md":
        return "docs/architecture/retrieval-ssot.md", "architecture-doc"
    if source == "docs/legacy/retrieval-ssot.duplicate.md":
        return "docs/legacy/retrieval-ssot.duplicate.md", "duplicate-doc-preserved"
    if source == "docs/README.md":
        return "docs/README.md", "documentation-index"
    if source == "docs/runbooks/dependency-governance.md":
        return "docs/runbooks/dependency-governance.md", "runbook"
    if source.startswith("integration/scripts/") and path.name == "README.md":
        domain = path.parts[2]
        if domain in DOMAIN_MAP and domain != "governance":
            return f"src/personal_knowledge/{DOMAIN_MAP[domain]}/README.md", "module-contract"
    if source.startswith("tests/") and path.suffix == ".py":
        return f"tests/{_test_bucket(path.name)}/{path.name}", f"test-{_test_bucket(path.name)}"
    raise ValueError(f"unclassified app/asset/doc/test path: {source}")


def build_apps_assets_docs_tests(root: Path) -> tuple[dict, dict]:
    """Build the approved non-data layout cohort and its explicit classification."""
    roots = (
        root / "apps", root / "assets/prompts", root / "integration/evals",
        root / "assets/vendor", root / "docs", root / "tests",
    )
    sources: list[Path] = []
    retained: list[dict] = []
    for folder in roots:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            if rel == "tests/README.md":
                retained.append({
                    "source": rel,
                    "target": rel,
                    "type": "test-documentation",
                    "privacy": "R1",
                    "disposition": "retain",
                    "reason": "The test root README is the required module contract for docs coverage.",
                })
                continue
            if rel.endswith(PRIVATE_EVAL_SUFFIX):
                retained.append({
                    "source": rel,
                    "target": None,
                    "type": "private-eval-data",
                    "privacy": "R3",
                    "disposition": "phase20-pending",
                    "reason": "Private evaluation data is outside the Phase 19 tracked-source boundary.",
                })
                continue
            sources.append(path)
    dependency_doc = root / "docs/runbooks/dependency-governance.md"
    if dependency_doc.is_file():
        sources.append(dependency_doc)
    for domain in DOMAIN_MAP:
        if domain == "governance":
            continue
        module_readme = root / "integration/scripts" / domain / "README.md"
        if module_readme.is_file():
            sources.append(module_readme)

    tracked = set(_git(root, "ls-files"))
    entries: list[dict] = []
    classifications: list[dict] = []
    path_map: dict[str, str] = {}
    for path in sorted(sources):
        source = path.relative_to(root).as_posix()
        target, kind = _layout_target(source)
        path_map[source] = target
        entry = _approved_entry(root, source, target)
        entry["text_source"] = True
        entries.append(entry)
        classifications.append({
            "source": source,
            "target": target,
            "type": kind,
            "privacy": "R1" if kind not in {"eval-contract"} else "R2",
            "disposition": "relocate",
            "tracked": source in tracked,
        })

    candidates = set(_git(root, "ls-files")) | set(path_map)
    for folder in (root / "src", root / "tools", root / "governance", root / "docs", root / "integration/scripts"):
        if folder.exists():
            candidates.update(path.relative_to(root).as_posix() for path in folder.rglob("*") if path.is_file())
    rewrites: list[dict] = []
    prefix_map = {
        "apps": "apps",
        "apps/personal_data_chatgpt": "apps/personal_data_chatgpt",
        "assets/prompts": "assets/prompts",
        "assets/vendor": "assets/vendor",
        "docs": "docs",
    }
    prefix_map.update({
        f"integration/scripts/{domain}": f"src/personal_knowledge/{target}"
        for domain, target in DOMAIN_MAP.items()
        if domain != "governance"
    })
    excluded = PRIVATE_PREFIXES + (".planning/", "archive/", ".migration-backup/", "var/", "governance/manifests/")
    for rel in sorted(candidates):
        normalized_rel = rel.replace("\\", "/")
        if normalized_rel.startswith(excluded) or normalized_rel.endswith(PRIVATE_EVAL_SUFFIX):
            continue
        path = root / rel
        before = _read_searchable(path)
        if before is None:
            continue
        after = before
        replacements: list[dict] = []
        for old, new in {**path_map, **prefix_map}.items():
            windows_target = new if old.startswith("tests/") else new.replace("/", "\\")
            for old_value, new_value in ((old, new), (old.replace("/", "\\"), windows_target)):
                if old_value in after:
                    count = after.count(old_value)
                    after = after.replace(old_value, new_value)
                    replacements.append({"old": old_value, "new": new_value, "count": count})
        for old, new in (
            ('/ "assets" / "prompts"', '/ "assets" / "prompts"'),
            ('Path(__file__).resolve().parents[4] / "assets" / "prompts"', 'Path(__file__).resolve().parents[4] / "assets" / "prompts"'),
        ):
            if old in after:
                count = after.count(old)
                after = after.replace(old, new)
                replacements.append({"old": old, "new": new, "count": count})
        if normalized_rel != "src/personal_knowledge/evaluation/build_private_suite.py":
            changed, count = re.subn(
                r'/ "integration"(\s*\n\s*|\s*)/ "evals"',
                r'/ "assets"\g<1>/ "evals"',
                after,
            )
            if count:
                after = changed
                replacements.append({"old": "Path components integration/evals", "new": "assets/evals", "count": count})
        if normalized_rel.startswith("tests/") and normalized_rel.endswith(".py"):
            if normalized_rel == "tests/unit/test_knowledge_unit_eval_dataset.py":
                after = after.replace("_ROOT = _THIS_DIR.parent", "_ROOT = _THIS_DIR.parents[1]")
                after = after.replace(
                    'EVAL_DIR = _ROOT / "assets" / "evals" / "knowledge_units"',
                    'PUBLIC_EVAL_DIR = _ROOT / "assets" / "evals" / "knowledge_units"\n'
                    'PRIVATE_EVAL_DIR = _ROOT / "assets" / "evals" / "knowledge_units"',
                )
                for filename in ("synthetic_cases.jsonl", "README.md"):
                    after = after.replace(f'EVAL_DIR / "{filename}"', f'PUBLIC_EVAL_DIR / "{filename}"')
                for filename in (
                    "dev_queries.private.jsonl", "frozen_test_queries.private.jsonl",
                    "merge_positive_pairs.private.jsonl", "hard_negative_pairs.private.jsonl",
                ):
                    after = after.replace(f'EVAL_DIR / "{filename}"', f'PRIVATE_EVAL_DIR / "{filename}"')
            for old, new in (
                ("Path(__file__).resolve().parents[1]", "Path(__file__).resolve().parents[2]"),
                ("Path(__file__).resolve().parent.parent", "Path(__file__).resolve().parents[2]"),
            ):
                if old in after:
                    count = after.count(old)
                    after = after.replace(old, new)
                    replacements.append({"old": old, "new": new, "count": count})
        if normalized_rel == "governance/policies/architecture.yaml":
            after = re.sub(
                r'(?m)^  src: \{paths: \[[^\]]*\],',
                '  src: {paths: ["src", "apps", "integration/scripts"],',
                after,
            )
            after = re.sub(
                r'(?m)^  assets: \{paths: \[[^\]]*\],',
                '  assets: {paths: ["assets"],',
                after,
            )
            after = re.sub(
                r'(?m)^  docs: \{paths: \[[^\]]*\],',
                '  docs: {paths: ["README.md", "docs"],',
                after,
            )
            after = re.sub(
                r'(?m)^  data: \{paths: \[[^\]]*\],',
                '  data: {paths: ["Agent", "Google", "imports", "integration/evals"],',
                after,
            )
        if normalized_rel == "governance/stable_modules.yaml":
            after = after.replace("{path: integration/evals,", "{path: assets/evals,")
        if after == before:
            continue
        before_bytes = path.read_bytes()
        after_bytes = after.encode("utf-8")
        rewrites.append({
            "path": normalized_rel,
            "effective_path_after_move": path_map.get(normalized_rel, normalized_rel),
            "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
            "before_size": len(before_bytes),
            "before_b64": base64.b64encode(before_bytes).decode("ascii"),
            "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
            "after_text": after,
            "replacements": replacements,
        })

    classification = {
        "schema_version": 1,
        "cohort": "apps-assets-docs-tests",
        "entries": sorted(classifications + retained, key=lambda item: item["source"]),
        "summary": {
            "relocate": len(classifications),
            "phase20_pending_private": sum(1 for item in retained if item["disposition"] == "phase20-pending"),
            "retain": sum(1 for item in retained if item["disposition"] == "retain"),
            "ambiguous": 0,
        },
    }
    classification = _sign(classification)
    payload = {
        "schema_version": 2,
        "cohort": "apps-assets-docs-tests",
        "scope": "tracked-text-source-only",
        "require_git_tracked": True,
        "entries": entries,
        "consumer_rewrites": rewrites,
        "asset_classification_sha256": classification["manifest_sha256"],
        "preflight_snapshot": {
            "operation_count": len(entries),
            "rewrite_count": len(rewrites),
            "source_bytes": sum((root / item["source"]).stat().st_size for item in entries),
            "existing_targets": [item["target"] for item in entries if (root / item["target"]).exists()],
            "casefold_target_collisions": len(entries) - len({item["target"].casefold() for item in entries}),
            "private_eval_retained": [item["source"] for item in retained if item["disposition"] == "phase20-pending"],
            "test_buckets": {bucket: sum(1 for item in entries if item["target"].startswith(f"tests/{bucket}/")) for bucket in ("unit", "contract", "integration", "e2e", "governance")},
        },
        "approval": {
            "status": "approved-current-bytes",
            "authorization": "user approved preserve-and-govern and directly complete all Phase 19 tasks",
        },
    }
    return _sign(payload), classification


def _sign(payload: dict) -> dict:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _root_shim_target(path: Path) -> str:
    return f"tools/compat/v1_1/{path.name}"


def _tool_target(source: str) -> str:
    rel = PurePosixPath(source).relative_to("integration/scripts")
    if rel.name.lower() == "readme.md":
        return f"docs/runbooks/tooling/{rel.parts[0].lstrip('_')}.md"
    if rel.parts[0] == "examples":
        return str(PurePosixPath("tools/forensics/examples") / PurePosixPath(*rel.parts[1:]))
    name = rel.name.lower()
    if "fix" in name or "migrat" in name:
        bucket = "migrations"
    elif name.startswith("_") or name.startswith("phase") or "audit" in name or "probe" in name:
        bucket = "forensics"
    else:
        bucket = "supported"
    return str(PurePosixPath("tools") / bucket / PurePosixPath(*rel.parts[1:]))


def _approved_entry(root: Path, source: str, target: str) -> dict:
    content = (root / source).read_bytes()
    return {
        "source": source,
        "target": target,
        "inverse": {"source": target, "target": source},
        "sha256": hashlib.sha256(content).hexdigest(),
        "consumers": [],
        "dirty": True,
        "tracked": source in set(_git(root, "ls-files")),
        "authorized_untracked": True,
        "approved_prestate": True,
    }


def _shim_module(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'import_module\(["\']([^"\']+)["\']\)', text)
    if not match:
        match = re.search(r"(?m)^from\s+(personal_knowledge(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s+import", text)
    if not match:
        raise ValueError(f"root script is not a compatibility shim: {path}")
    return match.group(1)


def _consumer_rewrites(root: Path, module_map: dict[str, str]) -> list[dict]:
    candidates: set[str] = set(_git(root, "ls-files"))
    for base in (root / "tests", root / "docs", root / "src"):
        if base.exists():
            candidates.update(path.relative_to(root).as_posix() for path in base.rglob("*") if path.is_file())
    rewrites = []
    for rel in sorted(candidates):
        if rel.startswith(PRIVATE_PREFIXES) or rel.startswith((".planning/", "archive/", ".migration-backup/")):
            continue
        path = root / rel
        before = _read_searchable(path)
        if before is None or path.suffix != ".py":
            continue
        after = before
        replacements = []
        for old, new in module_map.items():
            patterns = (
                (rf"(?m)^(\s*from\s+){re.escape(old)}(?=\.|\s+import\b)", rf"\g<1>{new}", "from"),
                (rf"(?m)^(\s*import\s+){re.escape(old)}(?=\s+as\s+)", rf"\g<1>{new}", "import-alias"),
                (rf"(?m)^(\s*)import\s+{re.escape(old)}(?=\s*(?:#.*)?$)", rf"\g<1>import {new} as {old}", "import"),
            )
            for pattern, replacement, kind in patterns:
                changed, count = re.subn(pattern, replacement, after)
                if count:
                    after = changed
                    replacements.append({"old_module": old, "new_module": new, "kind": kind, "count": count})
        if after != before:
            before_bytes = path.read_bytes()
            after_bytes = after.encode("utf-8")
            rewrites.append({
                "path": rel,
                "effective_path_after_move": rel,
                "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
                "before_size": len(before_bytes),
                "before_b64": base64.b64encode(before_bytes).decode("ascii"),
                "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
                "after_text": after,
                "replacements": replacements,
            })
    return rewrites


def build_root_shims(root: Path) -> dict:
    paths = sorted(path for path in (root / "integration/scripts").glob("*.py") if path.is_file())
    module_map = {path.stem: _shim_module(path) for path in paths}
    entries = [_approved_entry(root, path.relative_to(root).as_posix(), _root_shim_target(path)) for path in paths]
    payload = {
        "schema_version": 2,
        "cohort": "root-shims",
        "scope": "tracked-text-source-only",
        "require_git_tracked": True,
        "entries": entries,
        "consumer_rewrites": _consumer_rewrites(root, module_map),
        "approval": {"status": "approved-by-user", "authorization": "preserve and govern; complete all tasks"},
    }
    return _sign(payload)


def build_tools(root: Path) -> dict:
    paths = []
    for folder in (root / "integration/scripts/_tools", root / "integration/scripts/examples"):
        paths.extend(path for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() in {".py", ".md", ".txt", ".json", ".yaml", ".yml"})
    entries = [_approved_entry(root, path.relative_to(root).as_posix(), _tool_target(path.relative_to(root).as_posix())) for path in sorted(paths)]
    payload = {
        "schema_version": 2,
        "cohort": "tools",
        "scope": "tracked-text-source-only",
        "require_git_tracked": True,
        "entries": entries,
        "consumer_rewrites": [],
        "registry": [{"source": item["source"], "target": item["target"], "category": ("documentation" if item["target"].startswith("docs/") else PurePosixPath(item["target"]).parts[1])} for item in entries],
        "approval": {"status": "approved-by-user", "authorization": "preserve and govern; complete all tasks"},
    }
    return _sign(payload)


def _git(root: Path, *args: str) -> list[str]:
    out = subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8")
    return [line for line in out.splitlines() if line]


def target_for(source: str) -> str:
    rel = PurePosixPath(source).relative_to("integration/scripts")
    first = rel.parts[0]
    if len(rel.parts) == 1:
        return str(PurePosixPath("tools/compat/v1_1") / rel)
    if first in DOMAIN_MAP:
        return str(PurePosixPath("src/personal_knowledge") / DOMAIN_MAP[first] / PurePosixPath(*rel.parts[1:]))
    if first == "_tools":
        return str(PurePosixPath("tools/supported") / PurePosixPath(*rel.parts[1:]))
    if first == "examples":
        return str(PurePosixPath("tools/forensics/examples") / PurePosixPath(*rel.parts[1:]))
    raise ValueError(f"unclassified tracked source: {source}")


def _read_searchable(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    if path.suffix == ".py":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            try:
                ast.parse(text)
            except SyntaxError:
                pass
    return text


def _references(text: str, source: str) -> bool:
    names = {source, source.replace("/", "\\"), PurePosixPath(source).name}
    return any(name in text for name in names)


def build(root: Path) -> dict:
    tracked = sorted(p for p in _git(root, "ls-files", "integration/scripts/*.py") if p.endswith(".py"))
    dirty = {line[3:].replace("\\", "/") for line in _git(root, "status", "--porcelain=v1") if len(line) > 3}
    searchable: dict[str, str] = {}
    for path in _git(root, "ls-files"):
        if path.startswith(PRIVATE_PREFIXES) or path.startswith((".gsd/", ".planning/")):
            continue
        text = _read_searchable(root / path)
        if text is not None:
            searchable[path] = text
    entries = []
    for source in tracked:
        source_path = root / source
        target = target_for(source)
        consumers = [p for p, text in searchable.items() if p != source and _references(text, source)]
        entries.append({
            "source": source,
            "target": target,
            "inverse": {"source": target, "target": source},
            "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "consumers": consumers,
            "dirty": source in dirty,
        })
    payload = {
        "schema_version": 1,
        "scope": "tracked-text-source-only",
        "source_query": "git ls-files integration/scripts/*.py",
        "require_git_tracked": True,
        "tracked_source_count": len(entries),
        "dirty_status_sha256": hashlib.sha256("\n".join(_git(root, "status", "--porcelain=v1")).encode()).hexdigest(),
        "forbidden_prefixes": list(PRIVATE_PREFIXES),
        "entries": entries,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _is_reparse(path: Path) -> bool:
    """Return True without following a Windows reparse point."""
    try:
        attrs = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return path.is_symlink()
    return path.is_symlink() or bool(attrs & 0x400)


def _phase17_paths(root: Path) -> list[dict]:
    """Record Phase 17 code, fixtures and tests even when they are not tracked yet."""
    candidates: list[tuple[Path, str]] = []
    for path in sorted((root / "src/personal_knowledge/evaluation").glob("*.py")):
        candidates.append((path, f"src/personal_knowledge/evaluation/{path.name}"))
    for path in sorted((root / "integration/evals/knowledge_units").glob("*")):
        if path.is_file():
            candidates.append((path, f"assets/evals/knowledge_units/{path.name}"))
    for path in sorted((root / "tests").glob("test_knowledge_eval_*.py")):
        candidates.append((path, f"tests/evaluation/{path.name}"))
    tracked = set(_git(root, "ls-files"))
    return [
        {
            "source": path.relative_to(root).as_posix(),
            "target": target,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "tracked": path.relative_to(root).as_posix() in tracked,
            "status": "tracked-phase17-path" if path.relative_to(root).as_posix() in tracked else "untracked-conflict",
        }
        for path, target in candidates
    ]


def build_canonical_src(root: Path, *, approve_current_bytes: bool = False, approved_preview_sha256: str | None = None) -> dict:
    """Build the immutable canonical implementation cohort preview."""
    full = build(root)
    entries = [item for item in full["entries"] if item["target"].startswith("src/personal_knowledge/")]
    tracked = set(_git(root, "ls-files"))
    for item in _phase17_paths(root):
        if not item["source"].startswith("src/personal_knowledge/evaluation/"):
            continue
        entries.append({
            "source": item["source"],
            "target": item["target"],
            "inverse": {"source": item["target"], "target": item["source"]},
            "sha256": item["sha256"],
            "consumers": [],
            "dirty": True,
            "tracked": item["source"] in tracked,
            "authorized_untracked": approve_current_bytes,
        })
    represented = {item["source"] for item in entries}
    for domain in ("core", "conversation", "knowledge", "memory", "graph", "vector", "pipeline", "services", "source_adapters"):
        for path in sorted((root / "integration/scripts" / domain).rglob("*.py")):
            source = path.relative_to(root).as_posix()
            if source in represented:
                continue
            target = target_for(source)
            entries.append({
                "source": source,
                "target": target,
                "inverse": {"source": target, "target": source},
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "consumers": [],
                "dirty": True,
                "tracked": False,
                "authorized_untracked": approve_current_bytes,
            })
            represented.add(source)
    entries.sort(key=lambda item: item["source"])
    for item in entries:
        item["approved_prestate"] = bool(approve_current_bytes and item.get("dirty"))
    replacements = {item["source"]: item["target"] for item in entries}

    searchable: dict[str, str] = {}
    for rel in _git(root, "ls-files"):
        if rel.startswith(PRIVATE_PREFIXES) or rel.startswith((".gsd/", ".planning/")):
            continue
        text = _read_searchable(root / rel)
        if text is not None:
            searchable[rel] = text
    for path in sorted((root / "tests").glob("*.py")):
        rel = path.relative_to(root).as_posix()
        text = _read_searchable(path)
        if text is not None:
            searchable[rel] = text
    # Phase 17 is deliberately untracked in the current dirty checkout. It is
    # still part of the exact preview, without treating it as approved input.
    for item in _phase17_paths(root):
        rel = item["source"]
        if Path(rel).suffix.lower() not in {".py", ".md", ".yaml", ".yml"}:
            continue
        text = _read_searchable(root / rel)
        if text is not None:
            searchable[rel] = text
    consumer_rewrites = []
    for rel, before in searchable.items():
        after = before
        applied = []
        for old, new in replacements.items():
            variants = (old, old.replace("/", "\\"))
            for old_variant in variants:
                if old_variant in after:
                    replacement = new if "/" in old_variant else new.replace("/", "\\")
                    after = after.replace(old_variant, replacement)
                    applied.append({"old": old_variant, "new": replacement})
        if rel.endswith(".py"):
            for old_module, new_module in IMPORT_MAP.items():
                patterns = (
                    (rf"(?m)^(\s*from\s+){re.escape(old_module)}(?=\.|\s+import\b)", rf"\g<1>{new_module}"),
                    (rf"(?m)^(\s*import\s+){re.escape(old_module)}(?=\.|\s|$)", rf"\g<1>{new_module}"),
                )
                for pattern, replacement in patterns:
                    changed, count = re.subn(pattern, replacement, after)
                    if count:
                        applied.append({"old_module": old_module, "new_module": new_module, "count": count})
                        after = changed
                for quote in ('"', "'"):
                    old_dynamic = f"{quote}{old_module}."
                    new_dynamic = f"{quote}{new_module}."
                    if old_dynamic in after:
                        count = after.count(old_dynamic)
                        after = after.replace(old_dynamic, new_dynamic)
                        applied.append({"old_module": old_module, "new_module": new_module, "count": count, "kind": "dynamic-import"})
            for old_module, new_module in BARE_IMPORT_MAP.items():
                changed, count = re.subn(
                    rf"(?m)^(\s*from\s+){re.escape(old_module)}(?=\.|\s+import\b)",
                    rf"\g<1>{new_module}",
                    after,
                )
                if count:
                    after = changed
                    applied.append({"old_module": old_module, "new_module": new_module, "count": count, "kind": "bare-from"})
                changed, count = re.subn(
                    rf"(?m)^(\s*import\s+){re.escape(old_module)}(?=\s+as\s+)",
                    rf"\g<1>{new_module}",
                    after,
                )
                if count:
                    after = changed
                    applied.append({"old_module": old_module, "new_module": new_module, "count": count, "kind": "bare-import-alias"})
                changed, count = re.subn(
                    rf"(?m)^(\s*)import\s+{re.escape(old_module)}(?=\s*(?:#.*)?$)",
                    rf"\g<1>import {new_module} as {old_module}",
                    after,
                )
                if count:
                    after = changed
                    applied.append({"old_module": old_module, "new_module": new_module, "count": count, "kind": "bare-import"})
        if after != before:
            before_bytes = (root / rel).read_bytes()
            after_bytes = after.encode("utf-8")
            consumer_rewrites.append({
                "path": rel,
                "effective_path_after_move": replacements.get(rel, rel),
                "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
                "before_size": len(before_bytes),
                "before_b64": base64.b64encode(before_bytes).decode("ascii"),
                "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
                "after_text": after,
                "replacements": applied,
            })

    source_bytes = sum((root / item["source"]).stat().st_size for item in entries)
    target_existing = [item["target"] for item in entries if (root / item["target"]).exists()]
    dirty_sources = [item["source"] for item in entries if item["dirty"]]
    all_paths = [value for item in entries for value in (item["source"], item["target"])]
    folded: dict[str, list[str]] = {}
    normalized: dict[str, list[str]] = {}
    for rel in all_paths:
        folded.setdefault(rel.casefold(), []).append(rel)
        normalized.setdefault(unicodedata.normalize("NFC", rel).casefold(), []).append(rel)
    case_collisions = [values for values in folded.values() if len(set(values)) > 1]
    unicode_collisions = [values for values in normalized.values() if len(set(values)) > 1]
    reparse_nodes = []
    for item in entries:
        for candidate in ((root / item["source"]), (root / item["target"]).parent):
            cursor = candidate
            while cursor != root.parent and cursor != root:
                if cursor.exists() and _is_reparse(cursor):
                    reparse_nodes.append(str(cursor.relative_to(root)))
                    break
                cursor = cursor.parent
    longest = max(((root / rel).resolve(strict=False) for rel in all_paths), key=lambda p: len(str(p)))
    free = shutil.disk_usage(root).free
    phase17 = _phase17_paths(root)
    payload = {
        "schema_version": 2,
        "cohort": "canonical-src",
        "scope": "tracked-text-source-only",
        "require_git_tracked": True,
        "source_query": "git ls-files integration/scripts/*.py; targets under src/personal_knowledge only",
        "tracked_source_count": len(entries),
        "dirty_status_sha256": full["dirty_status_sha256"],
        "forbidden_prefixes": full["forbidden_prefixes"],
        "entries": entries,
        "consumer_rewrites": consumer_rewrites,
        "phase17_paths": phase17,
        "preflight_snapshot": {
            "source_bytes": source_bytes,
            "required_stage_bytes": source_bytes * 2,
            "free_bytes": free,
            "same_volume": all((root / item["source"]).resolve(strict=False).drive.casefold() == (root / item["target"]).resolve(strict=False).drive.casefold() for item in entries),
            "max_absolute_path_length": len(str(longest)),
            "max_absolute_path": str(longest),
            "path_length_limit": 240,
            "case_collisions": case_collisions,
            "unicode_nfc_collisions": unicode_collisions,
            "reparse_nodes": sorted(set(reparse_nodes)),
            "dirty_source_conflicts": dirty_sources,
            "existing_target_conflicts": target_existing,
            "phase17_untracked_conflicts": [item["source"] for item in phase17 if not item["tracked"]],
        },
        "approval": {
            "status": "approved-current-bytes" if approve_current_bytes else "pending",
            "approved_preview_sha256": approved_preview_sha256,
            "authorization": "user approved preserve-and-govern current dirty/untracked bytes; complete all tasks",
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--cohort", choices=("all", "canonical-src", "root-shims", "tools", "apps-assets-docs-tests"), default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--classification-output", type=Path)
    parser.add_argument("--approve-current-bytes", action="store_true")
    parser.add_argument("--approved-preview-sha256")
    args = parser.parse_args()
    root = args.root.resolve()
    classification = None
    if args.cohort == "canonical-src":
        payload = build_canonical_src(root, approve_current_bytes=args.approve_current_bytes, approved_preview_sha256=args.approved_preview_sha256)
    elif args.cohort == "root-shims":
        payload = build_root_shims(root)
    elif args.cohort == "tools":
        payload = build_tools(root)
    elif args.cohort == "apps-assets-docs-tests":
        payload, classification = build_apps_assets_docs_tests(root)
    else:
        payload = build(root)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if classification is not None and args.classification_output:
        args.classification_output.parent.mkdir(parents=True, exist_ok=True)
        args.classification_output.write_text(json.dumps(classification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.stdout or not args.output:
        print(rendered, end="")


if __name__ == "__main__":
    main()
