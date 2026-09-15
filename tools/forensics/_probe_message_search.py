import sqlite3
from pathlib import Path

for p in [
    Path("Agent/structured/db/agent_conversations.sqlite"),
    Path.home() / ".agentsview" / "sessions.db",
]:
    print("===", p, "exists", p.exists())
    if not p.exists():
        continue
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
    except Exception:
        pass
    rows = con.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    for n, t in rows:
        if any(k in n.lower() for k in ("fts", "message", "search", "canon")):
            print(f"  {t}: {n}")
    # try fts match
    for fts in ("messages_fts", "canonical_messages_fts"):
        try:
            n = con.execute(
                f"SELECT COUNT(*) FROM {fts} WHERE {fts} MATCH ?",
                ("python OR shell",),
            ).fetchone()[0]
            print(f"  FTS ok {fts} hits={n}")
        except Exception as e:
            print(f"  FTS fail {fts}: {type(e).__name__}: {e}")
    # sample message columns
    for tbl in ("messages", "canonical_messages"):
        try:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
            print(f"  {tbl} cols={cols[:14]}")
        except Exception:
            pass
    con.close()
