"""Deeper I03: canonical membership + evidence reachability."""
import sqlite3
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.project_paths import UNIFIED_DB, AGENT_CONVERSATIONS_DB, AI_CONTEXT_DIR

con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("member-like", [t for t in tabs if "member" in t or "canonical" in t])

for t in tabs:
    if "member" in t or "canonical" in t:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(t, n, cols)

# If canonical_unit_members exists: how many canonical units have ≥1 member with evidence?
if "canonical_unit_members" in tabs:
    cols = [r[1] for r in con.execute("PRAGMA table_info(canonical_unit_members)")]
    print("sample member", dict(con.execute("SELECT * FROM canonical_unit_members LIMIT 1").fetchone()))
    total_canon = con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
    # members with evidence
    with_ev = con.execute(
        """
        SELECT COUNT(DISTINCT m.canonical_unit_id)
        FROM canonical_unit_members m
        JOIN knowledge_unit_evidence e ON e.unit_id = m.unit_id
        """
    ).fetchone()[0] if "unit_id" in cols else None
    print("canon total", total_canon, "with member evidence", with_ev)
    members = con.execute("SELECT COUNT(*) FROM canonical_unit_members").fetchone()[0]
    print("members", members)

# Also check latest sqlite comparison draft_n
sq = json.loads((AI_CONTEXT_DIR / "sqlite_generation_comparison.json").read_text(encoding="utf-8"))
k = sq["layers"]["knowledge"]
print("stale comparison draft rows", k["tables"].get("knowledge_units", {}).get("rows"))
print("stale evidence", k.get("evidence"))
print("live draft", con.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0])
print("live evidence units", con.execute("SELECT COUNT(DISTINCT unit_id) FROM knowledge_unit_evidence").fetchone()[0])

# When was evidence filled relative to comparison?
print("sqlite_generation generated_at", sq.get("generated_at"))

# Check if any unit has source_message_ref that is NOT in evidence (should be 0)
orphan_smr = con.execute(
    """
    SELECT COUNT(*) FROM knowledge_units k
    WHERE (k.source_message_ref IS NOT NULL AND TRIM(k.source_message_ref) != '')
      AND NOT EXISTS (
        SELECT 1 FROM knowledge_unit_evidence e
        WHERE e.unit_id = k.unit_id AND e.evidence_ref = k.source_message_ref
      )
    """
).fetchone()[0]
print("units with smr not mirrored in evidence", orphan_smr)

con.close()
