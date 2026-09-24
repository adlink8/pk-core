"""Immutable metadata store for Wiki materialized projections.

The store is disposable and is not an authority.  It keeps the immutable
version/dependency/invalidation metadata tables and (since Phase 4) an
additive ``wiki_projection_pages`` table holding deterministic, consolidated
page bodies (aggregated claims + evidence refs only — never raw conversation
or source body text).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "personal_wiki_derived_store_v1"


@dataclass(frozen=True, slots=True)
class ProjectionVersion:
    topic_id: str
    topic_type: str
    projection_format_version: str
    projection_version: str
    projection_checksum: str
    generated_at: str
    freshness_status: str
    reason_codes: tuple[str, ...]
    snapshot_bindings: Mapping[str, Any]
    dependency_manifest_checksum: str


@dataclass(frozen=True, slots=True)
class ProjectionDependency:
    authority: str
    stable_ref: str
    expected_version: str | None = None
    expected_checksum: str | None = None
    expected_sequence: int | None = None
    essential: bool = True
    order_key: str = ""

    def canonical(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "stable_ref": self.stable_ref,
            "expected_version": self.expected_version,
            "expected_checksum": self.expected_checksum,
            "expected_sequence": self.expected_sequence,
            "essential": self.essential,
            "order_key": self.order_key or f"{self.authority}:{self.stable_ref}",
        }


@dataclass(frozen=True, slots=True)
class ProjectionPage:
    """A deterministic, consolidated wiki page body bound to a projection version.

    ``page_body`` is a JSON string containing only aggregated result content
    (subject / claims / evidence refs).  It never carries raw conversation text,
    source bodies, or provider output.  ``freshness_status`` mirrors the bound
    version's status so page readers can annotate stale pages without a second
    query.
    """

    topic_id: str
    topic_type: str
    projection_version: str
    page_body: str
    page_checksum: str
    generated_at: str
    snapshot_bindings: Mapping[str, Any]
    freshness_status: str | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _assert_derived_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() not in {".sqlite", ".db"}:
        raise ValueError("derived_store_path_invalid")
    return resolved


def connect_rw(path: Path | str) -> sqlite3.Connection:
    """Open the dedicated derived store for schema/transaction writes only."""
    target = _assert_derived_path(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(con)
    return con


def connect_ro(path: Path | str) -> sqlite3.Connection:
    """Open the derived store read-only with SQLite write protection enabled."""
    target = _assert_derived_path(Path(path))
    if not target.exists():
        raise FileNotFoundError(target)
    con = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS wiki_projection_versions (
            topic_id TEXT NOT NULL,
            topic_type TEXT NOT NULL,
            projection_format_version TEXT NOT NULL,
            projection_version TEXT NOT NULL,
            projection_checksum TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            freshness_status TEXT NOT NULL,
            reason_codes_json TEXT NOT NULL,
            snapshot_bindings_json TEXT NOT NULL,
            dependency_manifest_checksum TEXT NOT NULL,
            PRIMARY KEY (topic_id, projection_version)
        );
        CREATE TABLE IF NOT EXISTS wiki_projection_dependencies (
            topic_id TEXT NOT NULL,
            projection_version TEXT NOT NULL,
            authority TEXT NOT NULL,
            stable_ref TEXT NOT NULL,
            expected_version TEXT,
            expected_checksum TEXT,
            expected_sequence INTEGER,
            essential INTEGER NOT NULL,
            order_key TEXT NOT NULL,
            PRIMARY KEY (topic_id, projection_version, authority, stable_ref),
            FOREIGN KEY (topic_id, projection_version)
                REFERENCES wiki_projection_versions(topic_id, projection_version)
        );
        CREATE TABLE IF NOT EXISTS wiki_projection_invalidations (
            invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id TEXT NOT NULL,
            projection_version TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wiki_projection_pages (
            topic_id TEXT NOT NULL,
            topic_type TEXT NOT NULL,
            projection_version TEXT NOT NULL,
            page_body TEXT NOT NULL,
            page_checksum TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            snapshot_bindings_json TEXT NOT NULL,
            PRIMARY KEY (topic_id, projection_version)
        );
        """
    )
    con.commit()


def insert_version(
    con: sqlite3.Connection,
    version: ProjectionVersion,
    dependencies: Iterable[ProjectionDependency],
) -> None:
    """Insert an immutable version and its complete dependency manifest."""
    deps = tuple(dependencies)
    ordered = tuple(sorted(deps, key=lambda item: (item.order_key or f"{item.authority}:{item.stable_ref}", item.authority, item.stable_ref)))
    try:
        with con:
            con.execute(
                """INSERT INTO wiki_projection_versions
                (topic_id,topic_type,projection_format_version,projection_version,
                 projection_checksum,generated_at,freshness_status,reason_codes_json,
                 snapshot_bindings_json,dependency_manifest_checksum)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    version.topic_id, version.topic_type, version.projection_format_version,
                    version.projection_version, version.projection_checksum, version.generated_at,
                    version.freshness_status, _json(list(version.reason_codes)),
                    _json(dict(version.snapshot_bindings)), version.dependency_manifest_checksum,
                ),
            )
            con.executemany(
                """INSERT INTO wiki_projection_dependencies
                (topic_id,projection_version,authority,stable_ref,expected_version,
                 expected_checksum,expected_sequence,essential,order_key)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        version.topic_id, version.projection_version, item.authority,
                        item.stable_ref, item.expected_version, item.expected_checksum,
                        item.expected_sequence, int(item.essential),
                        item.order_key or f"{item.authority}:{item.stable_ref}",
                    )
                    for item in ordered
                ],
            )
    except sqlite3.IntegrityError:
        raise ValueError("projection_version_immutable") from None


