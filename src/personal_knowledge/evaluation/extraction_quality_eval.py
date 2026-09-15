"""L1/L2 extraction quality evaluator (unit-level, versioned artifact)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.core.project_paths import AI_CONTEXT_DIR, UNIFIED_DB  # noqa: E402
from personal_knowledge.evaluation.eval_contracts import content_checksum, dump_json  # noqa: E402

PRIVACY_PATTERNS = [
    re.compile(r"(api[_-]?key|secret|password|token\s*[:=])", re.I),
    re.compile(r"\b\d{16,19}\b"),  # crude card-like
    re.compile(r"(护照|银行卡|支付密码|身份证号)"),
]


@dataclass
class Metric:
    name: str
    numerator: int
    denominator: int
    value: float | None
    sample_ids: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ratio(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return num / den


def _load_units(
    con: sqlite3.Connection,
    *,
    l2_only: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    where = "unit_id LIKE 'l2|%'" if l2_only else "1=1"
    sql = (
        f"SELECT unit_id, run_id, unit_type, subject, question, answer, confidence, "
        f"evidence_quote, source_message_ref, source_session_id, status "
        f"FROM knowledge_units WHERE {where} AND status IN ('current','staging','validated') "
        f"ORDER BY unit_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(con.execute(sql))


def _has_privacy_leak(text: str) -> bool:
    t = text or ""
    return any(p.search(t) for p in PRIVACY_PATTERNS)


def evaluate_extraction(
    db_path: Path = UNIFIED_DB,
    *,
    sample_limit: int = 50,
    human_labels: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute extraction-quality metrics with explicit numerators/denominators."""
    if not db_path.exists():
        return {"ok": False, "error": f"db missing: {db_path}", "generated_at": _utc()}

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    l1 = _load_units(con, l2_only=False)
    # L1 units are those not prefixed l2|
    l1_units = [u for u in l1 if not str(u["unit_id"]).startswith("l2|")]
    l2_units = [u for u in l1 if str(u["unit_id"]).startswith("l2|")]

    # Evidence coverage: has source_message_ref
    def coverage(units: list[sqlite3.Row]) -> Metric:
        ids = []
        num = 0
        for u in units:
            ok = bool(u["source_message_ref"] or u["evidence_quote"])
            if ok:
                num += 1
            else:
                ids.append(u["unit_id"])
        return Metric(
            name="evidence_coverage",
            numerator=num,
            denominator=len(units),
            value=_ratio(num, len(units)),
            sample_ids=ids[:20],
            notes="units missing both source_message_ref and evidence_quote",
        )

    # Schema completeness: required fields present
    def schema_ok(units: list[sqlite3.Row]) -> Metric:
        num = 0
        bad = []
        for u in units:
            if u["unit_type"] and u["subject"] and u["answer"]:
                num += 1
            else:
                bad.append(u["unit_id"])
        return Metric(
            "schema_completeness",
            num,
            len(units),
            _ratio(num, len(units)),
            bad[:20],
        )

    # L1 duplication heuristic: same subject+answer hash collisions within type
    def duplication(units: list[sqlite3.Row]) -> Metric:
        seen: dict[str, str] = {}
        dups = []
        for u in units:
            key = f"{u['unit_type']}|{(u['subject'] or '').strip().lower()}|{(u['answer'] or '').strip().lower()[:120]}"
            if key in seen:
                dups.append(u["unit_id"])
            else:
                seen[key] = u["unit_id"]
        # duplication rate = dups / total
        return Metric(
            "duplication_rate",
            len(dups),
            len(units),
            _ratio(len(dups), len(units)),
            dups[:30],
            notes="exact subject+type+answer-prefix collisions",
        )

    # Privacy leakage on sample
    sample = (l2_units[:sample_limit] if l2_units else l1_units[:sample_limit])
    leak_ids = []
    for u in sample:
        blob = " ".join(
            str(u[k] or "") for k in ("subject", "question", "answer", "evidence_quote")
        )
        if _has_privacy_leak(blob):
            leak_ids.append(u["unit_id"])
    privacy = Metric(
        "privacy_leakage_sample",
        len(leak_ids),
        len(sample),
        _ratio(len(leak_ids), len(sample)) if sample else None,
        leak_ids,
        notes=f"sample_limit={sample_limit}",
    )

    # Cross-turn necessity proxy for L2: multi-message evidence or session present
    cross_num = 0
    cross_ids = []
    for u in l2_units:
        quote = u["evidence_quote"] or ""
        # L2 extract stores session windows; count if answer references multi-hop markers
        if u["source_session_id"] and (len(quote) >= 40 or "；" in quote or "\n" in quote):
            cross_num += 1
            if len(cross_ids) < 30:
                cross_ids.append(u["unit_id"])
    cross_turn = Metric(
        "cross_turn_proxy",
        cross_num,
        len(l2_units),
        _ratio(cross_num, len(l2_units)),
        cross_ids,
        notes="proxy: session_id + non-trivial quote; not human gold",
    )

    # Human-labeled grounded precision if provided
    human_metrics: list[Metric] = []
    if human_labels:
        grounded = [h for h in human_labels if h.get("grounded") is True]
        unsupported = [h for h in human_labels if h.get("grounded") is False]
        human_metrics.append(
            Metric(
                "grounded_precision_human",
                len(grounded),
                len(human_labels),
                _ratio(len(grounded), len(human_labels)),
                [h.get("unit_id", "") for h in grounded[:20]],
            )
        )
        human_metrics.append(
            Metric(
                "unsupported_rate_human",
                len(unsupported),
                len(human_labels),
                _ratio(len(unsupported), len(human_labels)),
                [h.get("unit_id", "") for h in unsupported[:20]],
            )
        )

    metrics = {
        "l1": {
            "count": len(l1_units),
            "evidence_coverage": coverage(l1_units).to_dict(),
            "schema_completeness": schema_ok(l1_units).to_dict(),
            "duplication_rate": duplication(l1_units).to_dict(),
        },
        "l2": {
            "count": len(l2_units),
            "evidence_coverage": coverage(l2_units).to_dict(),
            "schema_completeness": schema_ok(l2_units).to_dict(),
            "duplication_rate": duplication(l2_units).to_dict(),
            "cross_turn_proxy": cross_turn.to_dict(),
        },
        "privacy": privacy.to_dict(),
        "human": [m.to_dict() for m in human_metrics],
    }

    privacy_hit = privacy.numerator
    ok = privacy_hit == 0
    report = {
        "generated_at": _utc(),
        "ok": ok,
        "version": "extraction_quality_v1",
        "db_path": str(db_path).replace("\\", "/"),
        "sample_limit": sample_limit,
        "metrics": metrics,
        "hard_fail": {
            "privacy_leakage": privacy_hit,
            "threshold": 0,
            "passed": privacy_hit == 0,
        },
        "checksum": content_checksum(metrics),
    }
    con.close()
    return report


