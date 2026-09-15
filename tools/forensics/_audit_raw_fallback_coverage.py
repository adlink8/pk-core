"""Audit raw fallback (personal_events) coverage vs AgentsView / Google / KU."""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UNIFIED = ROOT / "integration" / "db" / "personal_system.sqlite"
AGENT_CONV = ROOT / "Agent" / "structured" / "db" / "agent_conversations.sqlite"
AGENT_NORM = ROOT / "Agent" / "structured" / "db" / "agentsview_normalized.sqlite"
AGENT_DATA = ROOT / "Agent" / "structured" / "db" / "agent_data.sqlite"
GOOGLE = ROOT / "Google" / "structured" / "db" / "google_data.sqlite"
VIEW = Path.home() / ".agentsview" / "sessions.db"


def ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
    except Exception:
        pass
    return con


def count_tables(path: Path, tables: list[str]) -> dict:
    if not path.exists():
        return {"_exists": False}
    con = ro(path)
    out = {"_exists": True, "_size_mb": round(path.stat().st_size / 1024 / 1024, 2)}
    names = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for t in tables:
        if t in names:
            out[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        else:
            out[t] = None
    con.close()
    return out


print("=== SOURCE COUNTS ===")
print("view", count_tables(VIEW, ["sessions", "messages", "tool_calls", "insights"]))
print("agentsview_normalized", count_tables(AGENT_NORM, ["sessions", "messages", "tool_events"]))
print("agent_conversations", count_tables(AGENT_CONV, ["canonical_sessions", "canonical_messages", "canonical_tool_events"]))
print("agent_data", count_tables(AGENT_DATA, ["sessions", "agent_messages", "session_messages"]))
print("google", count_tables(GOOGLE, ["activities", "gemini_attachments", "normalized_events"]))

con = ro(UNIFIED)
print("\n=== UNIFIED_EVENTS ===")
for r in con.execute(
    "SELECT source, event_type, COUNT(*) c FROM unified_events GROUP BY 1,2 ORDER BY source, c DESC"
):
    print(f"  {r[0]:8} {r[1]:24} {r[2]}")
print("totals by source:")
for r in con.execute("SELECT source, COUNT(*) FROM unified_events GROUP BY source"):
    print(" ", r)

# rich coverage
print("\ncontent_rich coverage by source:")
for r in con.execute(
    """
    SELECT ue.source,
           COUNT(*) total,
           SUM(CASE WHEN length(coalesce(r.content_rich,''))>=10 THEN 1 ELSE 0 END) rich_ok,
           SUM(CASE WHEN length(coalesce(ue.content,''))>=10 THEN 1 ELSE 0 END) content_ok
    FROM unified_events ue
    LEFT JOIN unified_events_rich r ON r.event_id=ue.event_id
    GROUP BY ue.source
    """
):
    print(f"  {r[0]}: total={r[1]} rich_ok={r[2]} content_ok={r[3]} rich_pct={r[2]/max(r[1],1):.2%}")

# knowledge evidence refs - agent only
print("\n=== KNOWLEDGE vs DIALOGUE ===")
ku = con.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0]
cku = con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
ev = con.execute("SELECT COUNT(*) FROM knowledge_unit_evidence").fetchone()[0]
print(f"knowledge_units={ku} canonical={cku} evidence_rows={ev}")

# inventory sources
try:
    for r in con.execute(
        "SELECT source, COUNT(*) FROM knowledge_inventory_items GROUP BY source ORDER BY 2 DESC"
    ):
        print(" inventory_items source:", r)
except Exception as e:
    print(" inventory", e)

# any google in knowledge?
for r in con.execute(
    """
    SELECT COUNT(*) FROM knowledge_units
    WHERE lower(coalesce(source_agent,'')) LIKE '%google%'
       OR lower(coalesce(subject,'')) LIKE '%google takeout%'
    """
):
    print(" ku with google-ish:", r[0])

con.close()

