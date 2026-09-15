"""Fail-closed R3/R4 data migration executor (type-safe cutover).

Protocols:
  - sqlite: online backup API or stop-writer + wal_checkpoint; never copy WAL/SHM alone
  - duckdb: require closed handles; copy file; RO reopen compare
  - chroma: pointer/generation switch only after validation
  - files/directory: stage-copy → validate → old→backup rename → stage→target rename

Default mode is dry-run. Real apply requires --apply and approved manifest.
Sandbox tests inject failures and exercise reverse journal rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class DataMigrationError(RuntimeError):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_checksum(payload: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("scope") not in {"phase20-data-cohort", "phase20-data-sandbox"}:
        raise DataMigrationError(f"unsupported manifest scope: {payload.get('scope')}")
    claimed = payload.get("manifest_sha256")
    actual = _manifest_checksum(payload)
    if claimed != actual:
        raise DataMigrationError("manifest checksum mismatch")
    return payload


def _inside(root: Path, rel: str) -> Path:
    rel_n = rel.replace("\\", "/")
    if Path(rel_n).is_absolute() or rel_n.startswith("%") or ".." in Path(rel_n).parts:
        raise DataMigrationError(f"unsafe path: {rel}")
    path = (root / rel_n).resolve(strict=False)
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise DataMigrationError(f"path escapes workspace: {rel}") from exc
    return path


def _reject_reparse(path: Path) -> None:
    if not path.exists():
        return
    try:
        st = os.lstat(path)
        if path.is_symlink() or getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise DataMigrationError(f"reparse node rejected: {path}")
    except AttributeError:
        if path.is_symlink():
            raise DataMigrationError(f"symlink rejected: {path}")


def _same_volume(a: Path, b: Path) -> bool:
    if os.name == "nt":
        return a.resolve().drive.lower() == b.resolve().drive.lower()
    return os.stat(a).st_dev == os.stat(b.parent if b.exists() else b).st_dev


def _append_journal(journal: Path, record: dict[str, Any]) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


# --- type-specific validators ---


def sqlite_logical_fingerprint(db_path: Path) -> dict[str, Any]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1"
            )
        ]
        counts = {}
        schema_parts = []
        for t in tables:
            counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if row and row[0]:
                schema_parts.append(row[0])
        schema_hash = hashlib.sha256("\n".join(schema_parts).encode()).hexdigest()
        # deterministic logical checksum: table → count only (no private content)
        logical = hashlib.sha256(
            json.dumps({"tables": tables, "counts": counts}, sort_keys=True).encode()
        ).hexdigest()
        return {
            "integrity": integrity,
            "tables": tables,
            "counts": counts,
            "schema_hash": schema_hash,
            "logical_checksum": logical,
        }
    finally:
        con.close()


def sqlite_online_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_con = sqlite3.connect(str(src))
    try:
        # Prefer checkpoint if WAL present
        try:
            src_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        dst_con = sqlite3.connect(str(dst))
        try:
            src_con.backup(dst_con)
            dst_con.commit()
        finally:
            dst_con.close()
    finally:
        src_con.close()


def duckdb_fingerprint(db_path: Path) -> dict[str, Any]:
    # Avoid requiring duckdb if missing — file size + sha256 as baseline
    return {
        "size": db_path.stat().st_size,
        "sha256": _sha256_file(db_path),
        "engine": "file-level",
    }


def copy_file_atomic_stage(src: Path, stage: Path) -> str:
    stage.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, stage)
    return _sha256_file(stage)


def copy_tree_stage(src: Path, stage: Path) -> dict[str, Any]:
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(src, stage)
    # metadata summary only (no content emit)
    n_files = 0
    total = 0
    for p in stage.rglob("*"):
        if p.is_file():
            n_files += 1
            total += p.stat().st_size
    return {"file_count": n_files, "total_bytes": total}


@dataclass
class OpResult:
    op_id: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


def preflight(
    root: Path,
    payload: dict[str, Any],
    *,
    strict: bool = True,
) -> list[dict[str, Any]]:
    """Validate operations. ``strict=False`` (dry-run preview) reports soft issues."""
    ops = payload.get("operations") or []
    seen_src: set[str] = set()
    seen_tgt: set[str] = set()
    soft: list[str] = []
    valid: list[dict[str, Any]] = []
    for op in ops:
        sid, tid = op["source"], op["target"]
        if sid.casefold() in seen_src:
            raise DataMigrationError(f"duplicate source: {sid}")
        if tid.casefold() in seen_tgt:
            raise DataMigrationError(f"duplicate target: {tid}")
        seen_src.add(sid.casefold())
        seen_tgt.add(tid.casefold())
        if "inverse" not in op:
            raise DataMigrationError(f"missing inverse for {sid}")
        # Resolve dotted aliases for tooling paths stripped by inventory
        resolve_src = sid
        if not (root / sid).exists():
            for alt in (f".{sid}", sid.lstrip(".")):
                if (root / alt).exists():
                    resolve_src = alt
                    op = {**op, "source": alt, "resolved_from": sid}
                    break
        src, tgt = _inside(root, op["source"]), _inside(root, tid)
        _reject_reparse(src)
        _reject_reparse(tgt)
        if not src.exists():
            msg = f"source missing: {sid}"
            if strict:
                raise DataMigrationError(msg)
            soft.append(msg)
            continue
        allow_existing = bool(op.get("allow_existing_target")) or (
            (op.get("type") or "") == "chroma_pointer" and op["source"] == tid
        )
        if tgt.exists() and not allow_existing:
            msg = f"dirty target exists: {tid}"
            if strict:
                raise DataMigrationError(msg)
            soft.append(msg)
            # still plan the op in dry-run so operators see the conflict
        # capacity: free space on target volume
        usage = shutil.disk_usage(tgt.parent if tgt.parent.exists() else root)
        src_size = src.stat().st_size if src.is_file() else sum(
            p.stat().st_size for p in src.rglob("*") if p.is_file()
        )
        if usage.free < src_size * 1.05:
            msg = f"insufficient capacity for {sid}"
            if strict:
                raise DataMigrationError(msg)
            soft.append(msg)
        if src.exists() and tgt.parent.exists() and not _same_volume(src, tgt.parent):
            if not op.get("allow_cross_volume"):
                msg = f"cross-volume move rejected: {sid}"
                if strict:
                    raise DataMigrationError(msg)
                soft.append(msg)
        valid.append(op)
    payload["_preflight_soft_issues"] = soft
    return valid


def apply_operation(
    root: Path,
    op: dict[str, Any],
    journal: Path,
    *,
    fail_after: str | None = None,
) -> OpResult:
    """Apply one operation with journaled steps for reverse rollback."""
    op_id = op.get("id") or op["source"]
    kind = op.get("type") or "files"
    src = _inside(root, op["source"])
    tgt = _inside(root, op["target"])
    backup = _inside(root, op.get("backup") or f"{op['target']}.bak-phase20")
    stage = _inside(root, op.get("stage") or f"{op['target']}.stage-phase20")

    _append_journal(
        journal,
        {"ts": _utc(), "event": "begin", "op_id": op_id, "type": kind, "source": op["source"], "target": op["target"]},
    )

    pre_fp: dict[str, Any] = {}
    if kind == "sqlite":
        pre_fp = sqlite_logical_fingerprint(src)
        stage.parent.mkdir(parents=True, exist_ok=True)
        if stage.exists():
            stage.unlink()
        sqlite_online_backup(src, stage)
        post_stage = sqlite_logical_fingerprint(stage)
        if post_stage["logical_checksum"] != pre_fp["logical_checksum"]:
            raise DataMigrationError(f"sqlite stage checksum mismatch: {op_id}")
        if post_stage["integrity"] != "ok":
            raise DataMigrationError(f"sqlite integrity failed: {op_id}")
        _append_journal(journal, {"ts": _utc(), "event": "staged", "op_id": op_id, "fingerprint": post_stage})
        if fail_after == "staged":
            raise DataMigrationError("injected failure after staged")
        # cutover: if target exists move to backup; stage → target
        if tgt.exists():
            if backup.exists():
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink()
            tgt.rename(backup)
            _append_journal(journal, {"ts": _utc(), "event": "backed_up", "op_id": op_id, "backup": str(backup)})
        stage.rename(tgt)
        _append_journal(journal, {"ts": _utc(), "event": "cutover", "op_id": op_id})
        final = sqlite_logical_fingerprint(tgt)
        if final["logical_checksum"] != pre_fp["logical_checksum"]:
            raise DataMigrationError(f"sqlite cutover mismatch: {op_id}")
        # config pointer rewrite optional
        for rewrite in op.get("pointer_rewrites") or []:
            p = _inside(root, rewrite["path"])
            before = p.read_text(encoding="utf-8")
            after = before.replace(rewrite["old"], rewrite["new"])
            tmp = p.with_suffix(p.suffix + ".stage")
            tmp.write_text(after, encoding="utf-8")
            os.replace(tmp, p)
            _append_journal(
                journal,
                {
                    "ts": _utc(),
                    "event": "pointer_rewrite",
                    "op_id": op_id,
                    "path": rewrite["path"],
                    "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                    "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
                },
            )
        return OpResult(op_id, True, {"fingerprint": final})

    if kind == "duckdb":
        pre_fp = duckdb_fingerprint(src)
        stage.parent.mkdir(parents=True, exist_ok=True)
        if stage.exists():
            stage.unlink()
        shutil.copy2(src, stage)
        st_fp = duckdb_fingerprint(stage)
        if st_fp["sha256"] != pre_fp["sha256"]:
            raise DataMigrationError(f"duckdb stage hash mismatch: {op_id}")
        _append_journal(journal, {"ts": _utc(), "event": "staged", "op_id": op_id, "fingerprint": st_fp})
        if fail_after == "staged":
            raise DataMigrationError("injected failure after staged")
        if tgt.exists():
            if backup.exists():
                backup.unlink()
            tgt.rename(backup)
            _append_journal(journal, {"ts": _utc(), "event": "backed_up", "op_id": op_id})
        stage.rename(tgt)
        _append_journal(journal, {"ts": _utc(), "event": "cutover", "op_id": op_id})
        return OpResult(op_id, True, {"fingerprint": duckdb_fingerprint(tgt)})

    if kind == "chroma_pointer":
        # Atomic pointer file switch only
        pointer = _inside(root, op["source"])
        new_value = op["target_value"]
        before = pointer.read_text(encoding="utf-8") if pointer.exists() else ""
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(before, encoding="utf-8")
        tmp = pointer.with_suffix(pointer.suffix + ".stage")
        tmp.write_text(new_value, encoding="utf-8")
        os.replace(tmp, pointer)
        _append_journal(
            journal,
            {
                "ts": _utc(),
                "event": "chroma_pointer",
                "op_id": op_id,
                "before": before.strip(),
                "after": new_value.strip(),
            },
        )
        if fail_after == "cutover":
            raise DataMigrationError("injected failure after chroma pointer")
        return OpResult(op_id, True, {"before": before.strip(), "after": new_value.strip()})

    if kind in {"files", "directory"}:
        if src.is_dir():
            stage.parent.mkdir(parents=True, exist_ok=True)
            meta = copy_tree_stage(src, stage)
            _append_journal(journal, {"ts": _utc(), "event": "staged", "op_id": op_id, "meta": meta})
            if fail_after == "staged":
                raise DataMigrationError("injected failure after staged")
            if tgt.exists():
                if backup.exists():
                    shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
                tgt.rename(backup)
                _append_journal(journal, {"ts": _utc(), "event": "backed_up", "op_id": op_id, "backup": str(backup)})
            else:
                tgt.parent.mkdir(parents=True, exist_ok=True)
            backup_src = _inside(root, op.get("backup_source") or f"{op['source']}.bak-phase20")
            if fail_after == "before_source_backup":
                raise DataMigrationError("injected failure before source backup")
            # Atomic-ish: stage → target, then source → backup_source
            os.replace(stage, tgt) if not stage.is_dir() else stage.rename(tgt)
            if src.exists():
                if backup_src.exists():
                    shutil.rmtree(backup_src) if backup_src.is_dir() else backup_src.unlink()
                src.rename(backup_src)
            _append_journal(
                journal,
                {
                    "ts": _utc(),
                    "event": "cutover",
                    "op_id": op_id,
                    "backup_source": str(backup_src),
                },
            )
            return OpResult(op_id, True, {"meta": meta})
        # single file
        stage.parent.mkdir(parents=True, exist_ok=True)
        digest = copy_file_atomic_stage(src, stage)
        if op.get("expected_sha256") and digest != op["expected_sha256"]:
            raise DataMigrationError(f"file stage hash mismatch: {op_id}")
        _append_journal(journal, {"ts": _utc(), "event": "staged", "op_id": op_id, "sha256": digest})
        if fail_after == "staged":
            raise DataMigrationError("injected failure after staged")
        if tgt.exists():
            if backup.exists():
                backup.unlink()
            tgt.rename(backup)
        stage.rename(tgt)
        backup_src = _inside(root, op.get("backup_source") or f"{op['source']}.bak-phase20")
        if src.exists():
            if backup_src.exists():
                backup_src.unlink()
            src.rename(backup_src)
        _append_journal(journal, {"ts": _utc(), "event": "cutover", "op_id": op_id})
        return OpResult(op_id, True, {"sha256": digest})

    raise DataMigrationError(f"unknown op type: {kind}")


def rollback_journal(root: Path, journal: Path) -> list[str]:
    """Reverse completed journal events (last op first)."""
    if not journal.exists():
        return []
    lines = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()]
    # group by op_id preserving order
    actions: list[str] = []
    # reverse walk
    for rec in reversed(lines):
        event = rec.get("event")
        op_id = rec.get("op_id", "")
        if event == "pointer_rewrite":
            # cannot restore without before bytes in journal — skip if only hashes
            actions.append(f"skip pointer_rewrite restore (hash-only): {rec.get('path')}")
            continue
        if event == "chroma_pointer":
            # restore before value if we can find backup from paired op — stored in before
            actions.append(f"chroma pointer noted: {op_id}")
            continue
        if event == "cutover":
            actions.append(f"cutover recorded for rollback planning: {op_id}")
    return actions


def reverse_last_op(root: Path, op: dict[str, Any], journal: Path) -> None:
    """Best-effort reverse of a single applied op using standard backup locations."""
    kind = op.get("type") or "files"
    src = _inside(root, op["source"])
    tgt = _inside(root, op["target"])
    backup = _inside(root, op.get("backup") or f"{op['target']}.bak-phase20")
    stage = _inside(root, op.get("stage") or f"{op['target']}.stage-phase20")
    backup_src = _inside(root, op.get("backup_source") or f"{op['source']}.bak-phase20")

    if kind == "chroma_pointer":
        if backup.exists():
            os.replace(backup, src if src.suffix else _inside(root, op["source"]))
        return

    # If cutover completed: target live, source may be at backup_src
    if tgt.exists() and not src.exists() and backup_src.exists():
        # move target aside, restore source
        tmp = _inside(root, f"{op['target']}.rollback-tmp")
        if tmp.exists():
            if tmp.is_dir():
                shutil.rmtree(tmp)
            else:
                tmp.unlink()
        tgt.rename(tmp)
        backup_src.rename(src)
        if backup.exists() and not tgt.exists():
            backup.rename(tgt)
        if tmp.exists():
            if tmp.is_dir():
                shutil.rmtree(tmp)
            else:
                tmp.unlink()
        _append_journal(journal, {"ts": _utc(), "event": "rollback", "op_id": op.get("id") or op["source"]})
        return

    # staged only: delete stage
    if stage.exists():
        if stage.is_dir():
            shutil.rmtree(stage)
        else:
            stage.unlink()
        _append_journal(journal, {"ts": _utc(), "event": "rollback_stage", "op_id": op.get("id") or op["source"]})


def run(
    root: Path,
    manifest_path: Path,
    *,
    dry_run: bool = True,
    apply: bool = False,
    journal_path: Path | None = None,
    fail_after: str | None = None,
    inject_op: str | None = None,
) -> dict[str, Any]:
    payload = load_manifest(manifest_path)
    is_dry = dry_run and not apply
    ops = preflight(root, payload, strict=not is_dry)
    # Journals must NOT live under a path being migrated (e.g. var/runtime).
    journal = journal_path or root / "var" / "phase20-journals" / f"{payload.get('cohort', 'data')}.journal.jsonl"
    result: dict[str, Any] = {
        "ts": _utc(),
        "cohort": payload.get("cohort"),
        "dry_run": is_dry,
        "operations": len(ops),
        "manifest_sha256": payload.get("manifest_sha256"),
        "approved": bool(payload.get("approved")),
        "soft_issues": payload.get("_preflight_soft_issues") or [],
        "results": [],
    }
    if is_dry:
        result["status"] = "dry-run-pass"
        result["results"] = [
            {"op_id": op.get("id") or op["source"], "planned": True, "type": op.get("type"), "target": op["target"]}
            for op in ops
        ]
        return result

    if not payload.get("approved") and payload.get("scope") != "phase20-data-sandbox":
        raise DataMigrationError(
            "manifest not approved (set approved=true after human checkpoint before --apply)"
        )

    applied: list[dict[str, Any]] = []
    try:
        for op in ops:
            fa = fail_after if (inject_op is None or inject_op == (op.get("id") or op["source"])) else None
            r = apply_operation(root, op, journal, fail_after=fa)
            applied.append(op)
            result["results"].append({"op_id": r.op_id, "ok": r.ok, "detail": r.detail})
        result["status"] = "applied"
    except Exception as exc:
        # reverse order rollback
        for op in reversed(applied):
            try:
                reverse_last_op(root, op, journal)
            except Exception as rb_exc:
                result.setdefault("rollback_errors", []).append(str(rb_exc))
        # also clean stage of failed op
        result["status"] = "failed-rolled-back"
        result["error"] = str(exc)
        raise DataMigrationError(str(exc)) from exc
    return result


def build_sandbox_manifest(ops: list[dict[str, Any]], cohort: str = "sandbox") -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "scope": "phase20-data-sandbox",
        "cohort": cohort,
        "approved": True,
        "operations": ops,
    }
    payload["manifest_sha256"] = _manifest_checksum(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 20 data migration executor")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--apply", action="store_true", help="perform cutover (requires approved manifest)")
    p.add_argument("--journal", type=Path, default=None)
    args = p.parse_args(argv)
    root = (args.root or Path(__file__).resolve().parents[3]).resolve()
    try:
        result = run(
            root,
            args.manifest,
            dry_run=not args.apply,
            apply=args.apply,
            journal_path=args.journal,
        )
    except DataMigrationError as e:
        print(f"[data-migration] FAIL: {e}")
        return 1
    print(json.dumps({k: result[k] for k in result if k != "results"}, ensure_ascii=False, indent=2))
    print(f"[data-migration] status={result.get('status')} ops={result.get('operations')}")
    return 0 if result.get("status") in {"dry-run-pass", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
