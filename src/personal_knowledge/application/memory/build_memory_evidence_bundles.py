"""Phase 09 Wave 3: build memory evidence bundles.

Structured evidence is assembled into auditable bundles before any LLM memory
candidate extraction step. This script never writes promotion candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
PREVIEW_JSON = AI_DIR / "memory_evidence_bundles_preview.json"
PREVIEW_MD = AI_DIR / "memory_evidence_bundles_preview.md"

PHASE = "09"
WAVE = "3"
TABLE_NAME = "memory_evidence_bundles"

BUNDLE_FIELDS = [
    "bundle_id",
    "bundle_type",
    "source_system",
    "bundle_label",
    "bundle_summary",
    "primary_ref",
    "evidence_refs_json",
    "source_refs_json",
    "duplicate_check_targets_json",
    "conflict_check_targets_json",
    "created_at",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def stable_id(*parts: object) -> str:
    text = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"meb:{digest}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    if parsed is None:
        return []
    return [parsed]


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def canonical_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def excerpt(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def required_source_tables(con: sqlite3.Connection) -> dict[str, bool]:
    names = [
        "unified_events_rich",
        "conversation_turns_summary",
        "graph_relation_candidates",
        "graph_relation_judgments",
        "memory_items",
    ]
    return {name: table_exists(con, name) for name in names}


def find_duplicate_targets(con: sqlite3.Connection, probe: str) -> tuple[list[str], list[str]]:
    if not probe or not table_exists(con, "memory_items"):
        return [], []

    normalized_probe = canonical_text(probe)
    duplicate_ids: list[str] = []
    conflict_ids: list[str] = []
    rows = con.execute("SELECT memory_id, subject, description FROM memory_items").fetchall()
    for row in rows:
        row_subject = canonical_text(row["subject"])
        row_claim = canonical_text(row["description"])
        if normalized_probe and normalized_probe in {row_subject, row_claim}:
            duplicate_ids.append(row["memory_id"])
        elif normalized_probe and row_subject and normalized_probe in row_subject:
            conflict_ids.append(row["memory_id"])
    return sorted(set(duplicate_ids)), sorted(set(conflict_ids))


def load_turn_summary(
    con: sqlite3.Connection,
    session_id: str | None,
    turn_token: str | None,
) -> dict[str, Any] | None:
    if not session_id or turn_token is None or not table_exists(con, "conversation_turns_summary"):
        return None
    row = con.execute(
        """
        SELECT session_id, turn_no, turn_id, narrative, source_ref, main_topic
        FROM conversation_turns_summary
        WHERE session_id = ?
          AND (COALESCE(turn_id, '') = ? OR CAST(turn_no AS TEXT) = ?)
        ORDER BY id
        LIMIT 1
        """,
        (session_id, str(turn_token), str(turn_token)),
    ).fetchone()
    return dict(row) if row else None


def turn_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "table": "conversation_turns_summary",
        "session_id": row["session_id"],
        "turn_no": row["turn_no"],
        "turn_id": row.get("turn_id"),
        "main_topic": row.get("main_topic"),
        "excerpt": excerpt(row.get("narrative")),
    }


def build_graph_edge_bundles(con: sqlite3.Connection, limit: int, created_at: str) -> list[dict[str, Any]]:
    if not all(table_exists(con, name) for name in ("graph_relation_candidates", "graph_relation_judgments")):
        return []
    rows = con.execute(
        """
        SELECT
            c.candidate_id,
            c.source_session_id,
            c.source_turn_id,
            c.target_session_id,
            c.target_turn_id,
            c.source_refs_json,
            j.relation_type,
            j.confidence,
            j.evidence_refs_json,
            j.reason,
            j.prompt_version,
            j.model
        FROM graph_relation_candidates c
        JOIN graph_relation_judgments j ON j.candidate_id = c.candidate_id
        WHERE j.gate_status = 'accepted'
        ORDER BY j.confidence DESC, c.candidate_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    bundles: list[dict[str, Any]] = []
    for row in rows:
        source_turn = load_turn_summary(con, row["source_session_id"], row["source_turn_id"])
        target_turn = load_turn_summary(con, row["target_session_id"], row["target_turn_id"])
        evidence_refs = json_loads_list(row["evidence_refs_json"])
        if source_turn:
            evidence_refs.append(turn_ref(source_turn))
        if target_turn:
            evidence_refs.append(turn_ref(target_turn))
        source_refs = json_loads_list(row["source_refs_json"])
        source_refs.extend(
            [
                {"table": "graph_relation_candidates", "candidate_id": row["candidate_id"]},
                {
                    "table": "graph_relation_judgments",
                    "candidate_id": row["candidate_id"],
                    "relation_type": row["relation_type"],
                    "prompt_version": row["prompt_version"],
                    "model": row["model"],
                },
            ]
        )
        if source_turn and source_turn.get("source_ref"):
            source_refs.append(source_turn["source_ref"])
        if target_turn and target_turn.get("source_ref"):
            source_refs.append(target_turn["source_ref"])
        duplicate_ids, conflict_ids = find_duplicate_targets(con, row["reason"])
        bundle = {
            "bundle_id": stable_id("accepted_graph_edge", row["candidate_id"]),
            "bundle_type": "accepted_graph_edge",
            "source_system": "accepted_graph_edge",
            "bundle_label": row["candidate_id"],
            "bundle_summary": row["reason"],
            "primary_ref": row["candidate_id"],
            "evidence_refs_json": json_dumps(evidence_refs),
            "source_refs_json": json_dumps(source_refs),
            "duplicate_check_targets_json": json_dumps(duplicate_ids),
            "conflict_check_targets_json": json_dumps(conflict_ids),
            "created_at": created_at,
        }
        if evidence_refs and source_refs:
            bundles.append(bundle)
    return bundles


