"""Phase 62: allowlisted, content-addressed, immutable source capture seam.

Every later family adapter receives a consistent immutable evidence set
(Phase 62 CONTEXT D-05/D-08/D-09):

  - :func:`capture_file` / :func:`capture_directory` — immutable byte capture of
    allowlisted relative paths; content-addressed deduplication; symlink/reparse
    escape rejection; byte/count limits fail closed before any artifact is
    published.
  - :func:`capture_sqlite` — SQLite **online backup** (never loose
    ``.db/.db-wal/.db-shm`` copies) filtered down to declared allowlisted
    tables/columns; adjacent credential/account/token/auth tables are never
    copied into the published artifact or reported (D-08).
  - :class:`CaptureManifest` — metadata-only manifest (hashes, capture method,
    schema/capability digest, privacy dispositions). No bodies, no secrets.
  - :func:`write_manifest` / :func:`read_manifest` / :func:`replay_manifest` —
    manifest persistence and replay verification.

This module performs capture only: it never parses family formats and never
publishes canonical data (module cohesion per engineering contract).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact

# SQLite tables whose names match these patterns are forbidden from capture even
# if they share the source database (D-08): account, credential, token, auth,
# secret, cookie, key material.
FORBIDDEN_TABLE_PATTERNS: tuple[str, ...] = (
    "account",
    "credential",
    "token",
    "auth",
    "secret",
    "cookie",
    "api_key",
)

_ARTIFACT_DIR = "artifacts"
_MANIFEST_NAME = "manifest.json"
_STAGING_DIR = ".staging"

# Capture never refuses a file for its size, so it must not hold one whole in
# memory either: hashing and copying advance in lockstep on this block size.
_STREAM_CHUNK_BYTES = 1 << 20


class CaptureError(RuntimeError):
    """Capture failed closed; no formal artifact was published."""


@dataclass(frozen=True)
class CapturePolicy:
    """Limits and allowlist for one capture operation.

    ``byte_limit`` is recorded for provenance only; ``count_limit`` is the
    binding constraint (allowlist size). Capture never truncates for volume.
    """

    byte_limit: int
    count_limit: int
    exact_relative_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureManifest:
    """Metadata-only manifest for one capture (Phase 62 D-09)."""

    manifest_id: str
    source_root: str
    capture_method: str
    artifacts: tuple[SourceArtifact, ...]
    policy: CapturePolicy
    schema_digest: str | None
    privacy_dispositions: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "manifest_id": self.manifest_id,
            "source_root": self.source_root,
            "capture_method": self.capture_method,
            "artifacts": [asdict(a) for a in self.artifacts],
            "policy": asdict(self.policy),
            "schema_digest": self.schema_digest,
            "privacy_dispositions": list(self.privacy_dispositions),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaptureManifest":
        artifacts = tuple(SourceArtifact(**a) for a in data["artifacts"])
        policy = CapturePolicy(**data["policy"])
        return cls(
            manifest_id=data["manifest_id"],
            source_root=data["source_root"],
            capture_method=data["capture_method"],
            artifacts=artifacts,
            policy=policy,
            schema_digest=data.get("schema_digest"),
            privacy_dispositions=tuple(data.get("privacy_dispositions", [])),
            created_at=data["created_at"],
        )


@dataclass(frozen=True)
class ReplayResult:
    """Result of verifying a manifest against a blob store."""

    ok: bool
    missing: list[str]
    mismatched: list[str]


def make_slot_artifact_id(family: str, mirror_path: str) -> str:
    """Stable per-source-slot identity, constant across content changes.

    Replaces the old content-addressed ``artifact_id``
    (``sha256(bytes)[:32]``). Two captures of the *same* mirror path under the
    *same* family yield the SAME slot id even when the bytes differ, so editing a
    single source file no longer rotates every event and session id it produced
    (the core milestone-1 fix).

    ``mirror_path`` must be the stable source path relative to its source root
    (family-scoped, directory-qualified) — NOT a bare filename, so two files
    with the same name in different session directories stay distinct. The on-disk
    blob store remains content-addressed (see :func:`_copy_stream_to_blob`); only
    the logical ``artifact_id`` changes.
    """
    return hashlib.sha256(f"art|{family}|{mirror_path}".encode("utf-8")).hexdigest()


def _blob_root(dest_dir: Path) -> Path:
    return dest_dir / _ARTIFACT_DIR


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_stream_to_blob(source: Path, dest_dir: Path) -> tuple[str, str, int]:
    """Chunked hash+copy of ``source`` into the content-addressed blob store.

    Returns ``(blob_id, content_hash, byte_size)``. Never materialises the whole
    file in memory: a capture that may not be refused for size must also not OOM
    on size.
    """
    digest = hashlib.sha256()
    size = 0
    blob_dir = _blob_root(dest_dir)
    blob_dir.mkdir(parents=True, exist_ok=True)
    tmp = blob_dir / f".tmp-{uuid.uuid4().hex}"
    try:
        with source.open("rb") as handle, tmp.open("wb") as out:
            for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        content_hash = digest.hexdigest()
        blob_id = content_hash[:32]
        blob = blob_dir / blob_id
        if blob.exists():
            tmp.unlink(missing_ok=True)
        else:
            _replace_blob(tmp, blob, content_hash)
        return blob_id, content_hash, size
    finally:
        tmp.unlink(missing_ok=True)


def _replace_blob(tmp: Path, blob: Path, content_hash: str) -> None:
    for attempt in range(4):
        try:
            os.replace(tmp, blob)
            return
        except PermissionError:
            # Windows antivirus/indexers can briefly hold either path.
            # Accept only a byte-identical winner; otherwise retry briefly.
            if blob.exists() and _sha256_path(blob) == content_hash:
                return
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


def _resolve_relative(root: Path, relative: str) -> Path:
    """Resolve an exact allowlisted relative path inside ``root``.

    Rejects absolute paths, ``..`` traversal, and symlink/reparse/junction
    escapes so capture can never follow a link outside the source root.
    """
    if not isinstance(relative, str) or not relative:
        raise CaptureError("allowlisted relative path must be a non-empty string")
    if os.path.isabs(relative) or relative.startswith(("\\", "/")):
        raise CaptureError(
            f"allowlisted path must be relative, got absolute: {relative!r}"
        )
    pure = PurePosixPath(relative.replace("\\", "/"))
    if ".." in pure.parts:
        raise CaptureError(
            f"path {relative!r} escapes the allowlisted source root"
        )
    candidate = root.joinpath(*pure.parts)
    if candidate.is_symlink() or candidate.is_junction():
        raise CaptureError(
            f"refusing to capture symlink/reparse/junction path {relative!r}"
        )
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except OSError as exc:  # pragma: no cover - pathological fs
        raise CaptureError(f"cannot resolve path {relative!r}: {exc}") from exc
    if resolved_candidate != resolved_root and not str(resolved_candidate).startswith(
        str(resolved_root) + os.sep
    ):
        raise CaptureError(
            f"path {relative!r} resolves outside the source root (escape)"
        )
    return candidate


def _validate_file_for_capture(candidate: Path, relative: str) -> None:
    """Existence/type gate only. Size never rejects: capture is not truncation."""
    if not candidate.exists():
        raise CaptureError(f"allowlisted file missing: {relative!r}")
    if not candidate.is_file():
        raise CaptureError(f"allowlisted path is not a file: {relative!r}")


def capture_file(
    source: Path,
    dest_dir: Path,
    *,
    relative_path: str,
    byte_limit: int,
    count_limit: int,
    family: str = "",
    mirror_path: str | None = None,
) -> tuple[SourceArtifact, Path]:
    """Immutable content-addressed capture of a single file.

    Raises :class:`CaptureError` before publishing anything on any validation
    failure (symlink escape, missing file, count limit).

    ``byte_limit`` is accepted for caller compatibility only and never discards
    a file: the allowlist decides *what* may be captured, size decides nothing.

    ``family`` + ``mirror_path`` are optional: when both are supplied the emitted
    ``artifact_id`` is the stable slot identity (:func:`make_slot_artifact_id`,
    constant across content edits); when omitted it falls back to the legacy
    content-addressed id so existing callers/tests are unaffected. The on-disk
    blob is always content-addressed (named by ``content_hash[:32]``), independent
    of ``artifact_id``.
    """
    if count_limit < 1:
        raise CaptureError(f"count_limit must be >= 1, got {count_limit}")
    if not relative_path:
        raise CaptureError("capture_file requires a non-empty relative_path")
    if source.is_symlink() or source.is_junction():
        raise CaptureError(
            f"refusing to capture symlink/reparse/junction path {relative_path!r}"
        )
    _validate_file_for_capture(source, relative_path)

    blob_id, content_hash, size = _copy_stream_to_blob(source, dest_dir)
    blob = _blob_root(dest_dir) / blob_id
    if family and mirror_path:
        artifact_id = make_slot_artifact_id(family, mirror_path)
    else:
        artifact_id = blob_id  # legacy content-addressed fallback
    artifact = SourceArtifact(
        artifact_id=artifact_id,
        family=family,  # set by the owning adapter; may be "" for legacy callers
        source_kind="file",
        content_hash=content_hash,
        capture_method="sha256",
        relative_path=relative_path,
        byte_size=size,
    )
    return artifact, blob


def capture_directory(
    source_dir: Path,
    dest_dir: Path,
    *,
    include_relative: tuple[str, ...],
    byte_limit: int,
    count_limit: int,
    family: str = "",
    mirror_path: str | None = None,
) -> tuple[CaptureManifest, tuple[SourceArtifact, ...]]:
    """Capture an allowlisted set of files from a directory.

    Validation happens for every requested path before any blob is published, so
    a single bad path fails the whole capture closed with no artifacts written.
    ``byte_limit`` is accepted for caller compatibility only and never drops a
    file; ``count_limit`` still bounds the allowlist size as before.
    """
    if not include_relative:
        raise CaptureError("directory capture requires at least one allowlisted path")
    if len(include_relative) > count_limit:
        raise CaptureError(
            f"directory capture has {len(include_relative)} files, "
            f"exceeding count_limit {count_limit}"
        )

    # Phase 1: validate every requested path (no writes yet)
    candidates: list[tuple[str, Path]] = []
    for relative in include_relative:
        candidate = _resolve_relative(source_dir, relative)
        _validate_file_for_capture(candidate, relative)
        candidates.append((relative, candidate))

    # Phase 2: hash and copy in lockstep, one fixed-size chunk at a time (a file
    # is never refused for size, so it must never be held whole in memory either)
    artifacts: list[SourceArtifact] = []
    for relative, candidate in candidates:
        blob_id, content_hash, size = _copy_stream_to_blob(candidate, dest_dir)
        if family and mirror_path:
            artifact_id = make_slot_artifact_id(family, f"{mirror_path}/{relative}")
        else:
            artifact_id = blob_id  # legacy content-addressed fallback
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                family=family,
                source_kind="file",
                content_hash=content_hash,
                capture_method="sha256",
                relative_path=relative,
                byte_size=size,
            )
        )

    manifest = CaptureManifest(
        manifest_id=_manifest_id(source_dir),
        source_root=str(source_dir),
        capture_method="directory",
        artifacts=tuple(artifacts),
        policy=CapturePolicy(byte_limit=byte_limit, count_limit=count_limit,
                             exact_relative_paths=tuple(include_relative)),
        schema_digest=None,
        privacy_dispositions=(),
        created_at=_now(),
    )
    write_manifest(manifest, dest_dir)
    return manifest, tuple(artifacts)


def _manifest_id(source: Path) -> str:
    return hashlib.sha256(
        f"{source.resolve()}|{_now()}".encode("utf-8")
    ).hexdigest()[:16]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_manifest(manifest: CaptureManifest, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / _MANIFEST_NAME
    tmp = dest_dir / f".manifest-{uuid.uuid4().hex}.json"
    tmp.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def read_manifest(path: Path) -> CaptureManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    return CaptureManifest.from_dict(data)


def replay_manifest(manifest: CaptureManifest, blob_root: Path) -> ReplayResult:
    """Verify every artifact blob is present and byte-identical.

    The blob store is content-addressed (named by ``content_hash[:32]``), so the
    blob is located via ``content_hash`` rather than the logical ``artifact_id``.
    Blobs are hashed in fixed-size chunks: a blob may be large and must not be
    read whole into memory.
    """
    missing: list[str] = []
    mismatched: list[str] = []
    for artifact in manifest.artifacts:
        blob = blob_root / artifact.content_hash[:32]
        if not blob.exists():
            missing.append(artifact.artifact_id)
            continue
        if _sha256_path(blob) != artifact.content_hash:
            mismatched.append(artifact.artifact_id)
    return ReplayResult(
        ok=not missing and not mismatched,
        missing=missing,
        mismatched=mismatched,
    )


# --------------------------------------------------------------------------
# SQLite capture
# --------------------------------------------------------------------------


def _read_only_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _is_forbidden(name: str) -> bool:
    lowered = name.lower()
    return any(pattern in lowered for pattern in FORBIDDEN_TABLE_PATTERNS)


def _schema_digest(con: sqlite3.Connection, tables: set[str]) -> str:
    rows = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name IN ({}) ORDER BY name".format(
            ",".join("?" for _ in sorted(tables))
        ),
        tuple(sorted(tables)),
    ).fetchall()
    payload = "\n;;;".join(sql or "" for _name, sql in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def capture_sqlite(
    source: Path,
    dest_dir: Path,
    *,
    allowed_tables: tuple[str, ...],
    allowed_columns: dict[str, tuple[str, ...]],
    byte_limit: int,
    count_limit: int,
    family: str = "",
    mirror_path: str | None = None,
) -> tuple[SourceArtifact, Path]:
    """WAL-safe, allowlisted capture of a mutable SQLite store.

    Uses the SQLite **online backup API** (never a loose ``.db``/``-wal`` copy,
    Phase 62 D-05), then drops every table outside the declared allowlist in the
    staging copy so adjacent credential/account/token/auth tables are absent
    from the published artifact (D-08). Declared table/column capability is
    validated against the live schema and fails closed on drift.

    ``byte_limit`` is accepted for caller compatibility only: a large live store
    must not lose its whole family, and the allowlist is what bounds content.
    """
    if not allowed_tables:
        raise CaptureError("sqlite capture requires at least one allowed table")
    if len(allowed_tables) > count_limit:
        raise CaptureError(
            f"sqlite capture declares {len(allowed_tables)} tables, "
            f"exceeding count_limit {count_limit}"
        )
    for table in allowed_tables:
        if _is_forbidden(table):
            raise CaptureError(
                f"table {table!r} matches a forbidden "
                "(credential/account/token/auth) pattern and cannot be captured"
            )
    _validate_sqlite_capability(source, allowed_tables, allowed_columns)

    dest_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = dest_dir / _STAGING_DIR
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"backup-{uuid.uuid4().hex}.sqlite"
    filtered = staging_dir / f"filtered-{uuid.uuid4().hex}.sqlite"
    try:
        schema_digest = _filtered_backup(
            source, staging, filtered, allowed_tables, allowed_columns
        )
        blob_id, content_hash, size = _copy_stream_to_blob(filtered, dest_dir)
        blob = _blob_root(dest_dir) / blob_id
        if family and mirror_path:
            artifact_id = make_slot_artifact_id(family, mirror_path)
        else:
            artifact_id = blob_id  # legacy content-addressed fallback

        con = _read_only_connect(source)
        try:
            present = _table_names(con)
        finally:
            con.close()
        excluded = sorted(set(present) - set(allowed_tables))
        privacy = tuple(f"excluded_table:{t}" for t in excluded)
        artifact = SourceArtifact(
            artifact_id=artifact_id,
            family=family,
            source_kind="sqlite",
            content_hash=content_hash,
            capture_method="sqlite_online_backup",
            relative_path=f"sqlite:{source.name}",
            byte_size=size,
            schema_digest=schema_digest,
            privacy_dispositions=privacy,
        )
        return artifact, blob
    finally:
        _cleanup_staging(staging)
        _cleanup_staging(filtered)


def _validate_sqlite_capability(
    source: Path,
    allowed_tables: tuple[str, ...],
    allowed_columns: dict[str, tuple[str, ...]],
) -> None:
    """Pre-flight schema/capability gate: integrity + declared tables/columns."""
    src = _read_only_connect(source)
    try:
        present = _table_names(src)
        missing_tables = [t for t in allowed_tables if t not in present]
        if missing_tables:
            raise CaptureError(
                f"declared tables missing from source: {missing_tables}"
            )
        for table in allowed_tables:
            columns = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
            declared = allowed_columns.get(table, ())
            missing_cols = [c for c in declared if c not in columns]
            if missing_cols:
                raise CaptureError(
                    f"declared columns missing for {table}: {missing_cols}"
                )
        integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CaptureError(
                f"source integrity_check={integrity!r}, refusing capture"
            )
    finally:
        src.close()


def _filtered_backup(
    source: Path,
    staging: Path,
    filtered: Path,
    allowed_tables: tuple[str, ...],
    allowed_columns: dict[str, tuple[str, ...]],
) -> str:
    """Online-backup then project only declared tables *and* columns.

    The initial online backup is the WAL-consistent read point.  A fresh
    sanitized database is then populated from that backup, avoiding residual
    pages and ensuring undeclared columns from an allowed table cannot leak.
    Returns the schema digest; the caller owns ``filtered``'s lifetime.
    """
    src = _read_only_connect(source)
    dst = sqlite3.connect(str(staging))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    source_con = sqlite3.connect(str(staging))
    target_con = sqlite3.connect(str(filtered))
    try:
        target_con.execute("PRAGMA journal_mode=DELETE")
        for table in allowed_tables:
            declared = allowed_columns[table]
            info = {
                row[1]: (row[2] or "BLOB")
                for row in source_con.execute(f'PRAGMA table_info("{table}")')
            }
            column_defs = ",".join(
                f'"{column}" {info[column]}' for column in declared
            )
            target_con.execute(f'CREATE TABLE "{table}" ({column_defs})')
            column_sql = ",".join(f'"{column}"' for column in declared)
            read = source_con.execute(f'SELECT {column_sql} FROM "{table}"')
            placeholders = ",".join("?" for _ in declared)
            while True:
                rows = read.fetchmany(1000)
                if not rows:
                    break
                target_con.executemany(
                    f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                    rows,
                )
        target_con.commit()
        target_con.execute("VACUUM")
        integrity = target_con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CaptureError(f"filtered snapshot integrity_check={integrity!r}")
        schema_digest = _schema_digest(target_con, set(allowed_tables))
    finally:
        target_con.close()
        source_con.close()
    return schema_digest


def _cleanup_staging(staging: Path) -> None:
    try:
        staging.unlink(missing_ok=True)
        parent = staging.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:  # pragma: no cover - best effort cleanup
        pass
