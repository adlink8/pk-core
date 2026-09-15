"""Signed rewrite-only overlay for Phase 19 post-migration fixes.

The original four move manifests remain immutable. This overlay reconstructs
their exact combined afterstate, records every subsequent changed byte, and
supports capture/rollback/apply without claiming those edits were part of an
earlier migration.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

from personal_knowledge.governance.apply_source_migration import _atomic_replace_bytes

ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = ["canonical-src", "root-shims", "tools", "apps-assets-docs-tests"]
JOURNALS = [
    "source-canonical-src.journal.jsonl",
    "source-root-shims.journal.jsonl",
    "source-tools.journal.jsonl",
    "source-apps-assets-docs-tests.journal.jsonl",
]
EXTRA_NEW = {
    "src/personal_knowledge/governance/preflight.py",
    "src/personal_knowledge/governance/reconcile_phase19.py",
    "tests/governance/test_phase19_default_paths.py",
}


class OverlayError(RuntimeError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: str) -> Path:
    target = (ROOT / path).resolve(strict=False)
    try:
        target.relative_to(ROOT)
    except ValueError as exc:
        raise OverlayError(f"path escapes workspace: {path}") from exc
    return target


def _write(path: Path, data: bytes) -> None:
    if path.exists():
        _atomic_replace_bytes(path, data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        stage = Path(handle.name)
        handle.write(data)
    os.replace(stage, path)


def _sign(payload: dict) -> dict:
    unsigned = {k: v for k, v in payload.items() if k != "manifest_sha256"}
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {**unsigned, "manifest_sha256": _hash(canonical)}


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    claimed = payload.get("manifest_sha256")
    if _sign(payload)["manifest_sha256"] != claimed:
        raise OverlayError("final-fix manifest checksum mismatch")
    if payload.get("scope") != "phase19-final-fix-rewrite-only":
        raise OverlayError("unexpected final-fix scope")
    return payload


def reconstruct_original_afterstate() -> dict[str, bytes]:
    after_by_hash: dict[str, bytes] = {}
    for name in MANIFESTS:
        payload = json.loads((ROOT / "governance/manifests/source" / f"{name}.json").read_text(encoding="utf-8"))
        rewrites = list(payload.get("consumer_rewrites", []))
        for entry in payload["entries"]:
            rewrites.extend(entry.get("rewrites", []))
        for rewrite in rewrites:
            data = rewrite["after_text"].encode("utf-8")
            after_by_hash[_hash(data)] = data

    expected: dict[str, bytes] = {}
    for journal_name in JOURNALS:
        journal = ROOT / "var/runtime/migration" / journal_name
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "rewrite":
                try:
                    expected[record["path"]] = after_by_hash[record["after_sha256"]]
                except KeyError as exc:
                    raise OverlayError(f"missing immutable after bytes for {record['path']}") from exc
            else:
                expected.pop(record["source"], None)
                expected[record["target"]] = (ROOT / record["backup"]).read_bytes()
    return expected


def build_manifest(output: Path) -> dict:
    expected = reconstruct_original_afterstate()
    entries = []
    for path, before in sorted(expected.items()):
        target = _inside(path)
        if not target.is_file():
            raise OverlayError(f"expected current file missing: {path}")
        after = target.read_bytes()
        if after == before:
            continue
        entries.append({
            "path": path,
            "before_exists": True,
            "before_sha256": _hash(before),
            "before_b64": base64.b64encode(before).decode("ascii"),
            "after_sha256": _hash(after),
            "after_b64": base64.b64encode(after).decode("ascii"),
            "prestate_source": "replayed-signed-cohort-afterstate",
        })
    for path in sorted(EXTRA_NEW):
        target = _inside(path)
        if not target.is_file():
            raise OverlayError(f"new final-fix file missing: {path}")
        after = target.read_bytes()
        entries.append({
            "path": path,
            "before_exists": False,
            "before_sha256": None,
            "before_b64": None,
            "after_sha256": _hash(after),
            "after_b64": base64.b64encode(after).decode("ascii"),
            "prestate_source": "absent-before-final-fix",
        })
    payload = _sign({
        "schema_version": "1.0",
        "scope": "phase19-final-fix-rewrite-only",
        "approval": "user-approved-preserve-and-complete",
        "original_manifests": MANIFESTS,
        "entries": entries,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def verify(payload: dict, state: str) -> None:
    for entry in payload["entries"]:
        path = _inside(entry["path"])
        if state == "after":
            if not path.is_file() or _hash(path.read_bytes()) != entry["after_sha256"]:
                raise OverlayError(f"afterstate drift: {entry['path']}")
        elif entry["before_exists"]:
            if not path.is_file() or _hash(path.read_bytes()) != entry["before_sha256"]:
                raise OverlayError(f"prestate drift: {entry['path']}")
        elif path.exists():
            raise OverlayError(f"prestate expected absent: {entry['path']}")


def capture(payload: dict, journal: Path) -> None:
    verify(payload, "after")
    journal = journal.resolve(strict=False)
    try:
        journal.relative_to(ROOT)
    except ValueError as exc:
        raise OverlayError("journal escapes workspace") from exc
    records = [{"kind": "header", "manifest_sha256": payload["manifest_sha256"], "captured_existing_afterstate": True}]
    records.extend({"kind": "rewrite", "path": e["path"], "before_sha256": e["before_sha256"], "after_sha256": e["after_sha256"]} for e in payload["entries"])
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def rollback(payload: dict) -> None:
    verify(payload, "after")
    for entry in reversed(payload["entries"]):
        path = _inside(entry["path"])
        if entry["before_exists"]:
            _atomic_replace_bytes(path, base64.b64decode(entry["before_b64"]))
        else:
            path.unlink()
    verify(payload, "before")


def apply(payload: dict) -> None:
    verify(payload, "before")
    changed: list[dict] = []
    try:
        for entry in payload["entries"]:
            path = _inside(entry["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, base64.b64decode(entry["after_b64"]))
            changed.append(entry)
        verify(payload, "after")
    except Exception:
        for entry in reversed(changed):
            path = _inside(entry["path"])
            if entry["before_exists"]:
                _write(path, base64.b64decode(entry["before_b64"]))
            elif path.exists():
                path.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--capture", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    mode.add_argument("--verify", choices=["before", "after"])
    args = parser.parse_args()
    if args.build:
        payload = build_manifest(args.manifest)
        print(f"built final-fix entries={len(payload['entries'])} sha256={payload['manifest_sha256']}")
        return 0
    payload = load_manifest(args.manifest)
    if args.capture:
        if args.journal is None:
            parser.error("--capture requires --journal")
        capture(payload, args.journal)
    elif args.apply:
        apply(payload)
    elif args.rollback:
        rollback(payload)
    else:
        verify(payload, args.verify)
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
