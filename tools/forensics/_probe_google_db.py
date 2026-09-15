import sqlite3
from pathlib import Path

p = Path("Google/structured/db/google_data.sqlite")
con = sqlite3.connect(p)
print("tables", [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")])
print("activities", con.execute("SELECT COUNT(*) FROM activities").fetchone()[0])
print("normalized cols", con.execute("PRAGMA table_info(normalized_events)").fetchall())
print("by service", con.execute("SELECT service, COUNT(*) c FROM activities GROUP BY 1 ORDER BY c DESC").fetchall())
print("by category", con.execute("SELECT category, COUNT(*) c FROM activities GROUP BY 1 ORDER BY c DESC LIMIT 15").fetchall())
print("samples:")
for r in con.execute(
    "SELECT service, category, substr(title_or_query,1,60), domain FROM activities LIMIT 8"
):
    print(" ", r)
con.close()
