"""Inspect live AgentsView sessions.db (read-only)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

p = Path.home() / ".agentsview" / "sessions.db"
print("path", p)
print("exists", p.exists())
if not p.exists():
    raise SystemExit(0)
print("size_mb", round(p.stat().st_size / 1024 / 1024, 2))
con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
con.execute("PRAGMA query_only=ON")
tables = [
    r[0]
    for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
]
print("tables", len(tables))
for t in tables:
    try:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except Exception as e:
        n = f"err:{e}"
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")').fetchall()]
    print(f"  {t}: {n}  cols={len(cols)}  {cols[:10]}{'...' if len(cols)>10 else ''}")
# sample agent names / sources if possible
for t, col in (
    ("sessions", "agent"),
    ("sessions", "source"),
    ("messages", "role"),
):
    if t in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")').fetchall()]
        if col in cols:
            rows = con.execute(
                f'SELECT "{col}", COUNT(*) c FROM "{t}" GROUP BY 1 ORDER BY c DESC LIMIT 12'
            ).fetchall()
            print(f"dist {t}.{col}:", rows)
con.close()
