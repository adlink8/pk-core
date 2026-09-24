"""Conservative, human-invoked promotion of candidate/staging units."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from personal_knowledge.application.knowledge.build_knowledge_units_prod import _evidence_supported
from personal_knowledge.application.knowledge.eligibility import compute_eligible_messages
from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB, UNIFIED_DB


def rematch_unit_evidence(
    unified_con: sqlite3.Connection,
    agent_con: sqlite3.Connection,
    unit_row: sqlite3.Row | dict,
    eligible_refs: set[str],
) -> str | None:
    """Find an eligible canonical message containing the unit's quote probe."""

    quote = str(unit_row["evidence_quote"] if isinstance(unit_row, sqlite3.Row) else unit_row.get("evidence_quote") or "")
    probe = quote[:40]
    if len(probe) < 10:
        return None
    rows = agent_con.execute(
        "SELECT canonical_message_id, content FROM canonical_messages WHERE instr(content, ?) > 0",
        (probe,),
    ).fetchall()
    for row in rows:
        ref, content = row[0], row[1]
        if ref in eligible_refs and _evidence_supported(quote, content):
            return ref
    return None


def _unit_row(con: sqlite3.Connection, unit_id: str) -> tuple[str, sqlite3.Row | None]:
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM knowledge_units WHERE unit_id=?", (unit_id,)).fetchone()
    if row is not None:
        return "knowledge_units", row
    row = con.execute(
        "SELECT * FROM canonical_knowledge_units WHERE canonical_unit_id=?", (unit_id,)
    ).fetchone()
    return "canonical_knowledge_units", row


def promote_units(
    unified_db: Path = UNIFIED_DB,
    agent_db: Path = AGENT_CONVERSATIONS_DB,
    unit_ids: Iterable[str] = (),
    *,
    write: bool = False,
    eligible_refs: set[str] | None = None,
) -> dict:
    """Plan or apply per-unit promotion; failed evidence rematches never promote."""

    ids = [item for item in unit_ids if item]
    unit_uri = f"file:{Path(unified_db).resolve().as_posix()}?mode={'rw' if write else 'ro'}"
    agent_uri = f"file:{Path(agent_db).resolve().as_posix()}?mode=ro"
    unit_con = connect_rw(unit_uri, uri=True)
    agent_con = connect_rw(agent_uri, uri=True)
    unit_con.row_factory = sqlite3.Row
    try:
        if eligible_refs is None:
            eligible_refs = {item.evidence_ref for item in compute_eligible_messages(Path(agent_db))[0]}
        plans: list[dict] = []
        for unit_id in ids:
            table, row = _unit_row(unit_con, unit_id)
            if row is None:
                plans.append({"unit_id": unit_id, "action": "skip", "reason": "not_found"})
                continue
            new_ref = rematch_unit_evidence(unit_con, agent_con, row, eligible_refs)
            if new_ref is None:
                plans.append({"unit_id": unit_id, "table": table, "action": "skip", "reason": "rematch_failed"})
                continue
            old_ref = row["source_message_ref"] if "source_message_ref" in row.keys() else None
            action = "promote"
            if row["status"] == "current" and row["lifecycle"] == "current":
                action = "already_current"
            plans.append({
                "unit_id": unit_id,
                "table": table,
                "action": action,
                "old_source_message_ref": old_ref,
                "new_source_message_ref": new_ref,
                "lifecycle_before": row["lifecycle"],
                "status_before": row["status"],
            })

        report = {
            "write": write,
            "requested": len(ids),
            "promoted": sum(item["action"] == "promote" for item in plans),
            "already_current": sum(item["action"] == "already_current" for item in plans),
            "rematch_failed": sum(item.get("reason") == "rematch_failed" for item in plans),
            "not_found": sum(item.get("reason") == "not_found" for item in plans),
            "ref_remap": {
                item["unit_id"]: {"old": item["old_source_message_ref"], "new": item["new_source_message_ref"]}
                for item in plans
                if item.get("old_source_message_ref") != item.get("new_source_message_ref")
            },
            "plans": plans,
        }
        if not write:
            return report

        backup_dir = Path(unified_db).parents[1] / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"{Path(unified_db).stem}_{stamp}.sqlite"
        shutil.copy2(unified_db, backup)
        unit_con.execute("BEGIN IMMEDIATE")
        for item in plans:
            if item.get("action") != "promote":
                continue
            if item["table"] == "knowledge_units":
                unit_con.execute(
                    "UPDATE knowledge_units SET lifecycle='current', status='current', source_message_ref=? WHERE unit_id=?",
                    (item["new_source_message_ref"], item["unit_id"]),
                )
            else:
                unit_con.execute(
                    "UPDATE canonical_knowledge_units SET status='current', lifecycle='current' WHERE canonical_unit_id=?",
                    (item["unit_id"],),
                )
        unit_con.commit()
        report["backup"] = str(backup)
        return report
    finally:
        agent_con.close()
        unit_con.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-id", action="append", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    ids = [value for item in args.unit_id for value in item.split(",") if value]
    report = promote_units(unit_ids=ids, write=args.write)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
