"""Phase 62-05: generation/policy-bound view persistence.

Phase 62 CONTEXT D-16/D-21/D-22/D-24: views are queryable projections with
exact lineage and lifecycle, never a competing evidence or knowledge SSOT.
This module owns ONLY view storage:

  - additive ``ce_view_*`` companion tables beside the v2 event authority
  - :class:`ViewRepository` — idempotent save/rebuild, policy revision,
    generation drift, stale marking, lineage resolution and digest reads
  - view rows bind to exactly one staged/validated event generation and one
    policy digest; they never write ``canonical_*`` compatibility tables,
    KU tables, or ``ce_generation_authority`` (read-only authority queries
    still flow through the 62-01/62-04 event repository seam)

A saved revision is REPLACED by an identical (generation, policy digest,
builder version, view digest) revision, so rebuild is idempotent. A revision
changes when the event generation, the policy, or the builder version changes
— raw artifact/event/view identities are never rewritten by this module.

No I/O outside the caller-provided DB path; no network, no provider calls
(D-31).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from personal_knowledge.application.conversation.event_repository import (
    EventRepository,
)
from personal_knowledge.application.conversation.extraction_policy import (
    ExtractionPolicy,
    policy_digest,
)
from personal_knowledge.application.conversation.extraction_views import (
    BUILDER_VERSION,
    DerivedView,
    ViewBuildResult,
)

VIEW_TABLES: tuple[str, ...] = (
    "ce_view_headers",
    "ce_view_members",
    "ce_view_lineage",
    "ce_view_contradictions",
    "ce_view_revisions",
)

_VIEW_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ce_view_headers (
        revision_id   TEXT PRIMARY KEY,
        generation_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        view_id       TEXT NOT NULL,
        view_type     TEXT NOT NULL,
        builder_version TEXT NOT NULL,
        session_id    TEXT,
        fidelity_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        lifecycle     TEXT NOT NULL DEFAULT 'active',
        view_digest   TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        FOREIGN KEY (generation_id) REFERENCES ce_event_generations(generation_id),
        UNIQUE (generation_id, policy_digest, view_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_view_members (
        generation_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        view_id       TEXT NOT NULL,
        event_id      TEXT NOT NULL,
        PRIMARY KEY (generation_id, policy_digest, view_id, event_id),
        FOREIGN KEY (generation_id) REFERENCES ce_event_generations(generation_id),
        FOREIGN KEY (generation_id, event_id)
            REFERENCES ce_events(generation_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_view_lineage (
        generation_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        view_id       TEXT NOT NULL,
        lineage_ref   TEXT NOT NULL,
        PRIMARY KEY (generation_id, policy_digest, view_id, lineage_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_view_contradictions (
        generation_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        view_id       TEXT NOT NULL,
        slot_id       TEXT NOT NULL,
        kind          TEXT NOT NULL,
        refs_json     TEXT NOT NULL,
        PRIMARY KEY (generation_id, policy_digest, view_id, slot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_view_revisions (
        generation_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        builder_version TEXT NOT NULL,
        view_digest   TEXT NOT NULL,
        view_count    INTEGER NOT NULL,
        saved_at      TEXT NOT NULL,
        PRIMARY KEY (generation_id, policy_digest)
    )
    """,
)


class ViewRepositoryError(RuntimeError):
    """A view persistence operation violated the view/lineage contract."""


class ViewLifecycle(str, Enum):
    """Lifecycle states for a persisted view revision."""

    ACTIVE = "active"
    STALE = "stale"


@dataclass(frozen=True)
class ViewRevision:
    """One persisted view revision for (generation, policy digest)."""

    generation_id: str
    policy_digest: str
    builder_version: str
    view_digest: str
    view_count: int
    saved_at: str


