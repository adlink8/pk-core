"""Build Phase 20 cohort preview manifests (dry-run only, unapproved)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


def _ck(payload: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_disposition() -> dict[str, Any]:
    path = ROOT / "governance" / "manifests" / "data_disposition.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _infer_type(path: str) -> str:
    p = path.lower()
    if p.endswith(".sqlite") or p.endswith(".db"):
        return "sqlite"
    if p.endswith(".duckdb"):
        return "duckdb"
    if path.endswith("knowledge_index_active.txt"):
        return "chroma_pointer"
    # prefer directory ops for top-level cohort roots only
    return "files"


def build_cohort(cohort: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    # Collapse to top-level directory operations where possible for preview size
    roots: dict[str, dict[str, Any]] = {}
    file_ops: list[dict[str, Any]] = []
    for e in entries:
        if e.get("disposition") != "relocate":
            continue
        src, tgt = e["path"], e.get("target") or ""
        if not tgt:
            continue
        # top two segments as directory cohort unit when directory
        parts = src.split("/")
        if e.get("node_type") == "directory" and len(parts) <= 3:
            roots[src] = {
                "id": f"{cohort}:{src}",
                "type": "directory",
                "source": src,
                "target": tgt,
                "inverse": {"source": tgt, "target": src},
                "backup": f"{tgt}.bak-phase20",
                "stage": f"{tgt}.stage-phase20",
                "backup_source": f"{src}.bak-phase20",
            }
        elif e.get("node_type") == "file" and (
            src.endswith(".sqlite")
            or src.endswith(".duckdb")
            or src.endswith("knowledge_index_active.txt")
        ):
            kind = _infer_type(src)
            op: dict[str, Any] = {
                "id": f"{cohort}:{src}",
                "type": kind,
                "source": src,
                "target": tgt,
                "inverse": {"source": tgt, "target": src},
                "backup": f"{tgt}.bak-phase20",
                "stage": f"{tgt}.stage-phase20",
            }
            if kind == "chroma_pointer":
                # value stays same — pointer file relocates as files instead
                op["type"] = "files"
            file_ops.append(op)

    # Prefer directory roots that cover many files; if empty use first-level prefixes
    operations = list(roots.values()) + file_ops
    if not operations:
        # synthesize high-level directory moves from unique first two path segments
        buckets: dict[str, str] = {}
        for e in entries:
            if e.get("disposition") != "relocate":
                continue
            src = e["path"]
            tgt = e.get("target") or ""
            top = "/".join(src.split("/")[:2]) if "/" in src else src
            if top not in buckets:
                # map top to corresponding target prefix
                t_top = "/".join(tgt.split("/")[:3]) if tgt else ""
                buckets[top] = t_top or tgt
        for src, tgt in sorted(buckets.items()):
            operations.append(
                {
                    "id": f"{cohort}:{src}",
                    "type": "directory",
                    "source": src,
                    "target": tgt,
                    "inverse": {"source": tgt, "target": src},
                    "backup": f"{tgt}.bak-phase20",
                    "stage": f"{tgt}.stage-phase20",
                    "backup_source": f"{src}.bak-phase20",
                    "note": "preview-level directory op; refine before apply",
                }
            )

    payload = {
        "schema_version": 1,
        "scope": "phase20-data-cohort",
        "cohort": cohort,
        "approved": False,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry_count_relocate": sum(1 for e in entries if e.get("disposition") == "relocate"),
        "operations": operations,
        "notes": [
            "PREVIEW ONLY — approved=false",
            "Human checkpoint must set approved=true and verify capacity/backup before --apply",
            "No real data movement in this step",
        ],
    }
    payload["manifest_sha256"] = _ck(payload)
    return payload


def main() -> int:
    disp = _load_disposition()
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in disp["entries"]:
        if e.get("disposition") != "relocate":
            continue
        by_cohort[e.get("cohort") or "unknown"].append(e)

    out_dir = ROOT / "governance" / "manifests" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "agent-google-imports": "agent-google-imports.json",
        "var": "var.json",
        "archive": "archive.json",
    }
    for cohort, filename in mapping.items():
        entries = by_cohort.get(cohort, [])
        payload = build_cohort(cohort, entries)
        path = out_dir / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"[preview] {cohort}: relocate_nodes={payload['entry_count_relocate']} "
            f"ops={len(payload['operations'])} sha={payload['manifest_sha256'][:12]}… → {path}"
        )

    # retain tooling note
    retain = sum(1 for e in disp["entries"] if e.get("disposition") == "retain-in-place")
    print(f"[preview] retain-in-place nodes={retain}; protected-external=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
