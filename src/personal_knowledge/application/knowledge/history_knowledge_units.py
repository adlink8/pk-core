"""Phase 22-02: growth-line history query for canonical knowledge units.

Read-only surface listing multi-version units for a subject (current +
superseded + deprecated + conflict). Does not touch active pointer or DELETE.

Usage::

    python -m personal_knowledge.application.knowledge.history_knowledge_units \\
        --subject "Shell" --limit 20
    pk-ku history --subject "Shell" --limit 5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from personal_knowledge.core.project_paths import UNIFIED_DB

# Growth-line lifecycles (retrieval default is current-only; history is explicit).
GROWTH_LINE_LIFECYCLES = (
    "current", "superseded", "deprecated", "conflict", "corrected", "historical"
)

DEFAULT_ANSWER_SNIPPET = 160


@dataclass
class HistoryRow:
    unit_id: str
    subject: str
    unit_type: str
    lifecycle: str
    supersedes_id: str | None
    confidence: float | None
    created_at: str
    answer_snippet: str
    version: int | None = None
    status: str | None = None
    question: str | None = None
    lifecycle_events: list[dict] = field(default_factory=list)
    is_current_value: bool = False


@dataclass
class HistoryReport:
    subject: str
    count: int = 0
    limit: int | None = None
    include_all_lifecycle: bool = False
    lifecycles: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    note: str = (
        "growth line view: multi-version by subject; "
        "retrieval default remains lifecycle=current only"
    )


def _snippet(text: str | None, max_len: int = DEFAULT_ANSWER_SNIPPET) -> str:
    if not text:
        return ""
    s = " ".join(str(text).split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def list_history_for_subject(
    db_path: Path | str,
    subject: str,
    *,
    limit: int | None = 50,
    include_all_lifecycle: bool = False,
    answer_snippet_len: int = DEFAULT_ANSWER_SNIPPET,
) -> HistoryReport:
    """List canonical units for *subject* ordered by created_at desc.

    Default: growth-line lifecycles (current, superseded, deprecated, conflict).
    ``include_all_lifecycle=True`` drops the lifecycle filter (still subject-scoped).
    """
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("subject is required")

    report = HistoryReport(
        subject=subject,
        limit=limit,
        include_all_lifecycle=include_all_lifecycle,
        lifecycles=(
            ["*"] if include_all_lifecycle else list(GROWTH_LINE_LIFECYCLES)
        ),
    )

    path = Path(db_path)
    if not path.exists():
        report.note = f"db missing: {path}"
        return report

    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        # Table may be absent on empty/test DBs
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='canonical_knowledge_units'"
        ).fetchone()
        if not exists:
            report.note = "canonical_knowledge_units table not found"
            return report

        clauses = ["subject = ?"]
        params: list[object] = [subject]
        if not include_all_lifecycle:
            placeholders = ",".join("?" * len(GROWTH_LINE_LIFECYCLES))
            clauses.append(f"lifecycle IN ({placeholders})")
            params.extend(GROWTH_LINE_LIFECYCLES)

        where = " AND ".join(clauses)
        sql = (
            "SELECT canonical_unit_id, subject, unit_type, question, answer, "
            "confidence, lifecycle, status, version, supersedes_id, created_at "
            "FROM canonical_knowledge_units "
            f"WHERE {where} "
            "ORDER BY created_at DESC, canonical_unit_id DESC"
        )
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"

        rows = con.execute(sql, params).fetchall()
        has_events = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_lifecycle_events'"
        ).fetchone() is not None
        out: list[HistoryRow] = []
        for r in rows:
            events: list[dict] = []
            if has_events:
                events = [
                    dict(event)
                    for event in con.execute(
                        "SELECT event_id,manifest_id,event_type,lifecycle_before,lifecycle_after,"
                        "version_before,version_after,supersedes_before,supersedes_after,reason,"
                        "reviewer_id_hash,actor_id,rollback_of_event_id,created_at "
                        "FROM knowledge_lifecycle_events WHERE unit_id=? "
                        "ORDER BY created_at,CASE WHEN rollback_of_event_id IS NULL THEN 0 ELSE 1 END,event_id",
                        (r["canonical_unit_id"],),
                    ).fetchall()
                ]
            out.append(
                HistoryRow(
                    unit_id=r["canonical_unit_id"],
                    subject=r["subject"],
                    unit_type=r["unit_type"],
                    lifecycle=r["lifecycle"],
                    supersedes_id=r["supersedes_id"],
                    confidence=r["confidence"],
                    created_at=r["created_at"] or "",
                    answer_snippet=_snippet(r["answer"], answer_snippet_len),
                    version=r["version"],
                    status=r["status"],
                    question=_snippet(r["question"], 120) or None,
                    lifecycle_events=events,
                )
            )
        superseded_ids = {row.supersedes_id for row in out if row.supersedes_id}
        current_candidates = [
            row for row in out
            if row.lifecycle == "current" and row.unit_id not in superseded_ids
        ]
        for row in current_candidates:
            row.is_current_value = True
        report.rows = [asdict(x) for x in out]
        report.count = len(out)
    finally:
        con.close()

    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Growth-line history for a subject (read-only; never DELETE). "
            "Default lifecycles: current+superseded+deprecated+conflict."
        ),
    )
    p.add_argument("--subject", required=True, help="Exact subject string")
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Max rows (default 50; use 0 for unlimited)",
    )
    p.add_argument(
        "--include-all-lifecycle",
        action="store_true",
        help="Do not filter by growth-line lifecycles (include any lifecycle value)",
    )
    p.add_argument("--db", type=Path, default=None)
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit full JSON report (default is compact table + JSON summary)",
    )
    return p


def format_table(rows: Iterable[dict]) -> str:
    lines = [
        f"{'unit_id':<36}  {'lifecycle':<18}  {'conf':>5}  "
        f"{'created_at':<20}  supersedes_id  answer",
        "-" * 100,
    ]
    for r in rows:
        sid = (r.get("supersedes_id") or "")[:20]
        conf = r.get("confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else ""
        lines.append(
            f"{(r.get('unit_id') or '')[:36]:<36}  "
            f"{(r.get('lifecycle') or '') + (' ← 当前值' if r.get('is_current_value') else ''):<18}  "
            f"{conf_s:>5}  "
            f"{(r.get('created_at') or '')[:20]:<20}  "
            f"{sid:<14}  "
            f"{(r.get('answer_snippet') or '')[:60]}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = args.limit
    if limit == 0:
        limit = None

    try:
        report = list_history_for_subject(
            args.db or UNIFIED_DB,
            args.subject,
            limit=limit,
            include_all_lifecycle=bool(args.include_all_lifecycle),
        )
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0

    print(f"subject={report.subject!r} count={report.count} limit={report.limit}")
    print(f"lifecycles={report.lifecycles}")
    if report.rows:
        print(format_table(report.rows))
    else:
        print("(no units for subject)")
    # Machine-readable summary after human table
    summary = {
        "subject": report.subject,
        "count": report.count,
        "limit": report.limit,
        "include_all_lifecycle": report.include_all_lifecycle,
        "lifecycles": report.lifecycles,
        "rows": report.rows,
        "note": report.note,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
