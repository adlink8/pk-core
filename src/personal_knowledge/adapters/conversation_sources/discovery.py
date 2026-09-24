"""Phase 62: client-directory discovery and incremental staging seam.

Closes the 62-07 shadow gap (SQLite/directory families were reported
``no_source`` because the probe hard-coded ``source_kind="file"``). This module
owns the adapter-boundary discovery layer: for every registered family it
carries the machine-local candidate roots, probes each file with the owning
family detector (never a second parser), and stages new/changed files into a
shadow-compatible source root, deduplicated by content hash.

Public seam (engineering contract):

  - :data:`FAMILY_CLIENT_ROOTS` — family -> candidate root patterns.
  - :func:`discover_client_sources` — read-only family -> [file] detection.
  - :func:`stage_client_sources` — incremental copy into a v2 source root.
  - :func:`probe_source_kind` — sqlite magic vs generic file head probe.

Invariants: read-only discovery; per-family detector owned by the family
adapter; content-hash dedup; stage path = ``<stage_root>/<family>/<relative>``;
no canonical write, no activation, no paid calls (D-31).
"""

from __future__ import annotations

import hashlib
import os
import json
import shutil
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import (
    antigravity,
    chatgpt,
    cursor,
    mimo_opencode,
    zcode,
)
from personal_knowledge.adapters.conversation_sources.contracts import (
    PROBE_CONTENT_HASH,
    SourceArtifact,
)
from personal_knowledge.adapters.conversation_sources.registry import (
    ALIASES,
    detect_family,
    resolve_family,
)

_SQLITE_MAGIC = b"SQLite format 3\x00"
_HEAD_BYTES = 16

# Filename prefixes of capture intermediates written by
# :func:`snapshot_sqlite_to_file`. They are never source data: any consumer
# that walks a stage tree must ignore them, or a leaked temp is misread as a
# second copy of the trajectory it was derived from.
CAPTURE_TEMP_PREFIXES = (".snap-", ".filtered-", ".tmp-")


def capture_temp_root() -> Path:
    """Project-local scratch dir holding capture intermediates.

    Deliberately outside every stage root (see :func:`snapshot_sqlite_to_file`).
    """
    from personal_knowledge.core.project_paths import VAR_TMP

    return VAR_TMP / "conversation-capture"


def mirror_path_for(
    family: str, source: Path, *, source_root: Path | None = None
) -> str:
    """Stable, family-scoped, directory-qualified path used for slot identity.

    The slot ``artifact_id`` is derived from ``family`` + this mirror path, so the
    path MUST be unique per (family, source file) and constant across content
    edits. We prefer the source's path relative to ``source_root`` (when the sync
    is driven by an explicit root, e.g. the v2 shadow stage), else relative to the
    family's native client root, else fall back to the bare filename.

    The bare-filename fallback is only reached for sources outside every known
    root and is acceptable because such sources are not staged for live sync.
    """
    if source_root is not None:
        try:
            return str(source.relative_to(source_root))
        except ValueError:
            pass
    for root in FAMILY_CLIENT_ROOTS.get(resolve_family(family), ()):
        try:
            return str(source.relative_to(root))
        except ValueError:
            continue
    return source.name


# Maximum depth of recursive discovery under one family root.
MAX_DEPTH = 8

# Directories never traversed during discovery (vendor/VCS/system noise).
SKIP_DIR_NAMES = {
    "node_modules", ".git", ".hg", ".svn", "target", "dist", "build",
    "Library", "go", ".cargo", "__pycache__", ".cache", "Cache",
    "Logs", "logs", "Temp", "temp", "tmp", ".tmp", "crashpad", "GPUCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "Code Cache", ".venv", "venv",
}

