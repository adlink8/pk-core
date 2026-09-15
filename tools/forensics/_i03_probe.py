"""Probe I03 evidence metrics more carefully."""
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.project_paths import UNIFIED_DB, AGENT_CONVERSATIONS_DB, AI_CONTEXT_DIR
import json

con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("ku total", con.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0])
print("ev total", con.execute("SELECT COUNT(*) FROM knowledge_unit_evidence").fetchone()[0])
print("distinct unit_id in ev", con.execute("SELECT COUNT(DISTINCT unit_id) FROM knowledge_unit_evidence").fetchone()[0])
print("runs", con.execute("SELECT run_id, COUNT(*) c FROM knowledge_units GROUP BY run_id ORDER BY c DESC").fetchall())
print("status", con.execute("SELECT status, COUNT(*) c FROM knowledge_units GROUP BY status").fetchall())

# How was 0.511 computed? Check gap analysis source
gap = json.loads((AI_CONTEXT_DIR / "generation_gap_analysis.json").read_text(encoding="utf-8"))
print("gap evidence_coverage raw", gap.get("metrics", gap) if False else "")
# search keys
def find_ev(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "evidence" in str(k).lower() or k in ("G1", "current"):
                print(path + "." + k, "=>", v if not isinstance(v, (dict, list)) else type(v))
            find_ev(v, path + "." + k)
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        for i, x in enumerate(obj[:5]):
            find_ev(x, path + f"[{i}]")

find_ev(gap)

# Coverage by run: units with evidence / units
for run_id, cnt in con.execute("SELECT run_id, COUNT(*) c FROM knowledge_units GROUP BY run_id ORDER BY c DESC"):
    with_ev = con.execute(
        """SELECT COUNT(DISTINCT k.unit_id) FROM knowledge_units k
           INNER JOIN knowledge_unit_evidence e ON e.unit_id=k.unit_id
           WHERE k.run_id=?""",
        (run_id,),
    ).fetchone()[0]
    print(f"run {run_id[:20]}... total={cnt} with_ev={with_ev} cov={with_ev/cnt:.3f}")

# Active collection has 30012 - match draft run
active_run = "76c6259e"  # substring
# find run with ~30k
for run_id, cnt in con.execute("SELECT run_id, COUNT(*) c FROM knowledge_units GROUP BY run_id ORDER BY c DESC"):
    if cnt > 20000:
        empty_smr = con.execute(
            """SELECT COUNT(*) FROM knowledge_units
               WHERE run_id=? AND (source_message_ref IS NULL OR TRIM(source_message_ref)='')""",
            (run_id,),
        ).fetchone()[0]
        nonempty_smr = cnt - empty_smr
        # of nonempty, how many exist in canonical
        refs = [
            r[0]
            for r in con.execute(
                """SELECT source_message_ref FROM knowledge_units
                   WHERE run_id=? AND source_message_ref IS NOT NULL AND TRIM(source_message_ref)!=''""",
                (run_id,),
            )
        ]
        print(f"large run {run_id} empty_smr={empty_smr} nonempty={nonempty_smr}")
        # check evidence_quote empty as proxy for weak evidence
        empty_quote = con.execute(
            """SELECT COUNT(*) FROM knowledge_units
               WHERE run_id=? AND (evidence_quote IS NULL OR TRIM(evidence_quote)='')""",
            (run_id,),
        ).fetchone()[0]
        print("  empty_quote", empty_quote)

# Canonical units coverage
canon_cols = [r[1] for r in con.execute("PRAGMA table_info(canonical_knowledge_units)")]
print("canon cols", canon_cols)
print("canon count", con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0])
if "source_message_ref" in canon_cols:
    empty = con.execute(
        "SELECT COUNT(*) FROM canonical_knowledge_units WHERE source_message_ref IS NULL OR TRIM(source_message_ref)=''"
    ).fetchone()[0]
    print("canon empty smr", empty)

# Maybe inventory items vs extracted units?
# Compare authoritative_count 16743 vs units?
# Or evidence coverage = units_with_evidence / inventory authoritative?
auth = 16743
ev_units = con.execute("SELECT COUNT(DISTINCT unit_id) FROM knowledge_unit_evidence").fetchone()[0]
print("ev_units/auth", ev_units / auth)

# Another definition: share of draft units that have evidence_ref resolving to cm|
# ALL have evidence rows now - maybe after a recent fill?

# Check if evidence_ref matches source_message_ref always
mismatch = con.execute(
    """SELECT COUNT(*) FROM knowledge_units k
       JOIN knowledge_unit_evidence e ON e.unit_id=k.unit_id
       WHERE e.evidence_ref != k.source_message_ref
          OR k.source_message_ref IS NULL OR TRIM(k.source_message_ref)=''"""
).fetchone()[0]
print("mismatch or empty smr vs evidence_ref", mismatch)

# Resolvable rate of evidence_ref in agent_conversations
ccon = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
refs = [r[0] for r in con.execute("SELECT DISTINCT evidence_ref FROM knowledge_unit_evidence").fetchall()]
print("unique evidence_ref", len(refs))
# batch existence
exist = 0
for i in range(0, len(refs), 800):
    chunk = refs[i : i + 800]
    ph = ",".join("?" * len(chunk))
    exist += ccon.execute(
        f"SELECT COUNT(*) FROM canonical_messages WHERE canonical_message_id IN ({ph})",
        chunk,
    ).fetchone()[0]
print("unique refs exist in cm", exist, "rate", exist / max(len(refs), 1))

# units whose evidence_ref does NOT resolve
# sample 5k
bad = 0
total = 0
for row in con.execute(
    "SELECT k.unit_id, e.evidence_ref FROM knowledge_units k JOIN knowledge_unit_evidence e ON e.unit_id=k.unit_id LIMIT 5000"
):
    total += 1
    if not ccon.execute(
        "SELECT 1 FROM canonical_messages WHERE canonical_message_id=? LIMIT 1",
        (row[1],),
    ).fetchone():
        bad += 1
print(f"among 5000 joined units, unresolvable evidence_ref={bad} ({bad/total:.3f})")

# Alternative: coverage measured as fraction of inventory messages that produced KU with evidence
# Or from sqlite generation comparison
sq = AI_CONTEXT_DIR / "sqlite_generation_comparison.json"
if sq.exists():
    data = json.loads(sq.read_text(encoding="utf-8"))
    # print relevant
    def walk(o, p=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if any(x in str(k).lower() for x in ("evidence", "cover", "link", "knowledge")):
                    if not isinstance(v, (dict, list)):
                        print("sq", p + "." + k, v)
                if k in ("draft", "knowledge", "layers", "summary") or "evidence" in str(k).lower():
                    walk(v, p + "." + k)
        elif isinstance(o, list) and o and isinstance(o[0], dict):
            for i, x in enumerate(o[:3]):
                walk(x, p + f"[{i}]")
    walk(data)

con.close()
ccon.close()
