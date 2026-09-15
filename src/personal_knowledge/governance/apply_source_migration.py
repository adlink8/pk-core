"""Fail-closed executor for approved tracked text-source migration manifests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import random
from pathlib import Path

PRIVATE_PREFIXES = ("Agent/", "Google/", "imports/", "integration/db/", "integration/runtime/", "integration/analysis/", "_recycle/")


class MigrationError(RuntimeError):
    pass


RETRYABLE_WINERRORS = {5, 32}
REPLACE_ATTEMPTS = 5


def _append_journal(journal_path: Path, record: dict) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_replace_bytes(
    path: Path,
    content: bytes,
    *,
    attempts: int = REPLACE_ATTEMPTS,
    sleep: object = time.sleep,
) -> None:
    """Replace one file in-place, retaining mode and retrying Windows sharing errors."""
    original_mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".stage", delete=False) as handle:
        stage = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        # A read-only destination cannot be replaced on Windows. Temporarily
        # make only this exact path writable; the original mode is restored.
        os.chmod(stage, original_mode | stat.S_IWRITE)
        os.chmod(path, original_mode | stat.S_IWRITE)
        for attempt in range(attempts):
            try:
                os.replace(stage, path)
                os.chmod(path, original_mode)
                return
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                if winerror not in RETRYABLE_WINERRORS:
                    raise MigrationError(f"non-retryable atomic replace error for {path}: {exc}") from exc
                if attempt + 1 >= attempts:
                    raise MigrationError(
                        f"atomic replace exhausted {attempts} attempts for {path} (winerror={winerror})"
                    ) from exc
                delay = min(0.05 * (2**attempt), 0.8) + random.SystemRandom().uniform(0.0, 0.025)
                sleep(delay)  # type: ignore[operator]
    finally:
        if stage.exists():
            os.chmod(stage, stat.S_IWRITE)
            stage.unlink()
        if path.exists():
            os.chmod(path, original_mode)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, rel: str) -> Path:
    if Path(rel).is_absolute() or rel.replace("\\", "/").startswith(PRIVATE_PREFIXES):
        raise MigrationError(f"unsafe/private path: {rel}")
    path = (root / rel).resolve(strict=False)
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise MigrationError(f"path escapes workspace: {rel}") from exc
    return path


def _reject_reparse(path: Path) -> None:
    if path.exists() and (path.is_symlink() or os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise MigrationError(f"reparse node rejected: {path}")


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("scope") != "tracked-text-source-only":
        raise MigrationError("manifest scope is not tracked text source")
    claimed = payload.get("manifest_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    actual = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if claimed != actual:
        raise MigrationError("manifest checksum mismatch")
    return payload


def preflight(root: Path, payload: dict) -> list[dict]:
    seen: set[str] = set()
    operations = payload.get("entries", [])
    if payload.get("require_git_tracked"):
        tracked = set(subprocess.check_output(["git", "ls-files"], cwd=root, text=True, encoding="utf-8").splitlines())
        missing = [op["source"] for op in operations if op["source"] not in tracked and not op.get("authorized_untracked")]
        if missing:
            raise MigrationError(f"manifest contains untracked sources: {missing[:3]}")
    for op in operations:
        source_rel, target_rel = op["source"], op["target"]
        for rel in (source_rel, target_rel):
            key = rel.replace("\\", "/").casefold()
            if key in seen:
                raise MigrationError(f"case-folded collision: {rel}")
            seen.add(key)
        source, target = _inside(root, source_rel), _inside(root, target_rel)
        _reject_reparse(source)
        _reject_reparse(target.parent)
        if not source.is_file():
            raise MigrationError(f"source missing or non-file: {source_rel}")
        if source.suffix.lower() not in {".py", ".md", ".toml", ".json", ".yaml", ".yml", ".txt", ".js", ".mjs", ".css", ".html"} and not op.get("text_source"):
            raise MigrationError(f"binary/unsupported source: {source_rel}")
        if _hash(source) != op["sha256"]:
            raise MigrationError(f"source hash drift: {source_rel}")
        if target.exists():
            raise MigrationError(f"dirty target exists: {target_rel}")
        if op.get("dirty") and not op.get("approved_prestate"):
            raise MigrationError(f"dirty source requires a newly approved manifest: {source_rel}")
        for rewrite in op.get("rewrites", []):
            rewrite_path = _inside(root, rewrite["path"])
            _reject_reparse(rewrite_path)
            if not rewrite_path.is_file() or _hash(rewrite_path) != rewrite["before_sha256"]:
                raise MigrationError(f"rewrite source missing/drifted: {rewrite['path']}")
    return operations


def apply(root: Path, payload: dict, journal_path: Path) -> None:
    root = root.resolve()
    journal_path = journal_path.resolve(strict=False)
    try:
        journal_path.relative_to(root)
    except ValueError as exc:
        raise MigrationError(f"journal escapes workspace: {journal_path}") from exc
    operations = preflight(root, payload)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    try:
        for op in operations:
            source = _inside(root, op["source"])
            target = _inside(root, op["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            backup_dir = root / ".migration-backup"
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / (hashlib.sha256(op["source"].encode()).hexdigest() + source.suffix)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                stage = Path(handle.name)
            shutil.copy2(source, stage)
            if _hash(stage) != op["sha256"]:
                raise MigrationError(f"stage hash mismatch: {op['source']}")
            os.replace(source, backup)
            os.replace(stage, target)
            record = {"source": op["source"], "target": op["target"], "backup": str(backup.relative_to(root)), "sha256": op["sha256"]}
            records.append(record)
            _append_journal(journal_path, record)
            for rewrite in op.get("rewrites", []):
                rewrite_path = _inside(root, rewrite["path"])
                before = rewrite_path.read_bytes()
                after = rewrite["after_text"].encode("utf-8")
                rewrite_record = {
                    "kind": "rewrite",
                    "path": rewrite["path"],
                    "before_b64": base64.b64encode(before).decode("ascii"),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                }
                records.append(rewrite_record)
                rewrite_record["before_sha256"] = hashlib.sha256(before).hexdigest()
                rewrite_record["before_mode"] = stat.S_IMODE(rewrite_path.stat().st_mode)
                _append_journal(journal_path, rewrite_record)
                _atomic_replace_bytes(rewrite_path, after)
        for rewrite in payload.get("consumer_rewrites", []):
            rewrite_rel = rewrite.get("effective_path_after_move", rewrite["path"])
            rewrite_path = _inside(root, rewrite_rel)
            if not rewrite_path.is_file():
                raise MigrationError(f"rewrite source missing after move: {rewrite_rel}")
            before = rewrite_path.read_bytes()
            if hashlib.sha256(before).hexdigest() != rewrite["before_sha256"]:
                raise MigrationError(f"rewrite source drifted: {rewrite_rel}")
            after = rewrite["after_text"].encode("utf-8")
            rewrite_record = {
                "kind": "rewrite",
                "path": rewrite_rel,
                "original_path": rewrite["path"],
                "before_b64": base64.b64encode(before).decode("ascii"),
                "after_sha256": hashlib.sha256(after).hexdigest(),
            }
            records.append(rewrite_record)
            rewrite_record["before_sha256"] = hashlib.sha256(before).hexdigest()
            rewrite_record["before_mode"] = stat.S_IMODE(rewrite_path.stat().st_mode)
            _append_journal(journal_path, rewrite_record)
            _atomic_replace_bytes(rewrite_path, after)
    except Exception:
        rollback(root, journal_path, records=records)
        raise


def rollback(root: Path, journal_path: Path, records: list[dict] | None = None) -> None:
    root = root.resolve()
    journal_path = journal_path.resolve(strict=False)
    try:
        journal_path.relative_to(root)
    except ValueError as exc:
        raise MigrationError(f"journal escapes workspace: {journal_path}") from exc
    if records is None:
        if not journal_path.exists():
            raise MigrationError("rollback journal missing")
        records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line]
    for record in reversed(records):
        if record.get("kind") == "rewrite":
            path = _inside(root, record["path"])
            if not path.exists():
                original_rel = record.get("original_path")
                if original_rel:
                    original = _inside(root, original_rel)
                    if original.is_file() and _hash(original) == record.get("before_sha256"):
                        continue
                raise MigrationError(f"rewritten consumer missing during rollback: {record['path']}")
            current_hash = _hash(path)
            if current_hash == record.get("before_sha256"):
                continue
            if current_hash != record["after_sha256"]:
                raise MigrationError(f"rewritten consumer drift blocks rollback: {record['path']}")
            before = base64.b64decode(record["before_b64"])
            _atomic_replace_bytes(path, before)
            if "before_mode" in record:
                os.chmod(path, record["before_mode"])
            continue
        source, target = _inside(root, record["source"]), _inside(root, record["target"])
        backup = _inside(root, record["backup"])
        if source.is_file() and _hash(source) == record["sha256"] and not target.exists() and not backup.exists():
            # Idempotent resume after an interrupted rollback already consumed
            # this exact backup and restored the source.
            continue
        if target.exists() and _hash(target) != record["sha256"]:
            raise MigrationError(f"target drift blocks rollback: {record['target']}")
        if target.exists():
            target.unlink()
        source.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists() or _hash(backup) != record["sha256"]:
            raise MigrationError(f"backup missing/drifted: {record['backup']}")
        os.replace(backup, source)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--journal", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    journal = args.journal or root / "var/runtime/migration/source-migration.journal.jsonl"
    if args.rollback:
        rollback(root, journal)
        return
    if not args.manifest:
        parser.error("--manifest is required")
    payload = load_manifest(args.manifest)
    if args.apply:
        apply(root, payload, journal)
    else:
        preflight(root, payload)
        print(json.dumps({"mode": "dry-run", "operations": len(payload.get("entries", []))}))


if __name__ == "__main__":
    main()