# Family -> (allowed tables, allowed columns) for WAL-safe SQLite capture.
# Single data source: each adapter module owns its LIVE allowlist (62-01 D-08).
SQLITE_ALLOWLISTS: dict[str, tuple[tuple[str, ...], dict[str, tuple[str, ...]]]] = {
    "zcode": (zcode.LIVE_ALLOWED_TABLES, zcode.LIVE_ALLOWED_COLUMNS),
    "mimo": (mimo_opencode.LIVE_ALLOWED_TABLES, mimo_opencode.LIVE_ALLOWED_COLUMNS),
    "opencode": (mimo_opencode.LIVE_ALLOWED_TABLES, mimo_opencode.LIVE_ALLOWED_COLUMNS),
    "antigravity": (antigravity.LIVE_ALLOWED_TABLES, antigravity.LIVE_ALLOWED_COLUMNS),
    "chatgpt": (chatgpt.LIVE_ALLOWED_TABLES, chatgpt.LIVE_ALLOWED_COLUMNS),
}

# SQLite families (detected by magic head) that carry a native store.
SQLITE_FAMILIES: frozenset[str] = frozenset(SQLITE_ALLOWLISTS)



def _expand(pattern: str) -> Path:
    """Expand ``~`` and environment variables in a root pattern."""
    return Path(os.path.expandvars(os.path.expanduser(pattern))).resolve()


def _default_roots() -> dict[str, tuple[Path, ...]]:
    """Resolve default candidate roots (env override per family wins).

    Patterns follow Phase 62 adapter native-shape knowledge (62-RESEARCH
    format matrix) and the machine-local layout observed on this host.
    chatgpt has no native directory (manual zip import) and is not listed.
    """
    home = str(Path.home()).replace("\\", "/")
    candidates: dict[str, tuple[str, ...]] = {
        "codex": (f"{home}/.codex/sessions",),
        "claude": (f"{home}/.claude/projects",),
        "qoder": (f"{home}/.qoder", f"{home}/.qoder-cli", f"{home}/.qoder-cn"),
        "pi": (f"{home}/.pi/agent/sessions",),
        "workbuddy": (f"{home}/.workbuddy/projects",),
        "kimi": (f"{home}/.kimi-code", f"{home}/.kimi"),
        "kimi-work": (f"{home}/.kimi-work", f"{home}/.kimi-webbridge"),
        "copilot": (f"{home}/.copilot",),
        # vscode-copilot is an alias of copilot (registry); same native root.
        "vscode-copilot": (f"{home}/.copilot",),
        "gemini": (f"{home}/.gemini",),
        "zcode": (f"{home}/.zcode/cli/db", f"{home}/.zcode/cli"),
        "mimo": (f"{home}/.local/share/mimocode",),
        "opencode": (f"{home}/.local/share/opencode",),
        "antigravity": (f"{home}/.antigravity", f"{home}/.gemini/antigravity"),
        "grok": (f"{home}/.grok/sessions",),
        "cursor": (f"{home}/.cursor/projects", f"{home}/.cursor"),
        # chatgpt has no native directory: manual zip import only (empty roots).
        "chatgpt": (),
    }
    roots: dict[str, tuple[Path, ...]] = {}
    for family, patterns in candidates.items():
        override = os.environ.get(f"PK_CLIENT_ROOT_{family.upper().replace('-', '_')}")
        if override:
            patterns = tuple(p.strip() for p in override.split(os.pathsep) if p.strip())
        resolved = tuple(_expand(p) for p in patterns)
        roots[family] = tuple(p for p in resolved if p.is_dir())
    return roots


FAMILY_CLIENT_ROOTS: dict[str, tuple[Path, ...]] = _default_roots()


def probe_source_kind(path: Path) -> str:
    """Return ``"sqlite"`` for a SQLite store head, else ``"file"``."""
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return "file"
    return "sqlite" if head.startswith(_SQLITE_MAGIC) else "file"


def _artifact_for(path: Path) -> SourceArtifact:
    size = path.stat().st_size
    return SourceArtifact(
        artifact_id=path.name,
        family="",
        source_kind=probe_source_kind(path),
        content_hash=PROBE_CONTENT_HASH,
        capture_method="probe",
        relative_path=path.name,
        byte_size=size,
    )


