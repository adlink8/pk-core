"""Phase 22-01: subject-level lifecycle reconcile (growth line, zero DELETE).

Detects superseded / conflict candidates on ``canonical_knowledge_units`` using
heuristic answer similarity (token Jaccard). Default is dry-run; optional write
only updates ``lifecycle`` / ``supersedes_id`` — never deletes rows.

Usage::

    python -m personal_knowledge.application.knowledge.reconcile_knowledge_lifecycle \\
        --dry-run --max-subjects 20
    python -m personal_knowledge.application.knowledge.reconcile_knowledge_lifecycle \\
        --write --i-know --subject "Shell"
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw
from typing import Iterable

from personal_knowledge.core.project_paths import UNIFIED_DB

# Actions (v1)
ACTION_KEEP_CURRENT = "keep_current"
ACTION_MARK_SUPERSEDED = "mark_superseded"
ACTION_MARK_CONFLICT = "mark_conflict"
ACTION_NOOP = "noop"

SIMILAR_THRESHOLD = 0.85
CONFLICT_THRESHOLD = 0.4

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_answer(text: str) -> str:
    """Normalize answer text for token comparison (lowercase, collapse space)."""
    if not text:
        return ""
    return " ".join(text.lower().split())


def tokenize(text: str) -> set[str]:
    """Token set for Jaccard: whitespace/word + CJK char runs."""
    normalized = normalize_answer(text)
    if not normalized:
        return set()
    tokens = _TOKEN_RE.findall(normalized)
    return set(tokens) if tokens else set(normalized.split())


def answer_jaccard(a: str, b: str) -> float:
    """Token Jaccard on normalized answers. Empty → 0.0."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


@dataclass
class ReconcileAction:
    action: str
    canonical_unit_id: str
    subject: str
    unit_type: str
    lifecycle_before: str
    lifecycle_after: str
    supersedes_id: str | None = None
    peer_id: str | None = None
    similarity: float | None = None
    reason: str = ""


@dataclass
class ReconcileReport:
    write: bool = False
    dry_run: bool = True
    subjects_scanned: int = 0
    groups_scanned: int = 0
    units_scanned: int = 0
    row_count_before: int = 0
    row_count_after: int = 0
    counts: dict[str, int] = field(
        default_factory=lambda: {
            ACTION_KEEP_CURRENT: 0,
            ACTION_MARK_SUPERSEDED: 0,
            ACTION_MARK_CONFLICT: 0,
            ACTION_NOOP: 0,
        }
    )
    actions: list[dict] = field(default_factory=list)
    sample_actions: list[dict] = field(default_factory=list)
    artifact: str = ""
    filters: dict = field(default_factory=dict)
    note: str = ""


def _unit_sort_key(u: dict) -> tuple:
    """Newest first: created_at desc, then canonical_unit_id desc (stable)."""
    return (u.get("created_at") or "", u.get("canonical_unit_id") or "")


def _select_subjects(
    con: sqlite3.Connection,
    *,
    subject: str | None,
    since: str | None,
    max_subjects: int | None,
) -> list[str]:
    """Distinct subjects in scope (current lifecycle preferred)."""
    clauses: list[str] = ["lifecycle = 'current'"]
    params: list[object] = []
    if subject:
        clauses.append("subject = ?")
        params.append(subject)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    where = " AND ".join(clauses)
    sql = (
        f"SELECT DISTINCT subject FROM canonical_knowledge_units WHERE {where} "
        "ORDER BY subject"
    )
    subjects = [r[0] for r in con.execute(sql, params).fetchall()]
    if max_subjects is not None and max_subjects >= 0:
        subjects = subjects[: max_subjects]
    return subjects


def load_units_for_subjects(
    con: sqlite3.Connection,
    subjects: Iterable[str],
) -> list[dict]:
    """Load lifecycle=current units for given subjects (status not required)."""
    subjects = list(subjects)
    if not subjects:
        return []
    placeholders = ",".join("?" * len(subjects))
    rows = con.execute(
        "SELECT canonical_unit_id, subject, unit_type, question, answer, "
        "confidence, lifecycle, status, version, run_id, supersedes_id, created_at, "
        "rowid AS _rowid "
        "FROM canonical_knowledge_units "
        f"WHERE subject IN ({placeholders}) AND lifecycle = 'current' "
        "ORDER BY subject, unit_type, created_at, canonical_unit_id",
        subjects,
    ).fetchall()
    return [dict(r) for r in rows]


