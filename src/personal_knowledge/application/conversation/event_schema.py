"""Phase 62: generation-bound canonical v2 event schema (additive, versioned).

Adds cohesive v2 event-authority tables beside the existing compatibility
tables (Phase 62 CONTEXT D-16/D-19). The legacy ``canonical_sessions`` /
``canonical_messages`` / ``canonical_tool_events`` tables are untouched by this
migration; they remain readable as compatibility projections.

Every v2 table is generation-bound: the composite primary key carries
``generation_id`` so different generations are fully isolated, replay within a
generation is idempotent, and relations cannot cross generations (foreign keys
bind relation endpoints to events of the same generation).

Tables:
  - ``ce_source_artifacts``      — content-addressed immutable artifacts (D-05)
  - ``ce_adapter_runs``          — one run = one adapted source set
  - ``ce_event_generations``     — staged/validated generation header
  - ``ce_sessions``              — adapted sessions with provenance/fidelity
  - ``ce_events``                — typed semantic events (D-10/D-11)
  - ``ce_event_relations``       — first-class relations (D-12)
  - ``ce_field_dispositions``    — explicit field mapping decisions (D-07)
  - ``ce_generation_authority``  — active-generation pointer (read-only here;
                                  activation owned by a later orchestration plan)
  - ``ce_live_slots``            — stable per-source-slot registry: ``slot_id``
                                  IS the slot ``artifact_id``
                                  (``sha256("art|<family>|<mirror_path>")``),
                                  unique per (family, mirror_path) and constant
                                  across content edits (Milestone 1, additive)
  - ``ce_live_sync_log``         — append-only log of live in-place applies
  - ``ce_live_state``            — singleton live-sync coordination state
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "v2.1.0"

V2_TABLES = (
    "ce_source_artifacts",
    "ce_adapter_runs",
    "ce_event_generations",
    "ce_sessions",
    "ce_events",
    "ce_event_relations",
    "ce_field_dispositions",
    "ce_generation_authority",
    "ce_schema_meta",
    "ce_live_slots",
    "ce_live_sync_log",
    "ce_live_state",
)

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ce_schema_meta (
        schema_version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_source_artifacts (
        artifact_id        TEXT PRIMARY KEY,
        family             TEXT NOT NULL,
        source_kind        TEXT NOT NULL,
        content_hash       TEXT NOT NULL,
        capture_method     TEXT NOT NULL,
        relative_path      TEXT NOT NULL,
        byte_size          INTEGER NOT NULL,
        schema_digest      TEXT,
        privacy_dispositions TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_event_generations (
        generation_id  TEXT PRIMARY KEY,
        status         TEXT NOT NULL CHECK(status IN ('staged','validated')),
        source_manifest_id TEXT,
        dataset_digest TEXT,
        created_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_adapter_runs (
        run_id             TEXT PRIMARY KEY,
        generation_id      TEXT NOT NULL REFERENCES ce_event_generations(generation_id),
        family             TEXT NOT NULL,
        adapter_version    TEXT NOT NULL,
        contract_version   TEXT NOT NULL,
        capability_digest  TEXT NOT NULL,
        warnings           TEXT,
        created_at         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_sessions (
        generation_id      TEXT NOT NULL REFERENCES ce_event_generations(generation_id),
        session_id         TEXT NOT NULL,
        family             TEXT NOT NULL,
        native_session_id  TEXT,
        started_at         TEXT,
        ended_at           TEXT,
        artifact_id        TEXT NOT NULL REFERENCES ce_source_artifacts(artifact_id),
        native_locator     TEXT NOT NULL,
        contract_version   TEXT NOT NULL,
        fidelity_json      TEXT NOT NULL,
        cwd                TEXT,
        git_branch         TEXT,
        model              TEXT,
        title              TEXT,
        stop_reason        TEXT,
        PRIMARY KEY (generation_id, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_events (
        generation_id      TEXT NOT NULL REFERENCES ce_event_generations(generation_id),
        event_id           TEXT NOT NULL,
        session_id         TEXT NOT NULL,
        kind               TEXT NOT NULL,
        artifact_id        TEXT NOT NULL,
        native_locator     TEXT NOT NULL,
        native_event_id    TEXT,
        occurred_at        TEXT,
        ordinal            INTEGER,
        native_payload_ref TEXT,
        content            TEXT,
        summary            TEXT,
        contract_version   TEXT NOT NULL,
        fidelity_json      TEXT NOT NULL,
        PRIMARY KEY (generation_id, event_id),
        FOREIGN KEY (generation_id, session_id)
            REFERENCES ce_sessions(generation_id, session_id),
        FOREIGN KEY (artifact_id)
            REFERENCES ce_source_artifacts(artifact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_event_relations (
        generation_id   TEXT NOT NULL REFERENCES ce_event_generations(generation_id),
        relation_id     TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        target_event_id TEXT NOT NULL,
        relation_kind   TEXT NOT NULL,
        PRIMARY KEY (generation_id, relation_id),
        FOREIGN KEY (generation_id, source_event_id)
            REFERENCES ce_events(generation_id, event_id),
        FOREIGN KEY (generation_id, target_event_id)
            REFERENCES ce_events(generation_id, event_id),
        CHECK (source_event_id != target_event_id),
        UNIQUE (generation_id, source_event_id, target_event_id, relation_kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_field_dispositions (
        generation_id TEXT NOT NULL REFERENCES ce_event_generations(generation_id),
        event_id      TEXT NOT NULL,
        field_name    TEXT NOT NULL,
        disposition   TEXT NOT NULL,
        reason        TEXT,
        PRIMARY KEY (generation_id, event_id, field_name),
        FOREIGN KEY (generation_id, event_id)
            REFERENCES ce_events(generation_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_generation_authority (
        generation_id TEXT PRIMARY KEY,
        active        INTEGER NOT NULL DEFAULT 0,
        updated_at    TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ce_sessions_generation_family
        ON ce_sessions(generation_id, family, session_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ce_events_generation_session
        ON ce_events(generation_id, session_id, ordinal, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ce_relations_generation_source
        ON ce_event_relations(generation_id, source_event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ce_dispositions_generation_event
        ON ce_field_dispositions(generation_id, event_id)
    """,
    # ---- Live-sync overlay (Milestone 1) ---------------------------------
    # Additive only. ``artifact_id`` is now a *slot* identity derived from
    # ``sha256("art|" + family + "|" + mirror_path)``
    # (snapshots.make_slot_artifact_id), constant across content edits, while
    # ``content_hash`` keeps holding ``sha256(bytes)``. A slot therefore always
    # has exactly one *current* content hash, and the event/session/relation rows
    # that belong to a (generation, artifact) are indexed so a live rewrite can
    # locate and replace them without rescanning the whole generation.
    # ``UNIQUE(family, mirror_path)`` is what makes the slot key unambiguous:
    # the same mirror path must never map to two slots.
    """
    CREATE TABLE IF NOT EXISTS ce_live_slots (
        slot_id        TEXT PRIMARY KEY,
        family         TEXT NOT NULL,
        mirror_path    TEXT NOT NULL,
        content_hash   TEXT,
        byte_size      INTEGER,
        first_seen_at  TEXT NOT NULL,
        last_synced_at TEXT,
        active         INTEGER NOT NULL DEFAULT 1,
        UNIQUE(family, mirror_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_live_sync_log (
        sync_id        TEXT PRIMARY KEY,
        started_at     TEXT NOT NULL,
        finished_at    TEXT,
        n_added        INTEGER,
        n_changed      INTEGER,
        n_removed      INTEGER,
        rows_pruned    INTEGER,
        rows_inserted  INTEGER,
        per_family     TEXT,
        status         TEXT NOT NULL,
        detail         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_live_state (
        key         TEXT PRIMARY KEY,
        value       TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ce_events_gen_art
        ON ce_events(generation_id, artifact_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ce_sessions_gen_art
        ON ce_sessions(generation_id, artifact_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ce_rel_gen_target
        ON ce_event_relations(generation_id, target_event_id)
    """,
)


def create_v2_schema(db: Path) -> None:
    """Apply the additive v2 DDL and idempotent column migrations."""
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        for statement in _DDL:
            con.execute(statement)
        event_columns = {
            row[1] for row in con.execute("PRAGMA table_info(ce_events)")
        }
        if "content" not in event_columns:
            con.execute("ALTER TABLE ce_events ADD COLUMN content TEXT")
        session_columns = {
            row[1] for row in con.execute("PRAGMA table_info(ce_sessions)")
        }
        for column in ("cwd", "git_branch", "model", "title", "stop_reason"):
            if column not in session_columns:
                con.execute(
                    f"ALTER TABLE ce_sessions ADD COLUMN {column} TEXT"
                )
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT OR IGNORE INTO ce_schema_meta (schema_version, applied_at) "
            "VALUES (?, ?)",
            (SCHEMA_VERSION, now),
        )
        con.commit()
    finally:
        con.close()


def v2_table_names(con: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if r[0] in V2_TABLES
    }