def _walk(root: Path) -> list[Path]:
    """Bounded recursive file listing (no symlinks/junctions)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = Path(dirpath).relative_to(root).parts
        if len(depth) > MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIR_NAMES
        )
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                if candidate.is_symlink() or candidate.is_junction():
                    continue
                if not candidate.is_file():
                    continue
                out.append(candidate)
            except OSError:
                continue
    return out


def discover_client_sources(
    roots: dict[str, tuple[Path, ...]] | None = None,
    *,
    include_env_roots: bool = False,
) -> dict[str, list[Path]]:
    """Probe every candidate root with the owning family detector.

    Returns ``family -> [matching absolute file paths]``. Read-only; a file
    that fails its family detector is excluded (no silent coercion). When
    ``roots`` is None the default :data:`FAMILY_CLIENT_ROOTS` is used.
    """
    effective = roots if roots is not None else FAMILY_CLIENT_ROOTS
    found: dict[str, list[Path]] = {}
    for family, root_paths in effective.items():
        matches: list[Path] = []
        for root in root_paths:
            if not root.is_dir():
                continue
            for file_path in _walk(root):
                artifact = _artifact_for(file_path)
                try:
                    if detect_family(
                        family, artifact, artifact_root=file_path.parent
                    ):
                        matches.append(file_path)
                except Exception:  # noqa: BLE001 - a probe failure excludes the file
                    continue
        found[family] = sorted(matches)
    return found


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_to_root(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def snapshot_sqlite_to_file(
    source: Path, target: Path, *,
    allowed_tables: tuple[str, ...],
    allowed_columns: dict[str, tuple[str, ...]],
    byte_limit: int = 200_000_000,
) -> str:
    """WAL-safe allowlisted snapshot of a mutable SQLite store into ``target``.

    Uses the SQLite online backup API (never loose ``.db``/``-wal`` copies,
    Phase 62 D-05) and projects only declared tables/columns (D-08), so the
    staged file carries no adjacent credential/token tables. Returns the
    content hash of the snapshot bytes.
    """
    import sqlite3
    import uuid

    staging_dir = target.parent
    staging_dir.mkdir(parents=True, exist_ok=True)
    # Capture temps live OUTSIDE the stage tree. ``staging_dir`` is itself
    # re-scanned by the v2 shadow detector on the next run, so a leaked temp
    # (blocked cleanup, killed process) would be re-adapted as a second copy of
    # the same trajectory and collide on event ids. ``var/tmp`` sits on the same
    # volume as the stage root; the atomic publish below still writes its own
    # ``.tmp-`` marker next to ``target`` so the rename stays same-volume.
    temp_root = capture_temp_root()
    temp_root.mkdir(parents=True, exist_ok=True)
    staging = temp_root / f".snap-{uuid.uuid4().hex}.sqlite"
    filtered = temp_root / f".filtered-{uuid.uuid4().hex}.sqlite"
    try:
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(str(staging))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()

        src_con = sqlite3.connect(str(staging))
        tgt_con = sqlite3.connect(str(filtered))
        try:
            tgt_con.execute("PRAGMA journal_mode=DELETE")
            for table in allowed_tables:
                declared = allowed_columns[table]
                info = {
                    row[1]: (row[2] or "BLOB")
                    for row in src_con.execute(f'PRAGMA table_info("{table}")')
                }
                column_defs = ",".join(
                    f'"{column}" {info[column]}' for column in declared
                )
                tgt_con.execute(f'CREATE TABLE "{table}" ({column_defs})')
                column_sql = ",".join(f'"{column}"' for column in declared)
                read = src_con.execute(f'SELECT {column_sql} FROM "{table}"')
                placeholders = ",".join("?" for _ in declared)
                while True:
                    rows = read.fetchmany(1000)
                    if not rows:
                        break
                    tgt_con.executemany(
                        f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                        rows,
                    )
            tgt_con.commit()
            tgt_con.execute("VACUUM")
            integrity = tgt_con.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise CaptureError(f"snapshot integrity_check={integrity!r}")
        finally:
            tgt_con.close()
            src_con.close()
        bytes_data = filtered.read_bytes()
        if len(bytes_data) > byte_limit:
            raise CaptureError(
                f"sqlite snapshot of {len(bytes_data)} bytes exceeds byte_limit {byte_limit}"
            )
        digest = hashlib.sha256(bytes_data).hexdigest()
        # atomic publish into the stage root
        tmp = staging_dir / f".tmp-{uuid.uuid4().hex}"
        tmp.write_bytes(bytes_data)
        os.replace(tmp, target)
        return digest
    finally:
        for p in (staging, filtered):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def stage_client_sources(
    *,
    stage_root: Path,
    roots: dict[str, tuple[Path, ...]] | None = None,
    byte_limit: int = 50_000_000,
    count_limit: int = 2_000,
) -> dict:
    """Discover then incrementally copy matching files under
    ``<stage_root>/<family>/<relative-path>``.

    A file is staged only when its content hash is not already present under
    the stage root (dedup across runs). Returns a metadata-only report:
    ``staged`` / ``skipped`` counts plus per-family paths. No canonical write.
    """
    stage_root.mkdir(parents=True, exist_ok=True)
    discovered = discover_client_sources(roots=roots)
    report: dict[str, dict] = {}
    total_staged = 0
    total_skipped = 0
    for family, paths in sorted(discovered.items()):
        if family in ALIASES:
            # aliases resolve to the owning family (registry D-02); staging the
            # owner once avoids duplicate stage trees for the same root.
            continue
        fam_dir = stage_root / family
        fam_dir.mkdir(parents=True, exist_ok=True)
        staged: list[str] = []
        skipped: list[str] = []
        for src in paths[:count_limit]:
            try:
                size = src.stat().st_size
            except OSError:
                skipped.append(str(src))
                continue
            is_sqlite = probe_source_kind(src) == "sqlite"
            # SQLite sources are WAL-snapshotted first (online backup + allowlist
            # filter); the final snapshot size is checked after filtering, so the
            # raw live store may exceed byte_limit.
            if size > byte_limit and not is_sqlite:
                skipped.append(f"{src} (byte_limit)")
                continue
            rel = _relative_to_root(src.parent if len(src.parent.parts) else src, src)
            # Mirror the path under the family dir using the source relative name.
            for root in (roots or FAMILY_CLIENT_ROOTS).get(family, ()):
                if root in src.parents:
                    rel = src.relative_to(root).as_posix()
                    break
            target = fam_dir / rel
            digest = _file_hash(src)
            manifest = fam_dir / ".hashes.json"
            known: dict[str, str] = {}
            if manifest.exists():
                try:
                    known = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    known = {}
            if known.get(rel) == digest and target.exists():
                skipped.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if probe_source_kind(src) == "sqlite":
                allowlist = SQLITE_ALLOWLISTS.get(family)
                if allowlist is None:
                    skipped.append(f"{rel} (sqlite without allowlist)")
                    continue
                tables, columns = allowlist
                try:
                    digest = snapshot_sqlite_to_file(
                        src, target, allowed_tables=tables, allowed_columns=columns,
                        byte_limit=byte_limit,
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed per file
                    skipped.append(f"{rel} (sqlite_snapshot:{type(exc).__name__})")
                    continue
            else:
                shutil.copy2(src, target)
            known[rel] = digest
            manifest.write_text(
                json.dumps(known, sort_keys=True, indent=0),
                encoding="utf-8",
            )
            staged.append(rel)
        report[family] = {
            "staged": len(staged),
            "skipped": len(skipped),
            "staged_paths": staged,
            "skipped_paths": skipped,
        }
        total_staged += len(staged)
        total_skipped += len(skipped)
    return {
        "staged": total_staged,
        "skipped": total_skipped,
        "families": report,
    }


__all__ = [
    "FAMILY_CLIENT_ROOTS",
    "SQLITE_ALLOWLISTS",
    "SQLITE_FAMILIES",
    "discover_client_sources",
    "probe_source_kind",
    "snapshot_sqlite_to_file",
    "stage_client_sources",
]