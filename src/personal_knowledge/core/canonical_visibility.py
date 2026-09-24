"""Shared D-17 visibility predicate for canonical compatibility consumers."""

from __future__ import annotations

import re

_SQL_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


def canonical_projection_predicate(
    con,
    id_column: str,
) -> tuple[str, tuple[str, ...]]:
    """Return the canonical ID predicate bound to the active authority.

    Historically this hid every row that did not carry the ``v2|`` projection
    prefix, so an active generation's consumers saw only its own rows and a
    rollback could restore the previous set. That predicate became wrong with
    the 2026-09-23 uniform-id migration: projection rows and the rows around
    them share one origin-derived id shape, so a prefix filter matches nothing
    and silently blanks every consumer. The store is also a merged union by
    design — snapshot-track rows (AgentsView-addressed sessions the adapter
    never captured) must stay visible next to projection rows, or consumers
    lose whole families.

    Deactivation still removes the projection's own rows (recorded in
    ``ce_projected_ids`` at write time), so there is nothing left to hide.
    """
    if not _SQL_COLUMN.fullmatch(id_column):
        raise ValueError(f"invalid canonical id column: {id_column!r}")
    return "1=1", ()
