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
from dataclasses import dataclass, field
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
from personal_knowledge.adapters.conversation_sources.snapshots import CaptureError

_SQLITE_MAGIC = b"SQLite format 3\x00"
_HEAD_BYTES = 16

# Chunk size for the streaming snapshot publish (hash + write in lockstep).
_STREAM_CHUNK_BYTES = 1 << 20

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
        # Codex keeps two session trees: live sessions under sessions/ and
        # rotated sessions under archived_sessions/ (same rollout-*.jsonl
        # format, disjoint file sets). Missing either root undercounts the
        # family; the two trees share no file, so slot identity (mirror path
        # relative to root) stays unique across them.
        "codex": (f"{home}/.codex/sessions", f"{home}/.codex/archived_sessions"),
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


def _artifact_for(path: Path) -> SourceArtifact | None:
    try:
        size = path.stat().st_size
    except OSError:
        # The file vanished between listing and stat (SQLite -shm/-wal
        # sidecars disappear the moment their database closes). Skipping is
        # correct: a file that no longer exists has nothing to ingest.
        return None
    return SourceArtifact(
        artifact_id=path.name,
        family="",
        source_kind=probe_source_kind(path),
        content_hash=PROBE_CONTENT_HASH,
        capture_method="probe",
        relative_path=path.name,
        byte_size=size,
    )


def _gemini_tmp_file_allowed(root: Path, candidate: Path) -> bool:
    """Keep every file outside ``tmp``. Inside it, only session chats.

    Gemini stores live chats at ``tmp/<project>/chats/session-*.json``. Other
    families still skip the whole ``tmp`` directory; this exception must not
    pull the rest of that tree into discovery.
    """
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        return False
    if "tmp" not in parts:
        return True
    rest = parts[parts.index("tmp") + 1:]
    if len(rest) != 3:
        return False
    project, chats, name = rest
    return (
        bool(project)
        and chats == "chats"
        and name.startswith("session-")
        and name.endswith(".json")
    )


# Unclaimed-file reasons: the only short codes the ledger may emit.
REASON_NOT_THIS_FAMILY = "not_this_family"
REASON_PROBE_ERROR = "probe_error"
REASON_VANISHED = "vanished"


@dataclass
class UnclaimedFile:
    """One file that reached discovery but was not claimed by its family."""

    family: str
    path: str
    reason: str


@dataclass
class DiscoveryLedger:
    """Optional, read-only accounting sink for :func:`discover_client_sources`.

    Counts what discovery saw; never influences what discovery returns.
    """

    scanned_roots: int = 0
    candidates: int = 0
    claimed: int = 0
    unclaimed: list[UnclaimedFile] = field(default_factory=list)


def _file_identity(path: Path) -> str:
    """Case-normalized resolved path: one file counts once, however reached."""
    return os.path.normcase(str(path.resolve()))


def _walk(root: Path, *, family: str | None = None) -> list[Path]:
    """Bounded recursive file listing (no symlinks/junctions)."""
    gemini_tmp = family == "gemini"
    skip = SKIP_DIR_NAMES - {"tmp"} if gemini_tmp else SKIP_DIR_NAMES
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = Path(dirpath).relative_to(root).parts
        if len(depth) > MAX_DEPTH:
            dirnames[:] = []
            continue
        allowed = sorted(d for d in dirnames if d not in skip)
        if gemini_tmp and "tmp" in depth:
            after = depth[depth.index("tmp") + 1:]
            if len(after) == 1:
                allowed = [d for d in allowed if d == "chats"]
            elif len(after) >= 2:
                allowed = []
        dirnames[:] = allowed
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                if candidate.is_symlink() or candidate.is_junction():
                    continue
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if gemini_tmp and not _gemini_tmp_file_allowed(root, candidate):
                continue
            out.append(candidate)
    return out