class ViewRepository:
    """Generation/policy-bound persistence for derived views.

    Views are replaceable/rebuildable projections: saving the same revision
    twice is idempotent, and the repository never creates a fact authority.
    """

    def __init__(self, db: Path) -> None:
        self.db = Path(db)

    # ------------------------------------------------------------ schema

    def create_schema(self) -> None:
        """Apply additive view DDL (idempotent; v2/legacy tables untouched)."""
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("PRAGMA foreign_keys=ON")
            for statement in _VIEW_DDL:
                con.execute(statement)
            con.commit()
        finally:
            con.close()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)
        else:
            con = sqlite3.connect(str(self.db))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    # ------------------------------------------------------------ write

    def save_view_revision(
        self,
        view_result: ViewBuildResult,
        policy: ExtractionPolicy,
        *,
        lifecycle: ViewLifecycle = ViewLifecycle.ACTIVE,
    ) -> ViewRevision:
        """Persist one generation/policy-bound view revision (idempotent).

        An identical revision (generation, policy digest, builder version,
        view digest) replaces the prior revision in place; a changed policy or
        event generation creates a distinct revision that leaves the prior one
        fully queryable.
        """
        if not _generation_exists(self.db, view_result.generation_id):
            raise ViewRepositoryError(
                f"views reference an unknown event generation: "
                f"{view_result.generation_id}"
            )
        if view_result.builder_version != BUILDER_VERSION:
            raise ViewRepositoryError(
                "view result builder version must match the persisted builder "
                "version contract"
            )
        digest_value = policy.digest
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        view_digest = view_result.digest

        # idempotent rebuild: an identical revision (generation, policy,
        # builder, view digest, count) is returned unchanged, so re-running a
        # rebuild never rewrites rows or bumps the saved timestamp.
        existing = self._revision(view_result.generation_id, digest_value)
        if existing is not None and _is_identical_revision(
            existing, view_result, view_digest
        ):
            return existing

        con = self._connect()
        try:
            con.execute("BEGIN")
            _write_revision(
                con,
                view_result,
                digest_value,
                lifecycle.value,
                view_digest,
                now,
            )
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ViewRepositoryError(
                f"save_view_revision failed (view/lineage constraint): {exc}"
            ) from exc
        finally:
            con.close()
        return ViewRevision(
            generation_id=view_result.generation_id,
            policy_digest=digest_value,
            builder_version=view_result.builder_version,
            view_digest=view_digest,
            view_count=len(view_result.views),
            saved_at=now,
        )

    def mark_stale(
        self, generation_id: str, policy_digest_value: str, *, reason: str = ""
    ) -> None:
        """Mark every view of one revision stale (e.g. evidence drifted)."""
        con = self._connect()
        try:
            con.execute(
                "UPDATE ce_view_headers SET lifecycle=? "
                "WHERE generation_id=? AND policy_digest=?",
                (ViewLifecycle.STALE.value, generation_id, policy_digest_value),
            )
            con.commit()
        finally:
            con.close()

    def lifecycle_status(
        self, generation_id: str, policy_digest_value: str
    ) -> ViewLifecycle:
        """Current lifecycle of a revision (defaults to active)."""
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT lifecycle FROM ce_view_headers "
                "WHERE generation_id=? AND policy_digest=? LIMIT 1",
                (generation_id, policy_digest_value),
            ).fetchone()
            return ViewLifecycle(row[0]) if row else ViewLifecycle.ACTIVE
        finally:
            con.close()

    # ------------------------------------------------------------ reads

    def read_views(
        self, generation_id: str, policy_digest_value: str
    ) -> list[dict]:
        """Read the full view set of one revision (headers + membership)."""
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "SELECT view_id, view_type, builder_version, session_id, "
                "fidelity_json, metadata_json, lifecycle, view_digest "
                "FROM ce_view_headers "
                "WHERE generation_id=? AND policy_digest=? "
                "ORDER BY view_id",
                (generation_id, policy_digest_value),
            ).fetchall()
            return [
                _materialize_view(con, row, generation_id, policy_digest_value)
                for row in rows
            ]
        finally:
            con.close()

    def policy_revisions(self, generation_id: str) -> list[ViewRevision]:
        """Distinct persisted policy revisions for one generation."""
        con = self._connect(readonly=True)
        try:
            return [
                ViewRevision(
                    generation_id=r["generation_id"],
                    policy_digest=r["policy_digest"],
                    builder_version=r["builder_version"],
                    view_digest=r["view_digest"],
                    view_count=r["view_count"],
                    saved_at=r["saved_at"],
                )
                for r in con.execute(
                    "SELECT generation_id, policy_digest, builder_version, "
                    "view_digest, view_count, saved_at FROM ce_view_revisions "
                    "WHERE generation_id=? ORDER BY saved_at, policy_digest",
                    (generation_id,),
                )
            ]
        finally:
            con.close()

    def _revision(
        self, generation_id: str, policy_digest_value: str
    ) -> ViewRevision | None:
        """The persisted revision row for one (generation, policy)."""
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT generation_id, policy_digest, builder_version, "
                "view_digest, view_count, saved_at FROM ce_view_revisions "
                "WHERE generation_id=? AND policy_digest=?",
                (generation_id, policy_digest_value),
            ).fetchone()
            if not row:
                return None
            return ViewRevision(
                generation_id=row["generation_id"],
                policy_digest=row["policy_digest"],
                builder_version=row["builder_version"],
                view_digest=row["view_digest"],
                view_count=row["view_count"],
                saved_at=row["saved_at"],
            )
        finally:
            con.close()

    def view_digest(
        self, generation_id: str, policy_digest_value: str
    ) -> str | None:
        """Deterministic view-set digest of one revision (stable across
        identical rebuilds)."""
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT view_digest FROM ce_view_revisions "
                "WHERE generation_id=? AND policy_digest=?",
                (generation_id, policy_digest_value),
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def resolve_lineage(
        self, generation_id: str, view_id: str, policy_digest_value: str
    ) -> dict:
        """Resolve one view to its members, lineage and contradiction slots."""
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT view_id, view_type, session_id, fidelity_json, "
                "metadata_json, lifecycle "
                "FROM ce_view_headers "
                "WHERE generation_id=? AND policy_digest=? AND view_id=?",
                (generation_id, policy_digest_value, view_id),
            ).fetchone()
            if not row:
                raise ViewRepositoryError(
                    f"view {view_id} not found in generation {generation_id}"
                )
            members = [
                r[0]
                for r in con.execute(
                    "SELECT event_id FROM ce_view_members "
                    "WHERE generation_id=? AND policy_digest=? AND view_id=? "
                    "ORDER BY event_id",
                    (generation_id, policy_digest_value, view_id),
                )
            ]
            lineage = [
                r[0]
                for r in con.execute(
                    "SELECT lineage_ref FROM ce_view_lineage "
                    "WHERE generation_id=? AND policy_digest=? AND view_id=? "
                    "ORDER BY lineage_ref",
                    (generation_id, policy_digest_value, view_id),
                )
            ]
            slots = [
                {
                    "slot_id": r["slot_id"],
                    "kind": r["kind"],
                    "refs": json.loads(r["refs_json"]),
                }
                for r in con.execute(
                    "SELECT slot_id, kind, refs_json FROM ce_view_contradictions "
                    "WHERE generation_id=? AND policy_digest=? AND view_id=? "
                    "ORDER BY slot_id",
                    (generation_id, policy_digest_value, view_id),
                )
            ]
            return {
                "view_id": row["view_id"],
                "view_type": row["view_type"],
                "session_id": row["session_id"],
                "members": members,
                "lineage": lineage,
                "evidence_event_refs": members,
                "contradictions": slots,
                "fidelity": json.loads(row["fidelity_json"]),
                "metadata": json.loads(row["metadata_json"]),
                "lifecycle": row["lifecycle"],
            }
        finally:
            con.close()


