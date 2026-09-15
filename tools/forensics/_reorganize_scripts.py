"""Reorganize flat integration/scripts into packages + compatibility shims.

Layout after:
  scripts/
    core/           shared infra
    knowledge/      knowledge units / RAG
    memory/         memory layer
    conversation/   conversation graphs/summaries
    graph/          relations / triples
    vector/         chroma search / eval
    services/       api / mcp / dashboard
    pipeline/       run_pipeline / import / profiles
    source_adapters/
    examples/
    <name>.py       thin shim → package.module (keeps CLI & tests working)
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# package -> list of module basenames (without .py)
PACKAGES: dict[str, list[str]] = {
    "core": [
        # project_paths already lives in core/
        "common",
        "chroma_client",
        "local_embed",
        "rules",
        "conversation_repository",
        "memory_governance",
    ],
    "knowledge": [
        "knowledge_unit_pipeline",
        "build_knowledge_units",
        "build_knowledge_units_prod",
        "build_knowledge_inventory",
        "build_canonical_knowledge_units",
        "build_knowledge_unit_vector_store",
        "evaluate_knowledge_unit_extraction",
        "evaluate_knowledge_unit_rag",
        "evaluate_knowledge_canary",
        "promote_knowledge_index",
        "reconcile_knowledge_index",
        "refresh_knowledge_units",
        "rollback_knowledge_checkpoint",
        "migrate_add_knowledge_unit_tables",
        "build_pilot_sample",
        "build_pilot_report",
        "backfill_loop",
        "test_knowledge_unit_llm",
    ],
    "memory": [
        "build_memory_store",
        "build_memory_graph",
        "build_memory_evidence_bundles",
        "build_memory_promotion_candidates",
        "build_memory_relation_candidates",
        "build_capability_memory",
        "build_preference_memory",
        "build_context_memory",
        "build_mem0_candidate_memory",
        "build_profile_from_memory",
        "build_deep_memory_profile",
        "extract_memory_candidates_from_bundles",
        "apply_memory_promotions",
        "repair_memory_promotion_candidates",
        "sync_memory_lifecycle",
        "audit_memory_experiments",
        "compare_memory_experiments",
        "analyze_memory_mechanisms",
        "evaluate_memory_depth",
        "evaluate_memory_promotion_candidates",
        "evaluate_memory_relation_candidates",
        "mine_deep_memory_graph",
    ],
    "conversation": [
        "build_conversation_segments",
        "build_conversation_summary",
        "build_conversation_graph",
        "build_conversation_vector_store",
        "build_conversation_eval_set",
        "build_gpt_conversation_summary",
        "build_agentsview_normalized",
        "evaluate_conversation_prompt",
        "evaluate_conversation_quality",
        "evaluate_agent_conversation_cutover",
        "query_conversation_graph",
        "visualize_conversation_graph",
        "rollback_agent_conversation_source",
        "compare_summaries",
        "patch_summary_meta",
    ],
    "graph": [
        "build_graph_relation_candidates",
        "build_graph_relation_candidates_v2",
        "judge_graph_relations",
        "evaluate_graph_relation_judgments",
        "query_graph",
        "build_triple_store",
        "build_merge_layer",
    ],
    "vector": [
        "build_vector_store",
        "search_vectors",
        "evaluate_vector_collections",
        "evaluate_vector_retrieval",
        "unified_search",
    ],
    "services": [
        "api_server",
        "mcp_server",
        "dashboard",
    ],
    "pipeline": [
        "run_pipeline",
        "run_import_pipeline",
        "enrich_unified_events",
        "build_integrated_system",
        "build_deep_profiles",
        "build_context_doc",
        "dump_schema",
        "probe_codex_node",
    ],
}

# module -> package for import rewrite
MODULE_PKG: dict[str, str] = {}
for pkg, mods in PACKAGES.items():
    for m in mods:
        MODULE_PKG[m] = pkg

# known top-level modules that stay or are packages
SKIP_SHIM = {"_reorganize_scripts"}


def ensure_packages() -> None:
    for pkg in PACKAGES:
        d = SCRIPTS / pkg
        d.mkdir(exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            init.write_text(f'"""{pkg} package — integration scripts."""\n', encoding="utf-8")
    # ensure core is a package
    core_init = SCRIPTS / "core" / "__init__.py"
    if not core_init.exists():
        core_init.write_text('"""core package — shared paths and utilities."""\n', encoding="utf-8")


def rewrite_imports(text: str, current_pkg: str) -> str:
    """Rewrite flat local imports to package imports."""

    def repl_from(m: re.Match[str]) -> str:
        mod = m.group(1)
        rest = m.group(2)  # includes " import "
        if mod in MODULE_PKG:
            pkg = MODULE_PKG[mod]
            if pkg == current_pkg:
                return f"from .{mod}{rest}"
            return f"from {pkg}.{mod}{rest}"
        return m.group(0)

    def repl_import_line(m: re.Match[str]) -> str:
        # import foo  OR  import foo as bar
        whole = m.group(0)
        mod = m.group(1)
        if mod not in MODULE_PKG:
            return whole
        pkg = MODULE_PKG[mod]
        tail = m.group(2) or ""  # " as bar" optional
        if pkg == current_pkg:
            if tail:
                return f"from . import {mod}{tail}"
            return f"from . import {mod}"
        if tail:
            # import x as y -> from pkg import x as y
            return f"from {pkg} import {mod}{tail}"
        return f"from {pkg} import {mod}"

    # from X import ...  (incl. multi-line start)
    text = re.sub(
        r"^from ([a-zA-Z_][a-zA-Z0-9_]*)(\s+import\s+)",
        repl_from,
        text,
        flags=re.M,
    )
    # import X / import X as Y  (not import pkg.sub)
    text = re.sub(
        r"^import ([a-zA-Z_][a-zA-Z0-9_]*)(\s+as\s+[a-zA-Z_][a-zA-Z0-9_]*)?\s*$",
        repl_import_line,
        text,
        flags=re.M,
    )
    return text


def make_shim(mod: str, pkg: str) -> str:
    return (
        f'"""Compatibility shim — real module: `{pkg}.{mod}`.\n\n'
        f"Prefer: python -m {pkg}.{mod}  or  from {pkg}.{mod} import ...\n"
        f'This file keeps legacy CLI paths working.\n"""\n'
        f"from __future__ import annotations\n\n"
        f"from {pkg}.{mod} import *  # noqa: F403\n\n"
        f"try:\n"
        f"    from {pkg}.{mod} import main as main  # type: ignore\n"
        f"except ImportError:\n"
        f"    main = None  # type: ignore\n\n"
        f'if __name__ == "__main__":\n'
        f"    import runpy\n"
        f"    import sys\n"
        f"    if main is not None:\n"
        f"        raise SystemExit(main())\n"
        f"    # modules without main(): execute as __main__ via runpy\n"
        f"    sys.argv[0] = __file__\n"
        f'    runpy.run_module("{pkg}.{mod}", run_name="__main__")\n'
    )