def discover_client_sources(
    roots: dict[str, tuple[Path, ...]] | None = None,
    *,
    include_env_roots: bool = False,
    ledger: DiscoveryLedger | None = None,
) -> dict[str, list[Path]]:
    """Probe every candidate root with the owning family detector.

    Returns ``family -> [matching absolute file paths]``. Read-only; a file
    that fails its family detector is excluded (no silent coercion). When
    ``roots`` is None the default :data:`FAMILY_CLIENT_ROOTS` is used.

    ``ledger`` is a pure visibility sink: passing it records what was scanned
    and what was dropped, without changing the returned mapping.
    """
    effective = roots if roots is not None else FAMILY_CLIENT_ROOTS
    found: dict[str, list[Path]] = {}
    # 台账按解析后的绝对路径全局去重：别名家族（vscode-copilot → copilot）与属主
    # 共享同一 root，同一批文件不能算两遍。返回值保持原样（别名键另有消费者）。
    counted: set[str] = set()
    for family, root_paths in effective.items():
        matches: list[Path] = []
        seen: set[str] = set()
        for root in root_paths:
            if not root.is_dir():
                continue
            if ledger is not None:
                ledger.scanned_roots += 1
            for file_path in _walk(root, family=family):
                identity = _file_identity(file_path)
                if identity in seen:
                    # 同一家族的第二个 root 嵌套在第一个里面（zcode：先直取
                    # db 的 cli/db，再递归 cli），同一文件只认领、只计数一次。
                    continue
                seen.add(identity)
                # A listed file is a candidate even if it dies before stat:
                # candidates == claimed + len(unclaimed) must hold.
                first_sighting = ledger is not None and identity not in counted
                if first_sighting:
                    counted.add(identity)
                    ledger.candidates += 1
                artifact = _artifact_for(file_path)
                if artifact is None:
                    if first_sighting:
                        ledger.unclaimed.append(
                            UnclaimedFile(family, str(file_path), REASON_VANISHED)
                        )
                    continue
                try:
                    claimed = detect_family(
                        family, artifact, artifact_root=file_path.parent
                    )
                except Exception:  # noqa: BLE001 - a probe failure excludes the file
                    if first_sighting:
                        ledger.unclaimed.append(
                            UnclaimedFile(family, str(file_path), REASON_PROBE_ERROR)
                        )
                    continue
                if claimed:
                    matches.append(file_path)
                    if first_sighting:
                        ledger.claimed += 1
                elif first_sighting:
                    ledger.unclaimed.append(
                        UnclaimedFile(
                            family, str(file_path), REASON_NOT_THIS_FAMILY
                        )
                    )
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

    ``byte_limit`` is accepted for caller compatibility only: a snapshot is
    never discarded for its size (a live store above an entry threshold must
    not lose its whole family). The allowlist is what bounds the content.
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
        # Publish atomically by streaming: the snapshot is hashed and written in
        # fixed-size chunks (never whole-file ``read_bytes()``, which would OOM
        # once large stores are no longer refused), and the temp is renamed into
        # place only after a complete write.
        tmp = staging_dir / f".tmp-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        with filtered.open("rb") as handle, tmp.open("wb") as out:
            for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
                digest.update(chunk)
                out.write(chunk)
        os.replace(tmp, target)
        return digest.hexdigest()
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
    ledger: DiscoveryLedger | None = None,
) -> dict:
    """Discover then incrementally copy matching files under
    ``<stage_root>/<family>/<relative-path>``.

    A file is staged only when its content hash is not already present under
    the stage root (dedup across runs). Returns a metadata-only report:
    ``staged`` / ``skipped`` counts plus per-family paths. No canonical write.

    ``ledger`` is passed straight through to discovery as a visibility sink;
    it never changes what is staged or the returned report.
    """
    stage_root.mkdir(parents=True, exist_ok=True)
    discovered = discover_client_sources(roots=roots, ledger=ledger)
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
            # filter) and are exempt from the raw-size gate: the snapshot is kept
            # whole whatever its size, so a large live store keeps its family.
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
    "DiscoveryLedger",
    "UnclaimedFile",
    "discover_client_sources",
    "probe_source_kind",
    "snapshot_sqlite_to_file",
    "stage_client_sources",
]