def propose_actions_for_group(units: list[dict]) -> list[ReconcileAction]:
    """Heuristic v1 for one (subject, unit_type) group of lifecycle=current units.

    - Clusters with pairwise Jaccard >= 0.85: newest keep_current; older superseded.
    - Unclustered pairs with Jaccard < 0.4 among remaining multi-current:
      mark_conflict on both (if still current and not already acted as superseded).
    - Singletons / mid-similarity only: noop or keep_current.
    """
    if not units:
        return []
    if len(units) == 1:
        u = units[0]
        return [
            ReconcileAction(
                action=ACTION_NOOP,
                canonical_unit_id=u["canonical_unit_id"],
                subject=u["subject"],
                unit_type=u["unit_type"],
                lifecycle_before=u["lifecycle"],
                lifecycle_after=u["lifecycle"],
                reason="singleton_group",
            )
        ]

    n = len(units)
    # Build similarity matrix
    sim: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = answer_jaccard(units[i].get("answer") or "", units[j].get("answer") or "")
            sim[i][j] = sim[j][i] = s

    # Union-find similar clusters (edge if sim >= 0.85)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i][j] >= SIMILAR_THRESHOLD:
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    actions: list[ReconcileAction] = []
    acted: set[int] = set()

    # Supersede within multi-member high-sim clusters
    for idxs in clusters.values():
        if len(idxs) < 2:
            continue
        ordered = sorted(idxs, key=lambda i: _unit_sort_key(units[i]), reverse=True)
        newest_i = ordered[0]
        newest = units[newest_i]
        # Chain clusters via UF; report max pairwise sim to newest for artifact
        max_peer = max(
            (sim[newest_i][j] for j in ordered[1:]),
            default=1.0,
        )
        actions.append(
            ReconcileAction(
                action=ACTION_KEEP_CURRENT,
                canonical_unit_id=newest["canonical_unit_id"],
                subject=newest["subject"],
                unit_type=newest["unit_type"],
                lifecycle_before=newest["lifecycle"],
                lifecycle_after="current",
                similarity=max_peer,
                reason="newest_in_similar_cluster",
            )
        )
        acted.add(newest_i)
        for older_i in ordered[1:]:
            older = units[older_i]
            actions.append(
                ReconcileAction(
                    action=ACTION_MARK_SUPERSEDED,
                    canonical_unit_id=older["canonical_unit_id"],
                    subject=older["subject"],
                    unit_type=older["unit_type"],
                    lifecycle_before=older["lifecycle"],
                    lifecycle_after="superseded",
                    supersedes_id=newest["canonical_unit_id"],
                    peer_id=newest["canonical_unit_id"],
                    similarity=sim[newest_i][older_i],
                    reason="similar_answer_older",
                )
            )
            acted.add(older_i)

    # Remaining current units: conflict if low similarity vs another remaining
    remaining = [i for i in range(n) if i not in acted]
    conflicted: set[int] = set()
    for a in range(len(remaining)):
        for b in range(a + 1, len(remaining)):
            i, j = remaining[a], remaining[b]
            if sim[i][j] < CONFLICT_THRESHOLD:
                conflicted.add(i)
                conflicted.add(j)

    for i in sorted(conflicted):
        u = units[i]
        # pick a low-sim peer for reporting
        peer_id = None
        peer_sim = None
        for j in remaining:
            if j == i:
                continue
            if sim[i][j] < CONFLICT_THRESHOLD:
                peer_id = units[j]["canonical_unit_id"]
                peer_sim = sim[i][j]
                break
        actions.append(
            ReconcileAction(
                action=ACTION_MARK_CONFLICT,
                canonical_unit_id=u["canonical_unit_id"],
                subject=u["subject"],
                unit_type=u["unit_type"],
                lifecycle_before=u["lifecycle"],
                lifecycle_after="conflict",
                peer_id=peer_id,
                similarity=peer_sim,
                reason="low_similarity_same_subject_type",
            )
        )
        acted.add(i)

    for i in remaining:
        if i in acted:
            continue
        u = units[i]
        actions.append(
            ReconcileAction(
                action=ACTION_NOOP,
                canonical_unit_id=u["canonical_unit_id"],
                subject=u["subject"],
                unit_type=u["unit_type"],
                lifecycle_before=u["lifecycle"],
                lifecycle_after=u["lifecycle"],
                reason="mid_similarity_or_unresolved",
            )
        )

    return actions


def apply_actions(
    con: sqlite3.Connection,
    actions: list[ReconcileAction],
    *,
    write: bool,
) -> int:
    """Apply only lifecycle / supersedes_id updates. Never DELETE.

    Returns approximate number of mutating statements executed.
    """
    if not write:
        return 0
    n = 0
    for act in actions:
        if act.action == ACTION_MARK_SUPERSEDED:
            con.execute(
                "UPDATE canonical_knowledge_units "
                "SET lifecycle = 'superseded', supersedes_id = ? "
                "WHERE canonical_unit_id = ?",
                (act.supersedes_id, act.canonical_unit_id),
            )
            n += 1
        elif act.action == ACTION_MARK_CONFLICT:
            con.execute(
                "UPDATE canonical_knowledge_units "
                "SET lifecycle = 'conflict' "
                "WHERE canonical_unit_id = ?",
                (act.canonical_unit_id,),
            )
            n += 1
        elif act.action == ACTION_KEEP_CURRENT and act.lifecycle_before != "current":
            con.execute(
                "UPDATE canonical_knowledge_units "
                "SET lifecycle = 'current' "
                "WHERE canonical_unit_id = ?",
                (act.canonical_unit_id,),
            )
            n += 1
    con.commit()
    return n


