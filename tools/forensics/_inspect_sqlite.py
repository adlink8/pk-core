"""Quick inventory of major SQLite databases."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DBS = [
    ROOT / "integration" / "db" / "personal_system.sqlite",
    ROOT / "Agent" / "structured" / "db" / "agent_conversations.sqlite",
    ROOT / "Agent" / "structured" / "db" / "agentsview_normalized.sqlite",
    ROOT / "Agent" / "structured" / "db" / "agent_data.sqlite",
    ROOT / "Google" / "structured" / "db" / "google_data.sqlite",
]


def inspect(db: Path) -> None:
    print("===", db.relative_to(ROOT), "===")
    if not db.exists():
        print(" missing")
        return
    print(" size_mb", round(db.stat().st_size / 1024 / 1024, 2))
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(" tables", len(tables))
    for (t,) in tables:
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")').fetchall()]
            extra = "..." if len(cols) > 12 else ""
            print(f"  {t}: {n:,} cols={len(cols)} | {cols[:12]}{extra}")
        except Exception as e:
            print(" ", t, e)
    con.close()


if __name__ == "__main__":
    for db in DBS:
        inspect(db)
