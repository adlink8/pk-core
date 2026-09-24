"""Shared D-17 visibility predicate for canonical compatibility consumers."""

from __future__ import annotations

import re
import sqlite3

_SQL_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


def canonical_projection_predicate(
    con: sqlite3.Connection,
    id_column: str,
) -> tuple[str, tuple[str, ...]]:
    """Return the canonical ID predicate bound to the active authority.

    Activation deliberately leaves legacy compatibility rows in place for
    rollback.  When an active v2 authority exists, consumers must see only
    ``v2|`` projection IDs; databases without an authority table retain their
    historical all-row behaviour.
    """

    if not _SQL_COLUMN.fullmatch(id_column):
        raise ValueError(f"invalid canonical id column: {id_column!r}")
    try:
        active_v2 = con.execute(
            "SELECT 1 FROM ce_generation_authority WHERE active=1 LIMIT 1"
        ).fetchone() is not None
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        active_v2 = False
    if active_v2:
        return f"{id_column} LIKE ?", ("v2|%",)
    return "1=1", ()
