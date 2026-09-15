"""Probe AgentsView native analysis surfaces (insights, session stats, etc.)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

p = Path.home() / ".agentsview" / "sessions.db"
con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
con.execute("PRAGMA query_only=ON")
con.row_factory = sqlite3.Row

print("=== insights schema ===")
cols = con.execute("PRAGMA table_info(insights)").fetchall()
for c in cols:
    print(" ", dict(c))

print("\n=== insights rows ===")
for r in con.execute("SELECT * FROM insights LIMIT 5"):
    d = dict(r)
    for k in ("content", "prompt"):
        if k in d and d[k]:
            d[k] = (d[k][:300] + "…") if len(str(d[k])) > 300 else d[k]
    print(json.dumps(d, ensure_ascii=False, indent=2, default=str))

print("\n=== sessions analysis-ish columns sample ===")
scols = [r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()]
interesting = [
    c
    for c in scols
    if any(
        k in c.lower()
        for k in (
            "summary",
            "insight",
            "topic",
            "tag",
            "project",
            "display",
            "cost",
            "token",
            "model",
            "secret",
            "exclude",
            "stat",
            "name",
            "cwd",
            "git",
        )
    )
]
print("interesting cols:", interesting)
# non-null coverage
for c in interesting:
    try:
        n = con.execute(
            f'SELECT COUNT(*) FROM sessions WHERE "{c}" IS NOT NULL AND TRIM(CAST("{c}" AS TEXT))<>\'\''
        ).fetchone()[0]
        print(f"  sessions.{c} non_null={n}")
    except Exception as e:
        print(f"  sessions.{c} err {e}")

print("\n=== stats table ===")
for r in con.execute("SELECT * FROM stats"):
    print(dict(r))

print("\n=== secret_findings sample ===")
for r in con.execute(
    "SELECT rule_name, confidence, COUNT(*) c FROM secret_findings GROUP BY 1,2"
):
    print(dict(r))

print("\n=== tool_calls category top ===")
for r in con.execute(
    "SELECT category, COUNT(*) c FROM tool_calls GROUP BY 1 ORDER BY c DESC LIMIT 15"
):
    print(dict(r))

print("\n=== usage_events models ===")
for r in con.execute(
    "SELECT model, COUNT(*) c FROM usage_events GROUP BY 1 ORDER BY c DESC LIMIT 10"
):
    print(dict(r))

con.close()