def prepare_grounded_review(
    db_path: Path,
    out_path: Path,
    *,
    sample_size: int = 50,
    seed: str = "phase17-grounded-v1",
) -> dict[str, Any]:
    """Write a deterministic private L2 review packet without mutating the DB."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = _load_units(con, l2_only=True)
    con.close()
    ranked = sorted(
        rows,
        key=lambda row: hashlib.sha256(
            f"{seed}|{row['unit_id']}".encode("utf-8")
        ).hexdigest(),
    )[:sample_size]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in ranked:
            handle.write(
                json.dumps(
                    {
                        "unit_id": row["unit_id"],
                        "subject": row["subject"],
                        "question": row["question"],
                        "answer": row["answer"],
                        "evidence_quote": row["evidence_quote"],
                        "source_message_ref": row["source_message_ref"],
                        "grounded": None,
                        "reviewer_notes": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return {
        "out": str(out_path).replace("\\", "/"),
        "sample_size": len(ranked),
        "available_l2": len(rows),
        "seed": seed,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extraction quality eval")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    p.add_argument("--sample-limit", type=int, default=50)
    p.add_argument(
        "--human-labels",
        type=Path,
        default=None,
        help="optional JSONL with unit_id,grounded bool",
    )
    p.add_argument(
        "--prepare-human-template",
        type=Path,
        default=None,
        help="write a private deterministic L2 grounded-review JSONL template",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=AI_CONTEXT_DIR / "extraction_quality_v1.json",
    )
    args = p.parse_args(argv)

    if args.prepare_human_template:
        packet = prepare_grounded_review(
            args.db,
            args.prepare_human_template,
            sample_size=max(50, args.sample_limit),
        )
        print(f"[extraction-quality] prepared human review: {packet}")

    labels = None
    if args.human_labels and args.human_labels.exists():
        labels = [
            json.loads(line)
            for line in args.human_labels.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    report = evaluate_extraction(
        args.db, sample_limit=args.sample_limit, human_labels=labels
    )
    dump_json(args.out, report)
    print(f"[extraction-quality] ok={report.get('ok')} wrote {args.out}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
