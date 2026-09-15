"""Inventory integration/analysis for cleanup planning."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
root = ROOT / "integration" / "analysis"

rows = []
by_dir: dict = defaultdict(lambda: {"n": 0, "bytes": 0, "ext": Counter()})
for p in root.rglob("*"):
    if not p.is_file():
        continue
    rel = p.relative_to(root)
    parent = str(rel.parent) if str(rel.parent) != "." else "."
    sz = p.stat().st_size
    ext = p.suffix.lower() or "(none)"
    rows.append((str(rel).replace("\\", "/"), sz, ext, parent))
    by_dir[parent]["n"] += 1
    by_dir[parent]["bytes"] += sz
    by_dir[parent]["ext"][ext] += 1

print("TOTAL_FILES", len(rows))
print("TOTAL_MB", round(sum(r[1] for r in rows) / 1024 / 1024, 2))
print()
print("=== BY DIR (sorted by size) ===")
for d, info in sorted(by_dir.items(), key=lambda x: -x[1]["bytes"]):
    print(
        f"{info['n']:4d} files  {info['bytes']/1024/1024:8.2f} MB  {d}  "
        f"ext={dict(info['ext'].most_common(6))}"
    )

print()
print("=== TOP 50 LARGEST FILES ===")
for rel, sz, ext, parent in sorted(rows, key=lambda x: -x[1])[:50]:
    print(f"{sz/1024/1024:7.2f} MB  {rel}")

print()
print("=== EXT TOTAL ===")
extc = Counter(r[2] for r in rows)
for e, c in extc.most_common():
    b = sum(r[1] for r in rows if r[2] == e)
    print(f"{e:12} {c:4d}  {b/1024/1024:8.2f} MB")

# pattern buckets
patterns = {
    "phase14_": [],
    "knowledge_unit_": [],
    "memory_": [],
    "vector_": [],
    "sqlite_": [],
    "conversation_": [],
    "profile_": [],
    "deep_memory_": [],
    "graph_": [],
    "mem0_": [],
    "canary_": [],
    "test_coverage_": [],
    "other": [],
}
for rel, sz, ext, parent in rows:
    name = Path(rel).name
    hit = False
    for pref in list(patterns.keys()):
        if pref == "other":
            continue
        if name.startswith(pref) or f"/{pref}" in f"/{name}":
            patterns[pref].append((rel, sz))
            hit = True
            break
    if not hit:
        # also match mid-name
        matched = False
        for pref in ("phase14", "knowledge_unit", "memory_", "vector_", "sqlite_",
                     "conversation_", "profile", "deep_memory", "graph_", "mem0", "canary"):
            if pref in name:
                key = pref if pref.endswith("_") else pref + ("_" if not pref.endswith("_") and pref not in ("phase14",) else "")
                # normalize
                for k in patterns:
                    if k.rstrip("_") in pref or pref.startswith(k.rstrip("_")):
                        patterns[k].append((rel, sz))
                        matched = True
                        break
                if matched:
                    break
        if not matched:
            patterns["other"].append((rel, sz))

print()
print("=== NAME PREFIX BUCKETS ===")
for k, items in patterns.items():
    if not items:
        continue
    mb = sum(s for _, s in items) / 1024 / 1024
    print(f"{k:18} {len(items):4d} files  {mb:8.2f} MB")
