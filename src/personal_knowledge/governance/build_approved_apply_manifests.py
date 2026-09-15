"""Build non-overlapping Phase 20 apply manifests (human-approved cutover).

Only top-level roots — no nested child ops that would double-move after parent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


def _ck(payload: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _dir_op(cohort: str, source: str, target: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"{cohort}:{source}",
        "type": "directory",
        "source": source,
        "target": target,
        "inverse": {"source": target, "target": source},
        "backup": f"{target}.bak-phase20",
        "stage": f"{target}.stage-phase20",
        "backup_source": f"{source}.bak-phase20",
        **extra,
    }


def _sqlite_op(cohort: str, source: str, target: str) -> dict[str, Any]:
    return {
        "id": f"{cohort}:{source}",
        "type": "sqlite",
        "source": source,
        "target": target,
        "inverse": {"source": target, "target": source},
        "backup": f"{target}.bak-phase20",
        "stage": f"{target}.stage-phase20",
    }


def _duckdb_op(cohort: str, source: str, target: str) -> dict[str, Any]:
    return {
        "id": f"{cohort}:{source}",
        "type": "duckdb",
        "source": source,
        "target": target,
        "inverse": {"source": target, "target": source},
        "backup": f"{target}.bak-phase20",
        "stage": f"{target}.stage-phase20",
    }


def _files_op(cohort: str, source: str, target: str) -> dict[str, Any]:
    return {
        "id": f"{cohort}:{source}",
        "type": "files",
        "source": source,
        "target": target,
        "inverse": {"source": target, "target": source},
        "backup": f"{target}.bak-phase20",
        "stage": f"{target}.stage-phase20",
        "backup_source": f"{source}.bak-phase20",
    }


def build(cohort: str, operations: list[dict[str, Any]], notes: list[str]) -> dict[str, Any]:
    # Drop ops whose source does not exist (already moved / absent)
    existing = []
    for op in operations:
        src = ROOT / op["source"]
        if not src.exists():
            # dotted alias
            alt = ROOT / f".{op['source']}" if not op["source"].startswith(".") else ROOT / op["source"].lstrip(".")
            if alt.exists():
                op = {**op, "source": str(alt.relative_to(ROOT)).replace("\\", "/"), "resolved_from": op["source"]}
                # fix inverse/backup_source accordingly
                op["inverse"] = {"source": op["target"], "target": op["source"]}
                op["backup_source"] = f"{op['source']}.bak-phase20"
                existing.append(op)
            continue
        existing.append(op)

    payload = {
        "schema_version": 1,
        "scope": "phase20-data-cohort",
        "cohort": cohort,
        "approved": True,
        "approved_by": "user",
        "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approval_note": "全部批准 — full cohort apply authorization 2026-07-13",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operations": existing,
        "notes": notes,
    }
    payload["manifest_sha256"] = _ck(payload)
    return payload


def main() -> int:
    out = ROOT / "governance" / "manifests" / "data"
    out.mkdir(parents=True, exist_ok=True)

    # --- agent-google-imports: non-overlapping roots only ---
    agi = build(
        "agent-google-imports",
        [
            # Agent: only private db tree is material; move whole Agent to keep README co-located
            _dir_op("agent-google-imports", "Agent", "data/canonical/agent"),
            _dir_op("agent-google-imports", "Google/raw", "data/raw/google"),
            _dir_op(
                "agent-google-imports",
                "Google/structured",
                "data/canonical/google/structured",
            ),
            _dir_op("agent-google-imports", "imports", "data/imports"),
            # Google/README stays or goes with leftover Google shell — if only README left, move shell
            _dir_op(
                "agent-google-imports",
                "Google",
                "data/canonical/google/_shell",
            ),
        ],
        notes=[
            "Non-overlapping top-level roots only",
            "Google/raw and Google/structured moved first conceptually; executor order is list order — "
            "Google shell op is last so children move first",
            "User approved full Phase 20 apply 2026-07-13",
        ],
    )
    # Fix order: children before parent shell
    agi["operations"] = [
        o
        for o in agi["operations"]
        if o["source"] != "Google"
    ] + [o for o in agi["operations"] if o["source"] == "Google"]
    agi["manifest_sha256"] = _ck(agi)

    # --- var: specific subtrees only — NEVER whole integration/ ---
    # Prefer type-aware moves for DB files under integration/db
    var_ops: list[dict[str, Any]] = [
        _dir_op("var", "integration/db", "var/db", allow_existing_target=False),
        _dir_op("var", "integration/runtime", "var/runtime", allow_existing_target=True),
        _dir_op("var", "integration/analysis", "var/reports/analysis"),
        _dir_op("var", "integration/raw_index", "var/db/raw_index"),
        _dir_op("var", "integration/structured", "var/db/structured"),
        _dir_op("var", "logs", "var/logs"),
    ]
    # residual integration root logs/json (not directories already moved)
    for name in (
        "integration/api.stderr.log",
        "integration/api.stdout.log",
        "integration/dashboard.stderr.log",
        "integration/dashboard.stdout.log",
        "integration/classification_summary.json",
    ):
        if (ROOT / name).exists():
            base = Path(name).name
            var_ops.append(_files_op("var", name, f"var/logs/{base}" if name.endswith(".log") else f"var/reports/{base}"))

    var = build(
        "var",
        var_ops,
        notes=[
            "Does NOT move integration/scripts|evals|apps|lib|prompts|docs",
            "var/runtime may already exist (migration journals); allow_existing_target on runtime op uses merge-via-backup protocol",
            "User approved full Phase 20 apply 2026-07-13",
        ],
    )

    # --- archive ---
    archive = build(
        "archive",
        [
            _dir_op("archive", "_recycle", "archive/quarantine/_recycle"),
            _dir_op("archive", ".gsd", "archive/planning/.gsd"),
            _dir_op("archive", ".ai-bridge", "archive/vendor-reference/.ai-bridge"),
        ],
        notes=[
            "_recycle is large; stage-copy requires free capacity ~1.05x",
            "No R4 body content is printed; directory metadata move only",
            "User approved full Phase 20 apply 2026-07-13",
        ],
    )

    for name, payload in (
        ("agent-google-imports.apply.json", agi),
        ("var.apply.json", var),
        ("archive.apply.json", archive),
    ):
        path = out / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[apply-manifest] {name} ops={len(payload['operations'])} sha={payload['manifest_sha256'][:16]} approved={payload['approved']}")
        for op in payload["operations"]:
            print(f"  {op['type']:10} {op['source']} -> {op['target']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
