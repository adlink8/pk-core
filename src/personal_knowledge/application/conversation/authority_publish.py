import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# A gate receives the still-uncommitted connection and returns blocking reasons.
GateFn = Callable[[sqlite3.Connection], Sequence[str]]


@dataclass(frozen=True)
class PublishReport:
    published: bool
    blocked_reasons: tuple[str, ...] = ()


def _pre_publish_backup(db: Path) -> Path | None:
    # Nothing to snapshot when the authority DB does not exist yet.
    if not db.exists():
        return None
    final = db.with_name(f"{db.stem}.backup.sqlite")
    staging = final.with_name(f"{final.name}.tmp")
    source = sqlite3.connect(db, isolation_level=None)
    target = sqlite3.connect(staging, isolation_level=None)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    # os.replace so readers never observe a half-written backup.
    os.replace(staging, final)
    return final


def _discard_backup(backup_path: Path | None) -> None:
    # An unpublished snapshot is leftover garbage: the DB still holds the
    # pre-publish state, so keeping a full copy only wastes disk.
    if backup_path is not None:
        backup_path.unlink(missing_ok=True)


def authority_publish(
    db: Path,
    apply: Callable[[sqlite3.Connection], None],
    *,
    gates: Sequence[GateFn] = (),
    backup: bool = True,
) -> PublishReport:
    # Snapshot before BEGIN IMMEDIATE so the backup holds the pre-publish state.
    backup_path = _pre_publish_backup(db) if backup else None
    # isolation_level=None: the caller owns transaction control, no implicit BEGIN.
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        apply(con)
        blocked_reasons = tuple(
            reason for gate in gates for reason in gate(con)
        )
        if blocked_reasons:
            con.execute("ROLLBACK")
            # The DB is unchanged, so the snapshot is leftover garbage.
            _discard_backup(backup_path)
            return PublishReport(published=False, blocked_reasons=blocked_reasons)
        con.execute("COMMIT")
        return PublishReport(published=True)
    except BaseException:  # noqa: BLE001 - roll back, clean up, re-raise as-is
        # apply (or a gate) failed: the transaction is still open, so roll it
        # back explicitly. The caller classifies failures by the original
        # exception type, so it must propagate unwrapped.
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            # e.g. the failure already aborted the transaction; nothing to undo.
            pass
        _discard_backup(backup_path)
        raise
    finally:
        con.close()