def reconcile_knowledge_lifecycle(
    db_path: Path = UNIFIED_DB,
    *,
    subject: str | None = None,
    since: str | None = None,
    max_subjects: int | None = None,
    write: bool = False,
    dry_run: bool = True,
    artifact: Path | None = None,
    sample_limit: int = 20,
) -> ReconcileReport:
    """Run lifecycle reconcile over canonical_knowledge_units.

    ``write=True`` requires caller to have passed safety flags at CLI layer.
    When both write and dry_run are set, write wins only if write is True and
    dry_run is False; default dry_run means no DB mutation.
    """
    # Library contract: write=True means mutate. CLI maps --write → write=True, dry_run=False.
    do_write = bool(write)

    report = ReconcileReport(
        write=do_write,
        dry_run=not do_write,
        filters={
            "subject": subject or "",
            "since": since or "",
            "max_subjects": max_subjects,
        },
    )

    if not Path(db_path).exists():
        report.note = f"db missing: {db_path}"
        return report

    con = connect_rw(db_path)
    con.row_factory = sqlite3.Row
    try:
        report.row_count_before = int(
            con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
        )
        subjects = _select_subjects(
            con, subject=subject, since=since, max_subjects=max_subjects
        )
        report.subjects_scanned = len(subjects)
        units = load_units_for_subjects(con, subjects)
        report.units_scanned = len(units)

        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for u in units:
            key = (u["subject"], u["unit_type"])
            groups[key].append(u)
        report.groups_scanned = len(groups)

        all_actions: list[ReconcileAction] = []
        for _key, group_units in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1])):
            all_actions.extend(propose_actions_for_group(group_units))

        for act in all_actions:
            report.counts[act.action] = report.counts.get(act.action, 0) + 1
            report.actions.append(asdict(act))

        # Sample non-noop first, then fill
        interesting = [a for a in report.actions if a["action"] != ACTION_NOOP]
        sample = interesting[:sample_limit]
        if len(sample) < sample_limit:
            for a in report.actions:
                if a in sample:
                    continue
                sample.append(a)
                if len(sample) >= sample_limit:
                    break
        report.sample_actions = sample

        if do_write:
            apply_actions(con, all_actions, write=True)
        else:
            report.note = "dry-run only; pass --write --i-know to persist lifecycle updates"

        report.row_count_after = int(
            con.execute("SELECT COUNT(*) FROM canonical_knowledge_units").fetchone()[0]
        )
    finally:
        con.close()

    if artifact is not None:
        artifact = Path(artifact)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(report)
        # full actions in artifact; keep sample in summary
        artifact.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report.artifact = str(artifact)

    return report


def default_artifact_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return Path(f"var/reports/analysis/ai_context/ku_lifecycle_reconcile_{ts}.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcile canonical KU lifecycle (never DELETE; default dry-run)",
    )
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--subject", default="", help="Limit to exact subject")
    p.add_argument("--since", default="", metavar="YYYY-MM-DD", help="Subjects with units since date")
    p.add_argument("--max-subjects", type=int, default=None, metavar="N")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Explicit dry-run (default when --write not set)",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="Persist lifecycle/supersedes_id updates (requires --i-know)",
    )
    p.add_argument(
        "--i-know",
        action="store_true",
        help="Confirmation for --write (fail-closed without it)",
    )
    p.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="Write full JSON report path (optional)",
    )
    p.add_argument("--sample-limit", type=int, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.write and not args.i_know:
        print(
            "[error] --write requires --i-know (lifecycle updates are deliberate)",
            file=sys.stderr,
        )
        return 2

    do_write = bool(args.write and args.i_know)
    dry_run = not do_write

    report = reconcile_knowledge_lifecycle(
        db_path=args.db or UNIFIED_DB,
        subject=args.subject or None,
        since=args.since or None,
        max_subjects=args.max_subjects,
        write=do_write,
        dry_run=dry_run,
        artifact=args.artifact,
        sample_limit=args.sample_limit,
    )

    # Compact stdout summary
    summary = {
        "write": report.write,
        "dry_run": report.dry_run,
        "subjects_scanned": report.subjects_scanned,
        "groups_scanned": report.groups_scanned,
        "units_scanned": report.units_scanned,
        "row_count_before": report.row_count_before,
        "row_count_after": report.row_count_after,
        "counts": report.counts,
        "sample_actions": report.sample_actions,
        "artifact": report.artifact,
        "filters": report.filters,
        "note": report.note,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
