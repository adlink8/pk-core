"""Build and execute the signed Phase 19 consolidated recovery baseline.

This is deliberately independent of the historical Phase 19 journals.  Those
journals failed closed because some intermediate consumer bytes were never
recorded.  The consolidated manifest starts at the exact, observable
tools-forward state and owns the recoverable transition to the approved final
tree.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from personal_knowledge.governance.apply_source_migration import (
    _atomic_replace_bytes,
    _inside,
    _reject_reparse,
)

ROOT = Path(__file__).resolve().parents[3]
SCOPE = "phase19-consolidated-recovery"
ADDENDUM_SCOPE = "phase19-consolidated-recovery-addendum"
BACKUP_DIR = ".migration-backup-recovery"


class RecoveryError(RuntimeError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(payload: dict) -> dict:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {**unsigned, "manifest_sha256": _hash(canonical)}


def _load_signed(path: Path, *, expected_scope: str | None = None) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if _sign(payload)["manifest_sha256"] != payload.get("manifest_sha256"):
        raise RecoveryError(f"manifest checksum mismatch: {path}")
    if expected_scope is not None and payload.get("scope") != expected_scope:
        raise RecoveryError(f"unexpected manifest scope: {path}")
    return payload


def _bytes(path: str) -> bytes:
    target = _inside(ROOT, path)
    if not target.is_file():
        raise RecoveryError(f"file missing: {path}")
    return target.read_bytes()


def _write(path: Path, data: bytes) -> None:
    if path.exists():
        _atomic_replace_bytes(path, data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        stage = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(stage, path)


def build_addendum(
    base_manifest: Path,
    paths: list[str],
    output: Path,
    existing_addendum: Path | None = None,
) -> dict:
    """Capture approved fixes made after a consolidated afterstate was built."""
    base = _load_signed(base_manifest, expected_scope=SCOPE)
    expected: dict[str, tuple[bytes | None, str | None]] = {}
    for move in base["moves"]:
        source = Path(move["source"])
        backup = ROOT / BACKUP_DIR / (
            hashlib.sha256(move["source"].encode()).hexdigest() + source.suffix
        )
        data = backup.read_bytes() if backup.is_file() else None
        expected[move["target"]] = (data, move["sha256"])
    for rewrite in base["rewrites"]:
        expected[rewrite["path"]] = (
            base64.b64decode(rewrite["after_b64"]),
            rewrite["after_sha256"],
        )
    entries = []
    if existing_addendum is not None:
        existing = _load_signed(existing_addendum, expected_scope=ADDENDUM_SCOPE)
        entries.extend(existing["entries"])
    existing_by_path = {entry["path"]: entry for entry in entries}
    for path in sorted(set(paths)):
        if path in existing_by_path:
            entry = existing_by_path[path]
            after = _bytes(path)
            entry["after_sha256"] = _hash(after)
            entry["after_b64"] = base64.b64encode(after).decode("ascii")
            continue
        before, before_sha = expected.get(path, (None, None))
        if before is None:
            raise RecoveryError(f"base manifest does not embed after bytes for: {path}")
        if _hash(before) != before_sha:
            raise RecoveryError(f"base embedded after bytes drift: {path}")
        after = _bytes(path)
        if after == before:
            continue
        entries.append({
            "path": path,
            "before_sha256": before_sha,
            "before_b64": base64.b64encode(before).decode("ascii"),
            "after_sha256": _hash(after),
            "after_b64": base64.b64encode(after).decode("ascii"),
        })
    payload = _sign({
        "schema_version": "1.0",
        "scope": ADDENDUM_SCOPE,
        "approval": "user-approved-complete-phase19",
        "base_manifest_sha256": base["manifest_sha256"],
        "entries": entries,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def build_manifest(
    app_manifest: Path,
    historical_app_manifest: Path,
    final_manifest: Path,
    addendum_manifest: Path | None,
    output: Path,
) -> dict:
    apps = _load_signed(app_manifest, expected_scope="tracked-text-source-only")
    historical_apps = _load_signed(
        historical_app_manifest, expected_scope="tracked-text-source-only"
    )
    final = _load_signed(final_manifest, expected_scope="phase19-final-fix-rewrite-only")

    moves: list[dict] = []
    virtual: dict[str, bytes | None] = {}
    for entry in apps["entries"]:
        source, target = entry["source"], entry["target"]
        data = _bytes(source)
        if _hash(data) != entry["sha256"]:
            raise RecoveryError(f"recovery source drift: {source}")
        if _inside(ROOT, target).exists():
            raise RecoveryError(f"recovery target already exists: {target}")
        moves.append({"source": source, "target": target, "sha256": entry["sha256"]})
        virtual[source] = None
        virtual[target] = data

    def virtual_read(path: str) -> bytes | None:
        if path in virtual:
            return virtual[path]
        target = _inside(ROOT, path)
        return target.read_bytes() if target.is_file() else None

    rewrites: list[dict] = []

    def append_rewrite(path: str, before: bytes | None, after: bytes, provenance: str) -> None:
        if before == after:
            return
        rewrites.append({
            "path": path,
            "before_exists": before is not None,
            "before_sha256": _hash(before) if before is not None else None,
            "before_b64": base64.b64encode(before).decode("ascii") if before is not None else None,
            "after_sha256": _hash(after),
            "after_b64": base64.b64encode(after).decode("ascii"),
            "provenance": provenance,
        })
        virtual[path] = after

    for rewrite in apps.get("consumer_rewrites", []):
        path = rewrite.get("effective_path_after_move", rewrite["path"])
        before = virtual_read(path)
        if before is None or _hash(before) != rewrite["before_sha256"]:
            raise RecoveryError(f"consumer prestate drift while building: {path}")
        after = rewrite["after_text"].encode("utf-8")
        if _hash(after) != rewrite["after_sha256"]:
            raise RecoveryError(f"consumer afterstate hash mismatch: {path}")
        append_rewrite(path, before, after, "recovery-consumer-rewrite")

    # The historical manifest still owns the approved *after* bytes even though
    # its undocumented intermediate before bytes cannot be replayed.  Overlay
    # those recorded outcomes on the exact recovery prestate; this is what
    # restores canonical package imports in the relocated tests and consumers.
    for rewrite in historical_apps.get("consumer_rewrites", []):
        path = rewrite.get("effective_path_after_move", rewrite["path"])
        before = virtual_read(path)
        if before is None:
            raise RecoveryError(f"historical consumer target missing: {path}")
        after = rewrite["after_text"].encode("utf-8")
        if _hash(after) != rewrite["after_sha256"]:
            raise RecoveryError(f"historical consumer afterstate hash mismatch: {path}")
        append_rewrite(path, before, after, "historical-approved-afterstate")

    for entry in final["entries"]:
        path = entry["path"]
        before = virtual_read(path)
        after = base64.b64decode(entry["after_b64"])
        if _hash(after) != entry["after_sha256"]:
            raise RecoveryError(f"final-fix afterstate hash mismatch: {path}")
        append_rewrite(path, before, after, "approved-final-fix")

    addendum = None
    if addendum_manifest is not None:
        addendum = _load_signed(addendum_manifest, expected_scope=ADDENDUM_SCOPE)
        for entry in addendum["entries"]:
            path = entry["path"]
            before = virtual_read(path)
            if before is None or _hash(before) != entry["before_sha256"]:
                raise RecoveryError(f"addendum prestate drift while building: {path}")
            after = base64.b64decode(entry["after_b64"])
            if _hash(after) != entry["after_sha256"]:
                raise RecoveryError(f"addendum afterstate hash mismatch: {path}")
            append_rewrite(path, before, after, "approved-recovery-addendum")

    payload = _sign({
        "schema_version": "1.0",
        "scope": SCOPE,
        "approval": "user-approved-isolated-migration-and-completion",
        "baseline": "exact-observable-tools-forward-prestate",
        "historical_debt": (
            "HIGH: pre-consolidation Phase 19 replay cannot reproduce undocumented "
            "intermediate consumer bytes; original manifests remain audit evidence only"
        ),
        "inputs": {
            "apps_manifest": str(app_manifest.as_posix()),
            "apps_manifest_sha256": apps["manifest_sha256"],
            "historical_apps_manifest": str(historical_app_manifest.as_posix()),
            "historical_apps_manifest_sha256": historical_apps["manifest_sha256"],
            "final_manifest": str(final_manifest.as_posix()),
            "final_manifest_sha256": final["manifest_sha256"],
            "addendum_manifest": str(addendum_manifest.as_posix()) if addendum_manifest else None,
            "addendum_manifest_sha256": addendum["manifest_sha256"] if addendum else None,
        },
        "moves": moves,
        "rewrites": rewrites,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def load_manifest(path: Path) -> dict:
    return _load_signed(path, expected_scope=SCOPE)


def verify_before(payload: dict) -> None:
    move_targets = {move["target"] for move in payload["moves"]}
    for move in payload["moves"]:
        source, target = _inside(ROOT, move["source"]), _inside(ROOT, move["target"])
        _reject_reparse(source)
        _reject_reparse(target.parent)
        if not source.is_file() or _hash(source.read_bytes()) != move["sha256"]:
            raise RecoveryError(f"move prestate drift: {move['source']}")
        if target.exists():
            raise RecoveryError(f"move target must be absent: {move['target']}")
    # Only rewrites not produced by a move can be checked before moves execute.
    seen: set[str] = set()
    for rewrite in payload["rewrites"]:
        path = rewrite["path"]
        if path in seen or path in move_targets:
            seen.add(path)
            continue
        target = _inside(ROOT, path)
        if rewrite["before_exists"]:
            if not target.is_file() or _hash(target.read_bytes()) != rewrite["before_sha256"]:
                raise RecoveryError(f"rewrite prestate drift: {path}")
        elif target.exists():
            raise RecoveryError(f"rewrite prestate expected absent: {path}")
        seen.add(path)


def verify_after(payload: dict) -> None:
    for move in payload["moves"]:
        if _inside(ROOT, move["source"]).exists():
            raise RecoveryError(f"source remains after move: {move['source']}")
    expected: dict[str, str] = {move["target"]: move["sha256"] for move in payload["moves"]}
    for rewrite in payload["rewrites"]:
        expected[rewrite["path"]] = rewrite["after_sha256"]
    for path, checksum in expected.items():
        target = _inside(ROOT, path)
        if not target.is_file() or _hash(target.read_bytes()) != checksum:
            raise RecoveryError(f"afterstate drift: {path}")


def _journal(path: Path, records: list[dict]) -> None:
    path = path.resolve(strict=False)
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise RecoveryError("journal escapes workspace") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def apply(payload: dict, journal: Path) -> None:
    verify_before(payload)
    records: list[dict] = [{"kind": "header", "manifest_sha256": payload["manifest_sha256"]}]
    changed: list[dict] = []
    try:
        backup_root = ROOT / BACKUP_DIR
        backup_root.mkdir(exist_ok=True)
        for move in payload["moves"]:
            source, target = _inside(ROOT, move["source"]), _inside(ROOT, move["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (hashlib.sha256(move["source"].encode()).hexdigest() + source.suffix)
            if backup.exists():
                raise RecoveryError(f"dirty recovery backup: {backup.relative_to(ROOT)}")
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                stage = Path(handle.name)
            shutil.copy2(source, stage)
            if _hash(stage.read_bytes()) != move["sha256"]:
                raise RecoveryError(f"staged move hash mismatch: {move['source']}")
            os.replace(source, backup)
            os.replace(stage, target)
            record = {"kind": "move", **move, "backup": str(backup.relative_to(ROOT))}
            records.append(record)
            changed.append(record)
        for rewrite in payload["rewrites"]:
            path = _inside(ROOT, rewrite["path"])
            before = path.read_bytes() if path.is_file() else None
            if rewrite["before_exists"]:
                if before is None or _hash(before) != rewrite["before_sha256"]:
                    raise RecoveryError(f"rewrite prestate drift during apply: {rewrite['path']}")
            elif path.exists():
                raise RecoveryError(f"rewrite expected absent during apply: {rewrite['path']}")
            _write(path, base64.b64decode(rewrite["after_b64"]))
            record = {"kind": "rewrite", "path": rewrite["path"],
                      "before_sha256": rewrite["before_sha256"],
                      "after_sha256": rewrite["after_sha256"]}
            records.append(record)
            changed.append({"kind": "rewrite", **rewrite})
        verify_after(payload)
        _journal(journal, records)
    except Exception:
        _rollback_changed(changed)
        raise


def _rollback_changed(changed: list[dict]) -> None:
    for record in reversed(changed):
        if record["kind"] == "rewrite":
            path = _inside(ROOT, record["path"])
            if record["before_exists"]:
                _write(path, base64.b64decode(record["before_b64"]))
            elif path.exists():
                path.unlink()
            continue
        source, target = _inside(ROOT, record["source"]), _inside(ROOT, record["target"])
        backup = _inside(ROOT, record["backup"])
        if target.exists():
            target.unlink()
        source.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            os.replace(backup, source)


def rollback(payload: dict) -> None:
    verify_after(payload)
    for rewrite in reversed(payload["rewrites"]):
        path = _inside(ROOT, rewrite["path"])
        if rewrite["before_exists"]:
            _write(path, base64.b64decode(rewrite["before_b64"]))
        elif path.exists():
            path.unlink()
    for move in reversed(payload["moves"]):
        source, target = _inside(ROOT, move["source"]), _inside(ROOT, move["target"])
        backup = ROOT / BACKUP_DIR / (hashlib.sha256(move["source"].encode()).hexdigest() + source.suffix)
        if not target.is_file() or _hash(target.read_bytes()) != move["sha256"]:
            raise RecoveryError(f"move target drift during rollback: {move['target']}")
        if not backup.is_file() or _hash(backup.read_bytes()) != move["sha256"]:
            raise RecoveryError(f"recovery backup missing/drifted: {backup.relative_to(ROOT)}")
        target.unlink()
        source.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, source)
    verify_before(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--app-manifest", type=Path)
    parser.add_argument("--historical-app-manifest", type=Path)
    parser.add_argument("--final-manifest", type=Path)
    parser.add_argument("--addendum-manifest", type=Path)
    parser.add_argument("--existing-addendum", type=Path)
    parser.add_argument("--base-manifest", type=Path)
    parser.add_argument("--paths", nargs="*")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--build-addendum", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    mode.add_argument("--verify", choices=["before", "after"])
    args = parser.parse_args()
    if args.build_addendum:
        if args.base_manifest is None or not args.paths:
            parser.error("--build-addendum requires --base-manifest and --paths")
        payload = build_addendum(
            args.base_manifest,
            args.paths,
            args.manifest,
            args.existing_addendum,
        )
        print(f"built addendum entries={len(payload['entries'])} "
              f"sha256={payload['manifest_sha256']}")
        return 0
    if args.build:
        if (
            args.app_manifest is None
            or args.historical_app_manifest is None
            or args.final_manifest is None
        ):
            parser.error(
                "--build requires --app-manifest, --historical-app-manifest "
                "and --final-manifest"
            )
        payload = build_manifest(
            args.app_manifest,
            args.historical_app_manifest,
            args.final_manifest,
            args.addendum_manifest,
            args.manifest,
        )
        print(f"built moves={len(payload['moves'])} rewrites={len(payload['rewrites'])} "
              f"sha256={payload['manifest_sha256']}")
        return 0
    payload = load_manifest(args.manifest)
    if args.apply:
        if args.journal is None:
            parser.error("--apply requires --journal")
        apply(payload, args.journal)
    elif args.rollback:
        rollback(payload)
    elif args.verify == "before":
        verify_before(payload)
    else:
        verify_after(payload)
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
