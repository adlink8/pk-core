"""SQLite connection policy for production writes."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect_rw(
    database: str | Path,
    *,
    timeout: float = 5.0,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Open a read/write connection with referential integrity enforced."""
    con = sqlite3.connect(str(database), timeout=timeout, **kwargs)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        enabled = int(con.execute("PRAGMA foreign_keys").fetchone()[0])
        if enabled != 1:
            raise RuntimeError("SQLite foreign key enforcement could not be enabled")
        return con
    except Exception:
        con.close()
        raise


def assert_foreign_key_integrity(con: sqlite3.Connection) -> None:
    """Fail closed when a publish boundary sees existing FK violations."""
    first = con.execute("PRAGMA foreign_key_check").fetchone()
    if first is not None:
        table, rowid, parent, fk_index = first
        raise RuntimeError(
            "SQLite foreign key check failed before publish: "
            f"table={table} rowid={rowid} parent={parent} fk_index={fk_index}"
        )
