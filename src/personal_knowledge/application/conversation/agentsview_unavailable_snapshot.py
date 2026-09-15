"""Private, privacy-filtered AgentsView snapshot for unavailable native rows."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import chatgpt
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.adapters.conversation_sources.snapshots import (
    CaptureError,
    capture_file,
)
from personal_knowledge.core.project_paths import VAR_TMP


_CAPTURE_TEMP_ROOT = VAR_TMP / "conversation-capture"


def capture_pathless_agent_snapshot(
    source: Path,
    dest: Path,
    *,
    family: str,
    session_ids: tuple[str, ...] | None = None,
    byte_limit: int = 2_000_000_000,
) -> tuple[SourceArtifact, Path]:
    """Publish only declared unavailable-native rows for one agent family.

    The source is held in one read-only SQLite transaction while the filtered
    database is built. Staging lives under the project's private ``var/tmp``
    tree, never under the system drive or artifact store, and the whole
    directory (including SQLite sidecars) is removed before this function
    returns or raises. When
    ``session_ids`` is supplied, it is the exact inventory-derived set whose
    native locators could not be resolved; the legacy default remains strict
    NULL/blank ``file_path`` selection for the ChatGPT wrapper.
    """

    if family not in {"chatgpt", "grok"}:
        raise CaptureError(f"unsupported pathless observation family {family!r}")
    try:
        _CAPTURE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"pk-{family}-filtered-", dir=_CAPTURE_TEMP_ROOT,
        ) as temp_dir:
            filtered = Path(temp_dir) / f"{family}.sqlite"
            return _build_and_publish(
                source, filtered, dest, family=family,
                session_ids=session_ids, byte_limit=byte_limit,
            )
    except CaptureError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise CaptureError(
            f"{family} row-filtered capture failed: {exc}"
        ) from exc


def _build_and_publish(
    source: Path,
    filtered: Path,
    dest: Path,
    *,
    family: str,
    session_ids: tuple[str, ...] | None,
    byte_limit: int,
) -> tuple[SourceArtifact, Path]:
    snapshot = sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro", uri=True
    )
    target = sqlite3.connect(str(filtered))
    try:
        snapshot.execute("PRAGMA query_only=ON")
        snapshot.execute("BEGIN")
        present_tables, present_columns = _create_filtered_schema(
            snapshot, target, family=family
        )
        _copy_filtered_rows(
            snapshot, target, family=family, session_ids=session_ids
        )
        schema_digest = _finalize_filtered_db(target, family=family)
        target.close()
        target = None

        artifact, blob = capture_file(
            filtered,
            dest,
            relative_path=f"sqlite:{source.name}",
            byte_limit=byte_limit,
            count_limit=1,
            family=family,
            mirror_path=f"sqlite:{source.name}",
        )
        return replace(
            artifact,
            source_kind="sqlite",
            capture_method="sqlite_readonly_transaction_row_filter",
            schema_digest=schema_digest,
            privacy_dispositions=_privacy_dispositions(
                present_tables, present_columns, family=family,
                inventory_selected=session_ids is not None,
            ),
        ), blob
    finally:
        if target is not None:
            target.close()
        if snapshot.in_transaction:
            snapshot.rollback()
        snapshot.close()


def _create_filtered_schema(
    snapshot: sqlite3.Connection, target: sqlite3.Connection, *, family: str,
) -> tuple[set[str], dict[str, set[str]]]:
    present_tables = {
        row[0]
        for row in snapshot.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = set(chatgpt.LIVE_ALLOWED_TABLES) - present_tables
    if missing_tables:
        raise CaptureError(
            f"{family} declared tables missing: {sorted(missing_tables)}"
        )
    target.execute("PRAGMA journal_mode=DELETE")
    present_columns: dict[str, set[str]] = {}
    for table in chatgpt.LIVE_ALLOWED_TABLES:
        info_rows = snapshot.execute(f'PRAGMA table_info("{table}")').fetchall()
        info = {row[1]: (row[2] or "BLOB") for row in info_rows}
        present_columns[table] = set(info)
        declared = chatgpt.LIVE_ALLOWED_COLUMNS[table]
        missing_columns = set(declared) - set(info)
        if missing_columns:
            raise CaptureError(
                f"{family} declared columns missing for {table}: "
                f"{sorted(missing_columns)}"
            )
        column_defs = ",".join(
            f'"{column}" {info[column]}' for column in declared
        )
        target.execute(f'CREATE TABLE "{table}" ({column_defs})')
    return present_tables, present_columns


def _copy_filtered_rows(
    snapshot: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    family: str,
    session_ids: tuple[str, ...] | None,
) -> None:
    session_columns = chatgpt.LIVE_ALLOWED_COLUMNS["sessions"]
    session_sql = ",".join(f'"{column}"' for column in session_columns)
    message_columns = chatgpt.LIVE_ALLOWED_COLUMNS["messages"]
    message_sql = ",".join(f'm."{column}"' for column in message_columns)
    selections: tuple[tuple[str, ...] | None, ...]
    if session_ids is None:
        selections = (None,)
    else:
        selected = tuple(sorted(set(session_ids)))
        selections = tuple(
            selected[index:index + 400] for index in range(0, len(selected), 400)
        )
    for selected_ids in selections:
        predicate, params = _selection_predicate(
            family=family, session_ids=selected_ids, alias=""
        )
        session_rows = snapshot.execute(
            f'SELECT {session_sql} FROM "sessions" WHERE {predicate} ORDER BY id',
            params,
        )
        _copy_rows(target, "sessions", session_columns, session_rows)

        message_predicate, message_params = _selection_predicate(
            family=family, session_ids=selected_ids, alias="s."
        )
        message_rows = snapshot.execute(
            f'SELECT {message_sql} FROM "messages" m '
            'JOIN "sessions" s ON s.id=m.session_id '
            f"WHERE {message_predicate} ORDER BY m.session_id, m.ordinal, m.id",
            message_params,
        )
        _copy_rows(target, "messages", message_columns, message_rows)


def _selection_predicate(
    *, family: str, session_ids: tuple[str, ...] | None, alias: str,
) -> tuple[str, tuple[str, ...]]:
    prefix = f"lower({alias}agent)=? AND {alias}deleted_at IS NULL"
    if session_ids is None:
        return (
            f"{prefix} AND ({alias}file_path IS NULL "
            f"OR trim({alias}file_path)='')",
            (family,),
        )
    if not session_ids:
        return f"{prefix} AND 0", (family,)
    placeholders = ",".join("?" for _ in session_ids)
    return (
        f"{prefix} AND {alias}id IN ({placeholders})",
        (family, *session_ids),
    )


def _finalize_filtered_db(target: sqlite3.Connection, *, family: str) -> str:
    target.commit()
    target.execute("VACUUM")
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise CaptureError(
            f"{family} filtered snapshot integrity_check={integrity!r}"
        )
    schema_rows = target.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return hashlib.sha256(
        "\n;;;".join(row[0] or "" for row in schema_rows).encode("utf-8")
    ).hexdigest()[:16]


def _privacy_dispositions(
    present_tables: set[str],
    present_columns: dict[str, set[str]],
    *,
    family: str,
    inventory_selected: bool,
) -> tuple[str, ...]:
    session_filter = (
        f"excluded_rows:sessions:not_inventory_selected_unavailable_{family}"
        if inventory_selected else
        f"excluded_rows:sessions:not_pathless_active_{family}"
    )
    message_filter = (
        f"excluded_rows:messages:not_linked_to_inventory_selected_unavailable_{family}"
        if inventory_selected else
        f"excluded_rows:messages:not_linked_to_pathless_active_{family}"
    )
    return (
        *(f"excluded_table:{table}" for table in sorted(
            present_tables - set(chatgpt.LIVE_ALLOWED_TABLES)
        )),
        *(f"excluded_column:{table}:{column}"
          for table in chatgpt.LIVE_ALLOWED_TABLES
          for column in sorted(
              present_columns[table]
              - set(chatgpt.LIVE_ALLOWED_COLUMNS[table])
          )),
        session_filter,
        message_filter,
    )


def _copy_rows(
    target: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    rows: sqlite3.Cursor,
) -> None:
    """Copy one allowlisted cursor in bounded batches."""

    column_sql = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    insert_sql = f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'
    while True:
        batch = rows.fetchmany(1000)
        if not batch:
            return
        target.executemany(insert_sql, batch)