# Compare View messages vs canonical vs personal_events path
print("\n=== VIEW vs CANONICAL MESSAGE COVERAGE ===")
if VIEW.exists() and AGENT_CONV.exists():
    v = ro(VIEW)
    a = ro(AGENT_CONV)
    v_msg = v.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    v_user = v.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
    v_sess = v.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    c_msg = a.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0]
    c_user = a.execute(
        "SELECT COUNT(*) FROM canonical_messages WHERE role='user'"
    ).fetchone()[0]
    c_sess = a.execute("SELECT COUNT(*) FROM canonical_sessions").fetchone()[0]
    print(f"  view messages={v_msg} user={v_user} sessions={v_sess}")
    print(f"  canonical messages={c_msg} user={c_user} sessions={c_sess}")
    print(f"  msg ratio canonical/view={c_msg/max(v_msg,1):.2%}")
    # agent dist
    print("  view agents:", v.execute(
        "SELECT agent, COUNT(*) FROM sessions GROUP BY agent ORDER BY 2 DESC"
    ).fetchall())
    print("  canonical agents:", a.execute(
        "SELECT agent, COUNT(*) FROM canonical_sessions GROUP BY agent ORDER BY 2 DESC"
    ).fetchall() if 'agent' in [x[1] for x in a.execute('PRAGMA table_info(canonical_sessions)')] else a.execute(
        "SELECT primary_source, COUNT(*) FROM canonical_sessions GROUP BY 1"
    ).fetchall())
    v.close()
    a.close()

# Chroma personal_events
print("\n=== CHROMA personal_events ===")
import sys
sys.path.insert(0, str(ROOT / "integration" / "scripts"))
from personal_knowledge.core.chroma_client import ChromaClient  # noqa: E402

client = ChromaClient()
coll = client.get_or_create_collection("personal_events")
total = coll.count()
print(" count", total)
# full-ish source scan in batches
src = Counter()
etype = Counter()
scanned = 0
batch = 1000
for off in range(0, total, batch):
    s = coll.get(limit=min(batch, total - off), offset=off, include=["metadatas"])
    for m in s.get("metadatas") or []:
        m = m or {}
        src[m.get("source") or "?"] += 1
        etype[f"{m.get('source')}|{m.get('event_type')}"] += 1
        scanned += 1
print(" sources", dict(src))
print(" top event types", etype.most_common(12))
print(" scanned", scanned, "gap_vs_unified", "see above")

# vectorizable vs count
con = ro(UNIFIED)
rows = con.execute(
    """
    SELECT COUNT(*) FROM unified_events ue
    LEFT JOIN unified_events_rich r ON r.event_id=ue.event_id
    WHERE length(coalesce(nullif(r.content_rich,''), ue.content, '')) >= 10
    """
).fetchone()[0]
print(f" unified vectorizable(content>=10)={rows} chroma={total} gap={rows-total}")
con.close()

print("\n=== GOOGLE STRUCTURE DEPTH ===")
if GOOGLE.exists():
    g = ro(GOOGLE)
    tables = [r[0] for r in g.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(" tables", tables)
    for t in ("activities", "normalized_events", "gemini_attachments"):
        if t in tables:
            n = g.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            cols = [r[1] for r in g.execute(f"PRAGMA table_info({t})").fetchall()]
            print(f"  {t}: {n} cols={cols}")
    # service dist
    if "activities" in tables:
        print("  by service:", g.execute(
            "SELECT service, COUNT(*) FROM activities GROUP BY service ORDER BY 2 DESC"
        ).fetchall())
    g.close()

# Is Google in knowledge inventory / units at all?
con = ro(UNIFIED)
print("\nknowledge tables touching google content:")
# evidence refs start with cm| for conversation
ev_sample = con.execute(
    "SELECT evidence_ref FROM knowledge_unit_evidence LIMIT 5"
).fetchall()
print("  evidence_ref samples:", ev_sample)
cm_like = con.execute(
    "SELECT COUNT(*) FROM knowledge_unit_evidence WHERE evidence_ref LIKE 'cm|%'"
).fetchone()[0]
print("  evidence cm|* :", cm_like, "/", ev)
# google event_ids in unified
g_events = con.execute(
    "SELECT COUNT(*) FROM unified_events WHERE source='Google'"
).fetchone()[0]
print("  google events in unified:", g_events)
print("  google in personal_events:", src.get("Google", 0))
con.close()

print("\n=== CONCLUSION SNAPSHOT ===")
print(
    """
raw fallback = personal_events chroma ≈ vectorized unified_events (not live View)
AgentsView = live dialogue SSOT
Google = separate old structured layer, not in KU pipeline
"""
)