# ------------------------------------------------------------- helpers

def _is_identical_revision(
    existing: ViewRevision, view_result: ViewBuildResult, view_digest: str
) -> bool:
    """True when a persisted revision matches this exact rebuild output."""
    return (
        existing.builder_version == view_result.builder_version
        and existing.view_digest == view_digest
        and existing.view_count == len(view_result.views)
    )


def _write_revision(
    con: sqlite3.Connection,
    view_result: ViewBuildResult,
    policy_digest_value: str,
    lifecycle: str,
    view_digest: str,
    now: str,
) -> None:
    """Replace every row of one (generation, policy) view revision atomically."""
    # DELETE replaces the whole revision so a rebuilt view set always reflects
    # the current evidence for this generation under this policy digest.
    for table in (
        "ce_view_contradictions",
        "ce_view_lineage",
        "ce_view_members",
        "ce_view_headers",
    ):
        con.execute(
            f"DELETE FROM {table} "
            "WHERE generation_id=? AND policy_digest=?",
            (view_result.generation_id, policy_digest_value),
        )
    for view in view_result.views:
        _insert_view(
            con,
            view_result.generation_id,
            policy_digest_value,
            view,
            lifecycle,
            view_digest,
            now,
        )
    con.execute(
        "INSERT OR REPLACE INTO ce_view_revisions "
        "(generation_id, policy_digest, builder_version, view_digest, "
        " view_count, saved_at) VALUES (?,?,?,?,?,?)",
        (
            view_result.generation_id,
            policy_digest_value,
            view_result.builder_version,
            view_digest,
            len(view_result.views),
            now,
        ),
    )


