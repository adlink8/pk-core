from pathlib import Path

files = [
    "src/personal_knowledge/domains/memory/build_capability_memory.py",
    "src/personal_knowledge/domains/memory/build_context_memory.py",
    "src/personal_knowledge/domains/memory/build_memory_store.py",
    "src/personal_knowledge/domains/memory/build_preference_memory.py",
    "src/personal_knowledge/domains/memory/build_memory_graph.py",
    "src/personal_knowledge/domains/graph/query_graph.py",
]
needle = 'ANALYSIS_DIR = ROOT / "integration" / "analysis"'
repl = "from core.project_paths import STAGE1_PROFILE_DIR as ANALYSIS_DIR  # stage1 reports"
for f in files:
    p = Path(f)
    t = p.read_text(encoding="utf-8")
    if needle not in t:
        print("no change", f)
        continue
    # avoid double import mess if already has project_paths import nearby — simple replace ok
    p.write_text(t.replace(needle, repl), encoding="utf-8")
    print("updated", f)
