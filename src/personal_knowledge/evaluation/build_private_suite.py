"""Build private comprehensive suite from frozen/holdout + synthetic shells.

Private output is gitignored. Does not modify live DBs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import ROOT  # noqa: E402
from personal_knowledge.evaluation.eval_contracts import audit_dataset, load_cases_jsonl  # noqa: E402
from personal_knowledge.evaluation.review_packets import (  # noqa: E402
    GOLD_IMPORT,
    GOLD_MANIFEST,
    checksum,
)

ASSET_DIR = ROOT / "assets" / "evals" / "knowledge_units"
PRIVATE_SOURCE_DIR = ROOT / "integration" / "evals" / "knowledge_units"
OUT_DIR = ROOT / "var" / "runtime" / "private_evals"
SYN = ASSET_DIR / "comprehensive_v1.synthetic.jsonl"
FROZEN = PRIVATE_SOURCE_DIR / "frozen_test_queries.private.jsonl"
HOLDOUT = ASSET_DIR / "holdout_15_02.synthetic.jsonl"


def load_reviewed_gold() -> list[dict]:
    if not GOLD_IMPORT.exists() and not GOLD_MANIFEST.exists():
        return []
    if not GOLD_IMPORT.exists() or not GOLD_MANIFEST.exists():
        raise ValueError("reviewed Gold import and manifest must exist together")
    rows = [
        json.loads(line)
        for line in GOLD_IMPORT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = json.loads(GOLD_MANIFEST.read_text(encoding="utf-8"))
    if checksum(rows) != manifest.get("import_checksum"):
        raise ValueError("reviewed Gold import checksum mismatch")
    for row in rows:
        if row.get("gold_provenance") not in {"human_reviewed_v1", "llm_reviewed_v1"}:
            raise ValueError("reviewed Gold provenance invalid")
        if not row.get("reviewer_id") or not row.get("reviewed_at"):
            raise ValueError("reviewed Gold provenance missing")
        if row.get("gold_provenance") == "llm_reviewed_v1" and not all(
            row.get(key) for key in ("model_id", "review_run_id", "prompt_version")
        ):
            raise ValueError("LLM Gold audit provenance missing")
        if str(row.get("split") or "").startswith("synthetic"):
            raise ValueError("synthetic row cannot enter human Gold")
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=OUT_DIR / "comprehensive_v1.private.jsonl")
    args = p.parse_args(argv)

    rows: list[dict] = []
    # migrate frozen as regression_slice
    if FROZEN.exists():
        for line in FROZEN.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            c["scenario"] = c.get("scenario") or c.get("group") or "regression_slice"
            c["suite_tag"] = c.get("suite_tag") or "regression_slice"
            c["gold_provenance"] = c.get("gold_provenance") or "frozen_test_v1"
            c.setdefault("split", "frozen_test")
            rows.append(c)
    if HOLDOUT.exists():
        for line in HOLDOUT.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            c["scenario"] = c.get("suite_tag") or c.get("scenario") or "holdout"
            c["gold_provenance"] = "holdout_15_02"
            rows.append(c)
    # fill remaining families from synthetic so structural minima hold
    if SYN.exists():
        for line in SYN.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            rows.append(c)
    rows.extend(load_reviewed_gold())

    # dedupe by id (frozen wins)
    seen = set()
    uniq = []
    for c in rows:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        uniq.append(c)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in uniq) + "\n",
        encoding="utf-8",
    )

    # structural audit via contracts (re-parse)
    cases = load_cases_jsonl(args.out)
    audit = audit_dataset(cases)
    print(f"[private-suite] wrote {args.out} n={len(uniq)} audit_ok={audit['ok']}")
    if audit["errors"]:
        print(" errors:", audit["errors"])
    if audit["warnings"][:5]:
        print(" warnings sample:", audit["warnings"][:5])
    return 0 if audit["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