def _row_to_version(row: sqlite3.Row) -> ProjectionVersion:
    return ProjectionVersion(
        topic_id=str(row["topic_id"]), topic_type=str(row["topic_type"]),
        projection_format_version=str(row["projection_format_version"]),
        projection_version=str(row["projection_version"]), projection_checksum=str(row["projection_checksum"]),
        generated_at=str(row["generated_at"]), freshness_status=str(row["freshness_status"]),
        reason_codes=tuple(json.loads(row["reason_codes_json"])),
        snapshot_bindings=json.loads(row["snapshot_bindings_json"]),
        dependency_manifest_checksum=str(row["dependency_manifest_checksum"]),
    )


def _row_to_page(row: sqlite3.Row) -> ProjectionPage:
    return ProjectionPage(
        topic_id=str(row["topic_id"]), topic_type=str(row["topic_type"]),
        projection_version=str(row["projection_version"]), page_body=str(row["page_body"]),
        page_checksum=str(row["page_checksum"]), generated_at=str(row["generated_at"]),
        snapshot_bindings=json.loads(row["snapshot_bindings_json"]),
        freshness_status=str(row["freshness_status"]) if row["freshness_status"] is not None else None,
    )


def insert_page(con: sqlite3.Connection, page: ProjectionPage) -> None:
    """Insert an immutable consolidated page body bound to a version row.

    ``INSERT OR REPLACE`` keeps consolidation idempotent: re-running with the
    same input yields the same ``page_checksum`` and the write is a no-op at
    the row level.  The version row must already exist (FK is intentionally
    not enforced at the SQL layer so a missing version degrades to read-time
    compute rather than a hard failure).
    """
    con.execute(
        """INSERT OR REPLACE INTO wiki_projection_pages
        (topic_id,topic_type,projection_version,page_body,page_checksum,generated_at,snapshot_bindings_json)
        VALUES (?,?,?,?,?,?,?)""",
        (
            page.topic_id, page.topic_type, page.projection_version, page.page_body,
            page.page_checksum, page.generated_at, _json(dict(page.snapshot_bindings)),
        ),
    )
    con.commit()


def latest_page(path: Path | str, topic_id: str) -> ProjectionPage | None:
    """Return the page body for the newest projection version of a topic.

    Returns ``None`` when the store is missing, the topic has no version, or
    the topic has no consolidated page body yet — every failure mode is
    intended to degrade to read-time compute.
    """
    try:
        con = connect_ro(path)
    except (FileNotFoundError, OSError):
        return None
    try:
        row = con.execute(
            """SELECT p.topic_id,p.topic_type,p.projection_version,p.page_body,
                      p.page_checksum,p.generated_at,p.snapshot_bindings_json,
                      v.freshness_status
               FROM wiki_projection_pages p
               JOIN wiki_projection_versions v
                 ON v.topic_id=p.topic_id AND v.projection_version=p.projection_version
               WHERE p.topic_id=?
               ORDER BY v.generated_at DESC, p.projection_version DESC LIMIT 1""",
            (topic_id,),
        ).fetchone()
        return _row_to_page(row) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def list_pages(path: Path | str, *, limit: int = 500) -> list[ProjectionPage]:
    """Return the newest consolidated page for every topic that has one.

    Read-only and fail-safe: a missing store yields an empty list.
    """
    try:
        con = connect_ro(path)
    except (FileNotFoundError, OSError):
        return []
    try:
        rows = con.execute(
            """SELECT p.topic_id,p.topic_type,p.projection_version,p.page_body,
                      p.page_checksum,p.generated_at,p.snapshot_bindings_json,
                      v.freshness_status
               FROM wiki_projection_pages p
               JOIN wiki_projection_versions v
                 ON v.topic_id=p.topic_id AND v.projection_version=p.projection_version
               JOIN (
                   SELECT topic_id, MAX(generated_at) AS latest_generated_at
                   FROM wiki_projection_pages GROUP BY topic_id
               ) latest ON latest.topic_id=p.topic_id
               WHERE p.generated_at=latest.latest_generated_at
               ORDER BY p.topic_id LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [_row_to_page(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def latest_version(path: Path | str, topic_id: str) -> tuple[ProjectionVersion | None, tuple[ProjectionDependency, ...]]:
    con = connect_ro(path)
    try:
        row = con.execute(
            """SELECT * FROM wiki_projection_versions
               WHERE topic_id=? ORDER BY generated_at DESC, projection_version DESC LIMIT 1""",
            (topic_id,),
        ).fetchone()
        if row is None:
            return None, ()
        version = _row_to_version(row)
        dependencies = tuple(
            ProjectionDependency(
                authority=str(item["authority"]), stable_ref=str(item["stable_ref"]),
                expected_version=item["expected_version"], expected_checksum=item["expected_checksum"],
                expected_sequence=item["expected_sequence"], essential=bool(item["essential"]),
                order_key=str(item["order_key"]),
            )
            for item in con.execute(
                """SELECT * FROM wiki_projection_dependencies
                   WHERE topic_id=? AND projection_version=? ORDER BY order_key,authority,stable_ref""",
                (version.topic_id, version.projection_version),
            ).fetchall()
        )
        return version, dependencies
    finally:
        con.close()


__all__ = [
    "ProjectionDependency", "ProjectionPage", "ProjectionVersion", "SCHEMA_VERSION",
    "connect_ro", "connect_rw", "insert_page", "insert_version", "latest_page",
    "latest_version", "list_pages",
]
