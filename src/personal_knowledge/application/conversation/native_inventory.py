"""Read-only AgentsView locator inventory for Phase 62 native capture.

AgentsView remains discovery/crosswalk metadata only.  This module reads only
the ``sessions`` locator columns, resolves ``path#session-id`` virtual SQLite
locators, and returns paths/counts without reading conversation bodies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.registry import resolve_family


@dataclass(frozen=True)
class NativeLocator:
    family: str
    agent: str
    session_id: str
    source_path: Path | None
    virtual_session_id: str | None
    source_exists: bool


def read_native_inventory(db: Path) -> tuple[NativeLocator, ...]:
    """Read only session id, agent and file_path from AgentsView."""
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT id, agent, file_path FROM sessions "
            "WHERE deleted_at IS NULL ORDER BY agent,id"
        ).fetchall()
    finally:
        con.close()
    result: list[NativeLocator] = []
    for row in rows:
        agent = str(row["agent"])
        try:
            family = "mimo" if agent == "mimocode" else resolve_family(agent)
        except KeyError:
            family = agent
        path, virtual = split_virtual_locator(row["file_path"])
        result.append(NativeLocator(
            family=family, agent=agent, session_id=str(row["id"]),
            source_path=path, virtual_session_id=virtual,
            source_exists=bool(path and path.is_file()),
        ))
    return tuple(result)


def split_virtual_locator(value: str | None) -> tuple[Path | None, str | None]:
    if not value:
        return None, None
    raw = str(value)
    path_text, marker, virtual = raw.partition("#")
    return Path(path_text), (virtual or None) if marker else None


def inventory_summary(rows: tuple[NativeLocator, ...]) -> dict:
    families: dict[str, dict] = {}
    for row in rows:
        item = families.setdefault(row.family, {
            "sessions": 0, "native_available_sessions": 0,
            "unique_files": set(), "missing_sessions": 0,
        })
        item["sessions"] += 1
        if row.source_exists:
            item["native_available_sessions"] += 1
            item["unique_files"].add(str(row.source_path))
        else:
            item["missing_sessions"] += 1
    return {
        family: {
            "sessions": item["sessions"],
            "native_available_sessions": item["native_available_sessions"],
            "unique_files": len(item["unique_files"]),
            "missing_sessions": item["missing_sessions"],
        }
        for family, item in sorted(families.items())
    }


__all__ = ["NativeLocator", "inventory_summary", "read_native_inventory", "split_virtual_locator"]