def _materialize_view(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    generation_id: str,
    policy_digest_value: str,
) -> dict:
    """Hydrate one header row with membership, lineage and contradictions."""
    members = [
        r[0]
        for r in con.execute(
            "SELECT event_id FROM ce_view_members "
            "WHERE generation_id=? AND policy_digest=? AND view_id=? "
            "ORDER BY event_id",
            (generation_id, policy_digest_value, row["view_id"]),
        )
    ]
    lineage = [
        r[0]
        for r in con.execute(
            "SELECT lineage_ref FROM ce_view_lineage "
            "WHERE generation_id=? AND policy_digest=? AND view_id=? "
            "ORDER BY lineage_ref",
            (generation_id, policy_digest_value, row["view_id"]),
        )
    ]
    slots = [
        {
            "slot_id": r["slot_id"],
            "kind": r["kind"],
            "refs": json.loads(r["refs_json"]),
        }
        for r in con.execute(
            "SELECT slot_id, kind, refs_json FROM ce_view_contradictions "
            "WHERE generation_id=? AND policy_digest=? AND view_id=? "
            "ORDER BY slot_id",
            (generation_id, policy_digest_value, row["view_id"]),
        )
    ]
    return {
        "view_id": row["view_id"],
        "view_type": row["view_type"],
        "builder_version": row["builder_version"],
        "session_id": row["session_id"],
        "members": members,
        "lineage": lineage,
        "contradictions": slots,
        "fidelity": json.loads(row["fidelity_json"]),
        "metadata": json.loads(row["metadata_json"]),
        "lifecycle": row["lifecycle"],
        "view_digest": row["view_digest"],
        "evidence_event_refs": members,
    }


def _insert_view(
    con: sqlite3.Connection,
    generation_id: str,
    policy_digest_value: str,
    view: DerivedView,
    lifecycle: str,
    view_digest: str,
    now: str,
) -> None:
    con.execute(
        "INSERT INTO ce_view_headers "
        "(revision_id, generation_id, policy_digest, view_id, view_type, "
        " builder_version, session_id, fidelity_json, metadata_json, "
        " lifecycle, view_digest, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _revision_id(generation_id, policy_digest_value, view.view_id),
            generation_id,
            policy_digest_value,
            view.view_id,
            view.view_type.value,
            view.builder_version,
            view.session_id,
            json.dumps(view.fidelity.to_dict(), sort_keys=True),
            json.dumps(dict(view.metadata), sort_keys=True),
            lifecycle,
            view_digest,
            now,
        ),
    )
    for event_id in view.members:
        con.execute(
            "INSERT OR IGNORE INTO ce_view_members "
            "(generation_id, policy_digest, view_id, event_id) VALUES (?,?,?,?)",
            (generation_id, policy_digest_value, view.view_id, event_id),
        )
    for ref in view.lineage:
        con.execute(
            "INSERT OR IGNORE INTO ce_view_lineage "
            "(generation_id, policy_digest, view_id, lineage_ref) VALUES (?,?,?,?)",
            (generation_id, policy_digest_value, view.view_id, ref),
        )
    for slot in view.contradictions:
        con.execute(
            "INSERT OR IGNORE INTO ce_view_contradictions "
            "(generation_id, policy_digest, view_id, slot_id, kind, refs_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                generation_id,
                policy_digest_value,
                view.view_id,
                slot.slot_id,
                slot.kind,
                json.dumps(list(slot.refs), sort_keys=True),
            ),
        )


def _revision_id(
    generation_id: str, policy_digest_value: str, view_id: str
) -> str:
    payload = "|".join([generation_id, policy_digest_value, view_id])
    return "rev:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generation_exists(db: Path, generation_id: str) -> bool:
    if not db.exists():
        return False
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT 1 FROM ce_event_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        return row is not None
    finally:
        con.close()


__all__ = [
    "VIEW_TABLES",
    "ViewLifecycle",
    "ViewRepository",
    "ViewRepositoryError",
    "ViewRevision",
]
