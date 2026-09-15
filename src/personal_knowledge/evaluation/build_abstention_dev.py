"""Build a private abstention development set from independently labeled canary rows.

The source canary report contains labels and hashes but no query text. Queries are
resolved locally from the canonical knowledge database using the same hash scheme,
keeping the published report privacy-safe. The generated JSONL remains under gitignored
``var/runtime/private_evals`` and must never be used as frozen test data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from personal_knowledge.core.project_paths import ROOT, UNIFIED_DB

CANARY_REPORT = (
    ROOT
    / "var"
    / "reports"
    / "analysis"
    / "ai_context"
    / "ku_canary_ir_4cd8af4ad_20260716.json"
)
OUT = ROOT / "var" / "runtime" / "private_evals" / "abstention_dev_v1.private.jsonl"


def build_rows(
    report_path: Path = CANARY_REPORT,
    db_path: Path = UNIFIED_DB,
) -> list[dict[str, Any]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    query_by_hash: dict[str, str] = {}
    for (query,) in con.execute(
        "SELECT DISTINCT question FROM canonical_knowledge_units "
        "WHERE status='current' AND question IS NOT NULL AND length(question)>0"
    ):
        text = str(query or "").strip()
        if text:
            query_by_hash[hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]] = text
    con.close()
    rows: list[dict[str, Any]] = []
    for item in report.get("results") or []:
        if item.get("label") != "helpful":
            continue
        query_hash = str(item.get("query_hash") or "")
        query = query_by_hash.get(query_hash, "")
        returned_ids = [str(value) for value in (item.get("returned_ids") or []) if value]
        if not query or not returned_ids:
            continue
        short = query_hash[:12]
        rows.append(
            {
                "id": f"dev-positive-{short}",
                "split": "abstention_dev_v1",
                "query": query,
                "gold_unit_ids": [returned_ids[0]],
                "expected_abstain": False,
                "gold_provenance": "independent_canary_helpful",
                "scenario": "abstention_positive",
            }
        )
        nonce = "DEV-NO-EVIDENCE-" + hashlib.sha256(
            ("abstention-dev-v1:" + query_hash).encode("utf-8")
        ).hexdigest()[:16].upper()
        rows.append(
            {
                "id": f"dev-negative-{short}",
                "split": "abstention_dev_v1",
                "query": f"{query}\n仅当证据逐字包含校验码 {nonce} 时回答。",
                "expected_abstain": True,
                "gold_provenance": "derived_absent_nonce_v1",
                "scenario": "abstention_hard_negative",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    rows = build_rows()
    stats = {
        "rows": len(rows),
        "positive": sum(not row["expected_abstain"] for row in rows),
        "negative": sum(row["expected_abstain"] for row in rows),
        "output": str(OUT),
        "write": args.write,
    }
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats["positive"] >= 20 and stats["negative"] >= 20 else 1


if __name__ == "__main__":
    raise SystemExit(main())
