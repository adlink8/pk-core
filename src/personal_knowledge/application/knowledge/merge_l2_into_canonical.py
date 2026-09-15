"""Merge L2 session-window staging units into canonical_knowledge_units (current).

Strategy:
  - For each L2 unit (unit_id like l2|…, status staging/current):
    - If same unit_type + similar subject/answer to an existing *current* canonical
      (Jaccard on answer ≥ 0.85 and subject token overlap ≥ 0.5) → attach as member only.
    - Else create a new canonical row with status=current and member link.
  - Promote matched L2 knowledge_units rows to status=current.

Does not rebuild Chroma; run build_knowledge_unit_vector_store after.

Usage::

    python -m personal_knowledge.application.knowledge.merge_l2_into_canonical --dry-run
    python -m personal_knowledge.application.knowledge.merge_l2_into_canonical --write
    python -m personal_knowledge.application.knowledge.merge_l2_into_canonical --write --run-id 205bff…
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw

from personal_knowledge.core.project_paths import UNIFIED_DB, AI_CONTEXT_DIR
from personal_knowledge.application.knowledge.build_canonical_knowledge_units import (
    _canonical_id,
    compute_similarity,
)

REPORT_PATH = AI_CONTEXT_DIR / "knowledge_l2_canonical_merge_report.json"
ANSWER_SIM = 0.85
SUBJECT_SIM = 0.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_subject(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _subject_sim(a: str, b: str) -> float:
    return compute_similarity(_norm_subject(a), _norm_subject(b))


@dataclass
class MergeStats:
    l2_loaded: int = 0
    already_linked: int = 0
    attached_to_existing: int = 0
    new_canonical: int = 0
    skipped_empty: int = 0
    units_marked_current: int = 0
    by_type_new: dict = field(default_factory=dict)
    dry_run: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def load_l2_units(db_path: Path, run_id: str | None = None) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    if run_id:
        rows = con.execute(
            "SELECT unit_id, run_id, unit_type, subject, question, answer, confidence, "
            "evidence_quote, lifecycle, source_message_ref, source_session_id, source_agent "
            "FROM knowledge_units WHERE unit_id LIKE 'l2|%' AND run_id=? "
            "AND status IN ('staging','current','validated')",
            (run_id,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT unit_id, run_id, unit_type, subject, question, answer, confidence, "
            "evidence_quote, lifecycle, source_message_ref, source_session_id, source_agent "
            "FROM knowledge_units WHERE unit_id LIKE 'l2|%' "
            "AND status IN ('staging','current','validated')"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def load_current_canonical(db_path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT canonical_unit_id, subject, unit_type, question, answer, confidence "
        "FROM canonical_knowledge_units WHERE status='current'"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def find_match(unit: dict, canon_by_type: dict[str, list[dict]]) -> dict | None:
    """Return existing canonical to attach to, or None if novel."""
    cands = canon_by_type.get(unit["unit_type"] or "", [])
    best = None
    best_score = 0.0
    usub = unit.get("subject") or ""
    uans = unit.get("answer") or ""
    for c in cands:
        ss = _subject_sim(usub, c["subject"] or "")
        if ss < SUBJECT_SIM:
            continue
        ans = compute_similarity(uans, c.get("answer") or "")
        # also allow high subject match + moderate answer
        score = 0.4 * ss + 0.6 * ans
        if ans >= ANSWER_SIM and ss >= SUBJECT_SIM and score > best_score:
            best_score = score
            best = c
        elif ss >= 0.9 and ans >= 0.6 and score > best_score:
            # near-identical subject, related answer → attach
            best_score = score
            best = c
    return best


def merge_l2(
    db_path: Path = UNIFIED_DB,
    *,
    write: bool = False,
    run_id: str | None = None,
) -> tuple[MergeStats, list[dict]]:
    stats = MergeStats(dry_run=not write)
    l2 = load_l2_units(db_path, run_id=run_id)
    stats.l2_loaded = len(l2)
    if not l2:
        return stats, []

    current = load_current_canonical(db_path)
    by_type: dict[str, list[dict]] = {}
    for c in current:
        by_type.setdefault(c["unit_type"] or "", []).append(c)

    con = connect_rw(db_path)
    # already linked members
    linked = {
        r[0]
        for r in con.execute("SELECT member_unit_id FROM canonical_unit_members")
    }

    actions: list[dict] = []
    now = _utc_now()

    for u in l2:
        if not (u.get("answer") or "").strip() or not (u.get("subject") or "").strip():
            stats.skipped_empty += 1
            continue
        if u["unit_id"] in linked:
            stats.already_linked += 1
            actions.append({"unit_id": u["unit_id"], "action": "already_linked"})
            continue

        match = find_match(u, by_type)
        if match:
            stats.attached_to_existing += 1
            actions.append(
                {
                    "unit_id": u["unit_id"],
                    "action": "attach",
                    "canonical_unit_id": match["canonical_unit_id"],
                    "subject": u["subject"],
                }
            )
            if write:
                con.execute(
                    "INSERT OR IGNORE INTO canonical_unit_members "
                    "(canonical_unit_id, member_unit_id) VALUES (?,?)",
                    (match["canonical_unit_id"], u["unit_id"]),
                )
                con.execute(
                    "UPDATE knowledge_units SET status='current' WHERE unit_id=?",
                    (u["unit_id"],),
                )
                stats.units_marked_current += 1
            continue

        # new canonical
        cid = _canonical_id(u["subject"], u["unit_type"], u["answer"])
        stats.new_canonical += 1
        stats.by_type_new[u["unit_type"]] = stats.by_type_new.get(u["unit_type"], 0) + 1
        actions.append(
            {
                "unit_id": u["unit_id"],
                "action": "new_canonical",
                "canonical_unit_id": cid,
                "subject": u["subject"],
                "unit_type": u["unit_type"],
            }
        )
        if write:
            # if id collides with existing row, still attach member
            existing = con.execute(
                "SELECT canonical_unit_id, status FROM canonical_knowledge_units "
                "WHERE canonical_unit_id=?",
                (cid,),
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE canonical_knowledge_units SET status='current' "
                    "WHERE canonical_unit_id=?",
                    (cid,),
                )
            else:
                conf = float(u.get("confidence") or 0.7)
                con.execute(
                    "INSERT INTO canonical_knowledge_units VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        u["subject"][:200],
                        u["unit_type"],
                        (u.get("question") or "")[:500],
                        (u.get("answer") or "")[:2000],
                        conf,
                        "current",
                        "current",
                        1,
                        u.get("run_id") or "l2_merge",
                        "l2_session_window_import",
                        None,
                        now,
                    ),
                )
                # keep by_type index updated for subsequent matches in same run
                by_type.setdefault(u["unit_type"] or "", []).append(
                    {
                        "canonical_unit_id": cid,
                        "subject": u["subject"],
                        "unit_type": u["unit_type"],
                        "answer": u["answer"],
                        "confidence": conf,
                    }
                )
            con.execute(
                "INSERT OR IGNORE INTO canonical_unit_members "
                "(canonical_unit_id, member_unit_id) VALUES (?,?)",
                (cid, u["unit_id"]),
            )
            con.execute(
                "UPDATE knowledge_units SET status='current' WHERE unit_id=?",
                (u["unit_id"],),
            )
            stats.units_marked_current += 1

    if write:
        con.commit()
    con.close()
    return stats, actions


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Merge L2 staging units into canonical current")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--run-id", default=None, help="Only this L2 extraction run")
    p.add_argument("--db", type=Path, default=UNIFIED_DB)
    p.add_argument("--report", type=Path, default=REPORT_PATH)
    args = p.parse_args(argv)
    write = bool(args.write)
    if not write:
        args.dry_run = True

    stats, actions = merge_l2(args.db, write=write, run_id=args.run_id)
    # recount
    con = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    canon_n = con.execute(
        "SELECT COUNT(*) FROM canonical_knowledge_units WHERE status='current'"
    ).fetchone()[0]
    l2_cur = con.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE unit_id LIKE 'l2|%' AND status='current'"
    ).fetchone()[0]
    con.close()

    doc = {
        "generated_at": _utc_now(),
        "stats": stats.to_dict(),
        "canonical_current_after": canon_n,
        "l2_units_status_current": l2_cur,
        "sample_actions": actions[:30],
        "action_counts": {
            "attach": sum(1 for a in actions if a["action"] == "attach"),
            "new_canonical": sum(1 for a in actions if a["action"] == "new_canonical"),
            "already_linked": sum(1 for a in actions if a["action"] == "already_linked"),
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