def move_modules() -> list[tuple[str, str, str]]:
    """Returns list of (mod, pkg, status)."""
    results = []
    for pkg, mods in PACKAGES.items():
        for mod in mods:
            src = SCRIPTS / f"{mod}.py"
            dest = SCRIPTS / pkg / f"{mod}.py"
            if not src.exists():
                if dest.exists():
                    results.append((mod, pkg, "already_in_package"))
                else:
                    results.append((mod, pkg, "missing"))
                continue
            if dest.exists():
                results.append((mod, pkg, "dest_exists_skip"))
                continue
            text = src.read_text(encoding="utf-8")
            text = rewrite_imports(text, pkg)
            dest.write_text(text, encoding="utf-8")
            # replace original with shim
            src.write_text(make_shim(mod, pkg), encoding="utf-8")
            results.append((mod, pkg, "moved+shim"))
    return results


def main() -> int:
    ensure_packages()
    results = move_modules()
    # write package README
    readme = SCRIPTS / "README.md"
    lines = [
        "# integration/scripts 布局",
        "",
        "按领域分包；根目录 `*.py` 为**兼容入口 shim**（旧命令/测试仍可 `import xxx`）。",
        "",
        "## 包",
        "",
        "| 包 | 职责 |",
        "|----|------|",
        "| `core/` | 路径、common、chroma、embed、规则 |",
        "| `knowledge/` | 知识单元抽取 / 索引 / gate / promote |",
        "| `memory/` | 记忆层构建与评估 |",
        "| `conversation/` | 会话摘要 / 图 / AgentView |",
        "| `graph/` | 关系候选 / 三元组 |",
        "| `vector/` | 向量库与统一检索 |",
        "| `services/` | REST / MCP / dashboard |",
        "| `pipeline/` | 全量管道与导入 |",
        "| `source_adapters/` | 源适配器 |",
        "| `examples/` | 接入示例 |",
        "",
        "## 推荐调用",
        "",
        "```powershell",
        "python integration/scripts/build_knowledge_units_prod.py --status run_xxx",
        "python -m knowledge.build_knowledge_units_prod --status run_xxx",
        "```",
        "",
        "## 本轮移动",
        "",
        "| 模块 | 包 | 状态 |",
        "|------|----|------|",
    ]
    for mod, pkg, status in results:
        lines.append(f"| `{mod}` | `{pkg}` | {status} |")
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")

    moved = sum(1 for *_, s in results if s.startswith("moved"))
    print(f"moved+shim: {moved}")
    for mod, pkg, status in results:
        if status != "moved+shim":
            print(f"  {status}: {mod} -> {pkg}")
    print("wrote", readme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