def build_turn_bundles(con: sqlite3.Connection, limit: int, created_at: str) -> list[dict[str, Any]]:
    if not table_exists(con, "conversation_turns_summary"):
        return []
    rows = con.execute(
        """
        SELECT session_id, turn_no, turn_id, narrative, source_ref, main_topic
        FROM conversation_turns_summary
        WHERE COALESCE(narrative, '') <> ''
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    bundles: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        evidence_refs = [turn_ref(row_dict)]
        source_refs: list[Any] = []
        if row_dict.get("source_ref"):
            source_refs.append(row_dict["source_ref"])
        source_refs.append(
            {
                "table": "conversation_turns_summary",
                "session_id": row_dict["session_id"],
                "turn_no": row_dict["turn_no"],
                "turn_id": row_dict.get("turn_id"),
            }
        )
        probe = row_dict.get("main_topic") or row_dict.get("narrative")
        duplicate_ids, conflict_ids = find_duplicate_targets(con, str(probe or ""))
        bundles.append(
            {
                "bundle_id": stable_id("conversation_turn", row_dict["session_id"], row_dict.get("turn_id"), row_dict["turn_no"]),
                "bundle_type": "conversation_turn",
                "source_system": "conversation_turn",
                "bundle_label": f"{row_dict['session_id']}:{row_dict.get('turn_id') or row_dict['turn_no']}",
                "bundle_summary": excerpt(row_dict.get("narrative")),
                "primary_ref": row_dict.get("source_ref") or f"{row_dict['session_id']}:{row_dict['turn_no']}",
                "evidence_refs_json": json_dumps(evidence_refs),
                "source_refs_json": json_dumps(source_refs),
                "duplicate_check_targets_json": json_dumps(duplicate_ids),
                "conflict_check_targets_json": json_dumps(conflict_ids),
                "created_at": created_at,
            }
        )
    return bundles


def build_event_bundles(con: sqlite3.Connection, limit: int, created_at: str) -> list[dict[str, Any]]:
    if not table_exists(con, "unified_events_rich"):
        return []
    rows = con.execute(
        """
        SELECT event_id, content_rich, content_rich_source
        FROM unified_events_rich
        WHERE COALESCE(content_rich, '') <> ''
        ORDER BY event_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    bundles: list[dict[str, Any]] = []
    for row in rows:
        event_ref = {
            "table": "unified_events_rich",
            "event_id": row["event_id"],
            "content_rich_source": row["content_rich_source"],
            "excerpt": excerpt(row["content_rich"]),
        }
        source_refs: list[Any] = [event_ref]
        if row["content_rich_source"]:
            source_refs.append(row["content_rich_source"])
        duplicate_ids, conflict_ids = find_duplicate_targets(con, event_ref["excerpt"])
        bundles.append(
            {
                "bundle_id": stable_id("unified_event", row["event_id"]),
                "bundle_type": "unified_event",
                "source_system": "unified_event",
                "bundle_label": row["event_id"],
                "bundle_summary": event_ref["excerpt"],
                "primary_ref": row["event_id"],
                "evidence_refs_json": json_dumps([event_ref]),
                "source_refs_json": json_dumps(source_refs),
                "duplicate_check_targets_json": json_dumps(duplicate_ids),
                "conflict_check_targets_json": json_dumps(conflict_ids),
                "created_at": created_at,
            }
        )
    return bundles


def dedupe_bundles(bundles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for bundle in bundles:
        deduped[bundle["bundle_id"]] = bundle
    return [deduped[key] for key in sorted(deduped)]


def interleave_bundle_groups(groups: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    queues = [deque(group) for group in groups if group]
    chosen: list[dict[str, Any]] = []
    while queues and len(chosen) < limit:
        next_round: list[deque[dict[str, Any]]] = []
        for queue in queues:
            if len(chosen) >= limit:
                break
            if queue:
                chosen.append(queue.popleft())
            if queue:
                next_round.append(queue)
        queues = next_round
    return chosen


def validate_bundle(bundle: dict[str, Any]) -> None:
    missing = [field for field in BUNDLE_FIELDS if field not in bundle]
    if missing:
        raise ValueError(f"bundle missing fields: {missing}")
    evidence_refs = json_loads_list(bundle["evidence_refs_json"])
    source_refs = json_loads_list(bundle["source_refs_json"])
    if not evidence_refs:
        raise ValueError(f"{bundle['bundle_id']} has no evidence_refs_json")
    if not source_refs:
        raise ValueError(f"{bundle['bundle_id']} has no source_refs_json")
    if any(isinstance(ref, dict) and ref.get("table") == "memory_items" for ref in evidence_refs + source_refs):
        raise ValueError(f"{bundle['bundle_id']} cannot use memory_items as evidence")


def build_bundles(con: sqlite3.Connection, *, limit: int, created_at: str | None = None) -> list[dict[str, Any]]:
    created = created_at or utc_now()
    graph_bundles = dedupe_bundles(build_graph_edge_bundles(con, limit, created))
    turn_bundles = dedupe_bundles(build_turn_bundles(con, limit, created))
    event_bundles = dedupe_bundles(build_event_bundles(con, limit, created))
    bundles = interleave_bundle_groups([graph_bundles, turn_bundles, event_bundles], limit)
    for bundle in bundles:
        validate_bundle(bundle)
    return bundles


def bundle_stats(bundles: list[dict[str, Any]]) -> dict[str, Any]:
    type_distribution = Counter(bundle["bundle_type"] for bundle in bundles)
    source_distribution = Counter(bundle["source_system"] for bundle in bundles)
    return {
        "total": len(bundles),
        "bundle_type_distribution": dict(sorted(type_distribution.items())),
        "source_system_distribution": dict(sorted(source_distribution.items())),
        "all_have_evidence_refs": all(bool(json_loads_list(bundle["evidence_refs_json"])) for bundle in bundles),
        "all_have_source_refs": all(bool(json_loads_list(bundle["source_refs_json"])) for bundle in bundles),
    }


def create_bundles_table(con: sqlite3.Connection) -> None:
    con.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    con.execute(
        f"""
        CREATE TABLE {TABLE_NAME} (
            bundle_id TEXT PRIMARY KEY,
            bundle_type TEXT NOT NULL,
            source_system TEXT NOT NULL,
            bundle_label TEXT NOT NULL,
            bundle_summary TEXT NOT NULL,
            primary_ref TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            source_refs_json TEXT NOT NULL,
            duplicate_check_targets_json TEXT NOT NULL,
            conflict_check_targets_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(f"CREATE INDEX idx_meb_bundle_type ON {TABLE_NAME}(bundle_type)")
    con.execute(f"CREATE INDEX idx_meb_source_system ON {TABLE_NAME}(source_system)")


def write_bundles_table(con: sqlite3.Connection, bundles: list[dict[str, Any]]) -> None:
    create_bundles_table(con)
    rows = [tuple(bundle[field] for field in BUNDLE_FIELDS) for bundle in bundles]
    placeholders = ",".join("?" for _ in BUNDLE_FIELDS)
    con.executemany(
        f"INSERT INTO {TABLE_NAME} ({','.join(BUNDLE_FIELDS)}) VALUES ({placeholders})",
        rows,
    )
    con.commit()


def build_preview(
    *,
    mode: str,
    db_path: Path,
    source_tables: dict[str, bool],
    bundles: list[dict[str, Any]],
    table_written: bool,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "phase": PHASE,
        "wave": WAVE,
        "scope": "structured_evidence_bundle_boundary",
        "mode": mode,
        "db_path": rel(db_path),
        "table_name": TABLE_NAME,
        "table_written": table_written,
        "source_tables": source_tables,
        "bundle_strategy": [
            "accepted graph edges are bundled as auditable evidence packages, not direct promotion candidates.",
            "conversation turns and unified events are stored as structured evidence bundles for later LLM extraction.",
            "memory_items are only used as duplicate/conflict targets and never appear inside evidence_refs/source_refs.",
        ],
        "stats": bundle_stats(bundles),
        "bundles": bundles,
    }


def render_preview_md(preview: dict[str, Any]) -> str:
    stats = preview["stats"]
    lines = [
        "# Memory Evidence Bundles Preview",
        "",
        f"- generated_at: {preview['generated_at']}",
        f"- phase: {preview['phase']}",
        f"- wave: {preview['wave']}",
        f"- mode: {preview['mode']}",
        f"- table_written: {preview['table_written']}",
        "",
        "## Counts",
        "",
        f"- total bundles: {stats['total']}",
        f"- all_have_evidence_refs: {stats['all_have_evidence_refs']}",
        f"- all_have_source_refs: {stats['all_have_source_refs']}",
        "",
        "### Bundle Type Distribution",
        "",
    ]
    for bundle_type, count in stats["bundle_type_distribution"].items():
        lines.append(f"- `{bundle_type}`: {count}")
    lines += ["", "### Source System Distribution", ""]
    for source_system, count in stats["source_system_distribution"].items():
        lines.append(f"- `{source_system}`: {count}")
    lines += ["", "## Bundle Strategy", ""]
    lines.extend(f"- {item}" for item in preview["bundle_strategy"])
    lines += ["", "## Sample Bundles", ""]
    for bundle in preview["bundles"][:12]:
        lines.append(f"### {bundle['bundle_id']}")
        lines.append(f"- bundle_type: `{bundle['bundle_type']}`")
        lines.append(f"- source_system: `{bundle['source_system']}`")
        lines.append(f"- bundle_label: `{bundle['bundle_label']}`")
        lines.append(f"- primary_ref: `{bundle['primary_ref']}`")
        lines.append(f"- bundle_summary: {bundle['bundle_summary']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_preview(preview: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_preview_md(preview), encoding="utf-8")


def run(
    *,
    db_path: Path,
    preview_json: Path,
    preview_md: Path,
    write: bool,
    limit: int,
) -> dict[str, Any]:
    generated_at = utc_now()
    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        source_tables = required_source_tables(con)
        bundles = build_bundles(con, limit=limit, created_at=generated_at)
        if write:
            write_bundles_table(con, bundles)
        preview = build_preview(
            mode="write" if write else "dry-run",
            db_path=db_path,
            source_tables=source_tables,
            bundles=bundles,
            table_written=write,
            generated_at=generated_at,
        )
    write_preview(preview, preview_json, preview_md)
    return preview


def print_summary(preview: dict[str, Any]) -> None:
    stats = preview["stats"]
    print(f"mode: {preview['mode']}")
    print(f"table_written: {preview['table_written']}")
    print(f"bundle_count: {stats['total']}")
    print(f"bundle_type_distribution: {stats['bundle_type_distribution']}")
    print(f"source_system_distribution: {stats['source_system_distribution']}")
    print(f"all_have_evidence_refs: {stats['all_have_evidence_refs']}")
    print(f"all_have_source_refs: {stats['all_have_source_refs']}")
    print(f"preview_json: {rel(PREVIEW_JSON)}")
    print(f"preview_md: {rel(PREVIEW_MD)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 09 Wave 3 memory evidence bundles.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="generate preview files only; this is the default")
    mode.add_argument("--write", action="store_true", help="rebuild and populate memory_evidence_bundles")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--preview-json", type=Path, default=PREVIEW_JSON)
    parser.add_argument("--preview-md", type=Path, default=PREVIEW_MD)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    preview = run(
        db_path=args.db,
        preview_json=args.preview_json,
        preview_md=args.preview_md,
        write=args.write,
        limit=max(1, args.limit),
    )
    print_summary(preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
