"""Phase 09 Wave 3: build conservative memory promotion candidates.

This script only builds active promotion candidates from accepted graph
relations. Structured evidence no longer promotes directly. It must first be
assembled into memory evidence bundles and later extracted by an LLM candidate
step in Wave 4.

Usage:
  python integration\\scripts\\build_memory_promotion_candidates.py --dry-run
  python integration\\scripts\\build_memory_promotion_candidates.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
MATRIX_JSON = AI_DIR / "memory_mechanism_matrix.json"
PREVIEW_JSON = AI_DIR / "memory_promotion_candidates_preview.json"
PREVIEW_MD = AI_DIR / "memory_promotion_candidates_preview.md"

PHASE = "09"
WAVE = "3"
TABLE_NAME = "memory_promotion_candidates"
ALLOWED_SOURCE_SYSTEMS = {
    "graph_relation_candidate",
    "llm_memory_candidate",
    "manual_review_import",
}

ALLOWED_STATUSES = {"review_required", "reject_or_review", "needs_live_llm_review"}
DISALLOWED_STATUSES = {"approved", "promotion_ready"}

REQUIRED_FIELDS = [
    "promotion_id",
    "source_system",
    "source_candidate_id",
    "source_memory_id",
    "session_id",
    "turn_id",
    "relation_type",
    "proposed_memory_type",
    "proposed_subject",
    "proposed_claim",
    "confidence",
    "evidence_refs_json",
    "source_refs_json",
    "duplicate_of_memory_id",
    "conflict_with_memory_id",
    "promotion_status",
    "review_reason",
    "created_at",
]

LONG_TERM_RELATION_TYPES = {
    "preference_signal": "preference",
    "capability_signal": "capability",
    "tooling_signal": "tooling",
    "follow_up": "project",
    "enables": "project",
    "same_problem": "project",
}

ONE_TIME_RELATION_TYPES = {"same_problem"}
ONE_TIME_HINTS = ("同一具体", "一次性", "具体任务", "同一类实际任务", "报错", "作业", "题")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def stable_id(*parts: object) -> str:
    text = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"mpc:{digest}"


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


def clamp_confidence(value: object, *, cap: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.5
    return round(max(0.0, min(cap, confidence)), 4)


def load_mechanism_policy(path: Path = MATRIX_JSON) -> dict[str, Any]:
    matrix = load_json(path)
    steps = {row.get("mechanism_step"): row for row in matrix.get("mechanism_steps", []) if isinstance(row, dict)}
    return {
        "matrix_path": rel(path),
        "llm_status": matrix.get("llm_status", "unknown"),
        "prompt_version": matrix.get("prompt_version", "unknown"),
        "candidate_boundary": steps.get("candidate_generation", {}),
        "evidence_gate": steps.get("evidence_gate", {}),
        "promotion_policy": steps.get("promotion_policy", {}),
        "storage_boundary": steps.get("storage_boundary", {}),
    }


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def required_source_tables(con: sqlite3.Connection) -> dict[str, bool]:
    names = [
        "memory_items",
        "graph_relation_candidates",
        "graph_relation_judgments",
        "memory_evidence_bundles",
    ]
    return {name: table_exists(con, name) for name in names}


def conservative_status(policy: dict[str, Any], relation_type: str | None, reason: str | None) -> str:
    text = f"{relation_type or ''} {reason or ''}"
    one_time = relation_type in ONE_TIME_RELATION_TYPES or any(hint in text for hint in ONE_TIME_HINTS)
    if one_time:
        return "reject_or_review"
    if str(policy.get("llm_status", "")).startswith("fallback:no_api_key"):
        return "needs_live_llm_review"
    return "review_required"


def graph_proposed_subject(row: sqlite3.Row) -> str:
    return (
        f"{row['relation_type']} between "
        f"{row['source_session_id']}:{row['source_turn_id']} and "
        f"{row['target_session_id']}:{row['target_turn_id']}"
    )


def graph_source_refs(row: sqlite3.Row) -> list[Any]:
    refs = json_loads_list(row["candidate_source_refs_json"])
    refs.append(
        {
            "table": "graph_relation_candidates",
            "candidate_id": row["candidate_id"],
            "source_session_id": row["source_session_id"],
            "source_turn_id": row["source_turn_id"],
            "target_session_id": row["target_session_id"],
            "target_turn_id": row["target_turn_id"],
            "candidate_type": row["candidate_type"],
            "candidate_reason": row["candidate_reason"],
        }
    )
    refs.append(
        {
            "table": "graph_relation_judgments",
            "candidate_id": row["candidate_id"],
            "gate_status": row["gate_status"],
            "prompt_version": row["prompt_version"],
            "model": row["model"],
        }
    )
    return refs


def canonical_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def find_memory_target(
    con: sqlite3.Connection,
    proposed_memory_type: str,
    proposed_subject: str,
    proposed_claim: str,
) -> tuple[str | None, str | None]:
    if not table_exists(con, "memory_items"):
        return None, None

    normalized_subject = canonical_text(proposed_subject)
    normalized_claim = canonical_text(proposed_claim)
    if not normalized_subject and not normalized_claim:
        return None, None

    rows = con.execute(
        """
        SELECT memory_id, subject, description
        FROM memory_items
        WHERE memory_type = ?
        """,
        (proposed_memory_type,),
    ).fetchall()
    duplicate_id: str | None = None
    conflict_id: str | None = None
    for row in rows:
        row_subject = canonical_text(row["subject"])
        row_claim = canonical_text(row["description"])
        if normalized_subject and normalized_subject == row_subject and normalized_claim and normalized_claim == row_claim:
            duplicate_id = row["memory_id"]
            break
        if normalized_subject and normalized_subject == row_subject:
            conflict_id = row["memory_id"]
    return duplicate_id, conflict_id


def build_graph_candidates(
    con: sqlite3.Connection,
    policy: dict[str, Any],
    *,
    limit: int,
    created_at: str,
) -> list[dict[str, Any]]:
    if not (table_exists(con, "graph_relation_candidates") and table_exists(con, "graph_relation_judgments")):
        return []
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT
            j.candidate_id,
            j.relation_type,
            j.confidence,
            j.evidence_refs_json,
            j.reason,
            j.risk_flags_json,
            j.model,
            j.prompt_version,
            j.temperature,
            j.created_at AS judgment_created_at,
            j.gate_status,
            c.source_node_id,
            c.target_node_id,
            c.source_session_id,
            c.source_turn_id,
            c.target_session_id,
            c.target_turn_id,
            c.similarity,
            c.candidate_reason,
            c.candidate_type,
            c.source_refs_json AS candidate_source_refs_json
        FROM graph_relation_judgments j
        JOIN graph_relation_candidates c ON c.candidate_id = j.candidate_id
        WHERE j.gate_status = 'accepted'
        ORDER BY j.confidence DESC, j.candidate_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        evidence_refs = json_loads_list(row["evidence_refs_json"])
        source_refs = graph_source_refs(row)
        if not evidence_refs or not source_refs:
            continue
        proposed_memory_type = LONG_TERM_RELATION_TYPES.get(row["relation_type"], "fact")
        proposed_subject = graph_proposed_subject(row)
        proposed_claim = row["reason"]
        duplicate_of_memory_id, conflict_with_memory_id = find_memory_target(
            con,
            proposed_memory_type,
            proposed_subject,
            proposed_claim,
        )
        status = conservative_status(policy, row["relation_type"], proposed_claim)
        review_reason = (
            "Phase 07 gate_status=accepted is treated as relation evidence only; "
            "Wave 3 requires promotion review and forbids automatic long-term writes."
        )
        if status == "needs_live_llm_review":
            review_reason += " Wave 2 llm_status=fallback:no_api_key, so live promotion judging is still required."
        if status == "reject_or_review":
            review_reason += " Relation appears task-specific or one-off and must not auto-promote."
        candidate = {
            "promotion_id": stable_id("graph_relation_candidate", row["candidate_id"]),
            "source_system": "graph_relation_candidate",
            "source_candidate_id": row["candidate_id"],
            "source_memory_id": None,
            "session_id": row["source_session_id"],
            "turn_id": row["source_turn_id"],
            "relation_type": row["relation_type"],
            "proposed_memory_type": proposed_memory_type,
            "proposed_subject": proposed_subject,
            "proposed_claim": proposed_claim,
            "confidence": clamp_confidence(row["confidence"], cap=0.95),
            "evidence_refs_json": json_dumps(evidence_refs),
            "source_refs_json": json_dumps(source_refs),
            "duplicate_of_memory_id": duplicate_of_memory_id,
            "conflict_with_memory_id": conflict_with_memory_id,
            "promotion_status": status,
            "review_reason": review_reason,
            "created_at": created_at,
        }
        validate_candidate(candidate)
        candidates.append(candidate)
    return candidates


def traceable(candidate: dict[str, Any]) -> bool:
    evidence_refs = json_loads_list(candidate.get("evidence_refs_json"))
    source_refs = json_loads_list(candidate.get("source_refs_json"))
    has_event = any(isinstance(ref, dict) and ref.get("event_id") for ref in evidence_refs + source_refs)
    has_turn = bool(candidate.get("session_id") and candidate.get("turn_id") and source_refs)
    return has_event or has_turn


def validate_candidate(candidate: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_FIELDS if field not in candidate]
    if missing:
        raise ValueError(f"candidate missing fields: {missing}")
    if candidate["promotion_status"] in DISALLOWED_STATUSES:
        raise ValueError(f"disallowed promotion_status: {candidate['promotion_status']}")
    if candidate["promotion_status"] not in ALLOWED_STATUSES:
        raise ValueError(f"unknown promotion_status: {candidate['promotion_status']}")
    if candidate["source_system"] not in ALLOWED_SOURCE_SYSTEMS:
        raise ValueError(f"disallowed source_system: {candidate['source_system']}")
    if not json_loads_list(candidate["evidence_refs_json"]):
        raise ValueError(f"{candidate['promotion_id']} has no evidence_refs_json")
    if not json_loads_list(candidate["source_refs_json"]):
        raise ValueError(f"{candidate['promotion_id']} has no source_refs_json")
    if not traceable(candidate):
        raise ValueError(f"{candidate['promotion_id']} is not traceable to event_id or session_id+turn_id")
    source_refs = json_loads_list(candidate["source_refs_json"])
    if candidate.get("source_memory_id") and all(
        isinstance(ref, dict) and ref.get("table") == "memory_items" for ref in source_refs
    ):
        raise ValueError(f"{candidate['promotion_id']} cannot rely on memory_items as the only source")
    for field in ("promotion_id", "source_system", "proposed_memory_type", "proposed_subject", "proposed_claim"):
        if not candidate.get(field):
            raise ValueError(f"{candidate['promotion_id']} has empty {field}")


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        deduped[candidate["promotion_id"]] = candidate
    return [deduped[key] for key in sorted(deduped)]


def build_candidates(
    con: sqlite3.Connection,
    policy: dict[str, Any],
    *,
    max_graph: int = 50,
    max_legacy: int = 20,
    evidence_per_legacy: int = 5,
    created_at: str | None = None,
) -> list[dict[str, Any]]:
    created = created_at or utc_now()
    candidates = []
    candidates.extend(build_graph_candidates(con, policy, limit=max_graph, created_at=created))
    return dedupe_candidates(candidates)


def candidate_stats(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(candidate["promotion_status"] for candidate in candidates)
    sources = Counter(candidate["source_system"] for candidate in candidates)
    types = Counter(candidate["proposed_memory_type"] for candidate in candidates)
    return {
        "total": len(candidates),
        "status_distribution": dict(sorted(statuses.items())),
        "source_system_distribution": dict(sorted(sources.items())),
        "proposed_memory_type_distribution": dict(sorted(types.items())),
        "all_have_evidence_refs": all(bool(json_loads_list(c["evidence_refs_json"])) for c in candidates),
        "all_have_source_refs": all(bool(json_loads_list(c["source_refs_json"])) for c in candidates),
        "all_traceable": all(traceable(c) for c in candidates),
        "disallowed_status_count": sum(1 for c in candidates if c["promotion_status"] in DISALLOWED_STATUSES),
    }


def create_candidates_table(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            promotion_id TEXT PRIMARY KEY,
            source_system TEXT NOT NULL,
            source_candidate_id TEXT,
            source_memory_id TEXT,
            session_id TEXT,
            turn_id TEXT,
            relation_type TEXT,
            proposed_memory_type TEXT NOT NULL,
            proposed_subject TEXT NOT NULL,
            proposed_claim TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            source_refs_json TEXT NOT NULL,
            duplicate_of_memory_id TEXT,
            conflict_with_memory_id TEXT,
            promotion_status TEXT NOT NULL,
            review_reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_mpc_source_system ON {TABLE_NAME}(source_system)")
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_mpc_status ON {TABLE_NAME}(promotion_status)")
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_mpc_session_turn ON {TABLE_NAME}(session_id, turn_id)")


def write_candidates_table(con: sqlite3.Connection, candidates: list[dict[str, Any]]) -> None:
    create_candidates_table(con)
    con.execute(f"DELETE FROM {TABLE_NAME} WHERE source_system = 'graph_relation_candidate'")
    rows = [tuple(candidate[field] for field in REQUIRED_FIELDS) for candidate in candidates]
    placeholders = ",".join("?" for _ in REQUIRED_FIELDS)
    con.executemany(
        f"INSERT OR REPLACE INTO {TABLE_NAME} ({','.join(REQUIRED_FIELDS)}) VALUES ({placeholders})",
        rows,
    )
    con.commit()


def build_preview(
    *,
    mode: str,
    db_path: Path,
    policy: dict[str, Any],
    source_tables: dict[str, bool],
    candidates: list[dict[str, Any]],
    table_written: bool,
    generated_at: str,
) -> dict[str, Any]:
    stats = candidate_stats(candidates)
    return {
        "generated_at": generated_at,
        "phase": PHASE,
        "wave": WAVE,
        "scope": "target_pipeline_candidate_boundary",
        "mode": mode,
        "db_path": rel(db_path),
        "table_name": TABLE_NAME,
        "table_written": table_written,
        "mechanism_policy": {
            "matrix_path": policy["matrix_path"],
            "llm_status": policy["llm_status"],
            "prompt_version": policy["prompt_version"],
            "candidate_boundary": policy.get("candidate_boundary", {}).get("merged_method"),
            "evidence_gate": policy.get("evidence_gate", {}).get("merged_method"),
            "promotion_policy": policy.get("promotion_policy", {}).get("merged_method"),
            "storage_boundary": policy.get("storage_boundary", {}).get("merged_method"),
        },
        "source_tables": source_tables,
        "candidate_strategy": [
            "graph_relation_judgments gate_status='accepted' joined to graph_relation_candidates, but accepted edges remain relation evidence only.",
            "structured evidence is excluded from direct promotion generation and must first be written into memory_evidence_bundles.",
            "memory_items are only consulted as duplicate/conflict targets, never as candidate sources.",
            "No candidate can be emitted without evidence_refs_json and source_refs_json.",
            "No approved or promotion_ready statuses are emitted while llm_status is fallback:no_api_key.",
        ],
        "limits": {
            "graph_accepted_candidates": "bounded by --max-graph",
            "legacy_parameters": "--max-legacy and --evidence-per-legacy are deprecated and ignored",
        },
        "stats": stats,
        "candidates": candidates,
    }


def render_preview_md(preview: dict[str, Any]) -> str:
    stats = preview["stats"]
    lines = [
        "# Memory Promotion Candidates Preview",
        "",
        f"- generated_at: {preview['generated_at']}",
        f"- phase: {preview['phase']}",
        f"- wave: {preview['wave']}",
        f"- mode: {preview['mode']}",
        f"- table_written: {preview['table_written']}",
        f"- llm_status: `{preview['mechanism_policy']['llm_status']}`",
        "",
        "## Scope",
        "",
        "Wave 3 builds a candidate boundary from the Wave 2 mechanism matrix. It does not compare old memory results against new graph results, and it does not write long-term memory tables.",
        "",
        "## Counts",
        "",
        f"- total candidates: {stats['total']}",
        f"- all_have_evidence_refs: {stats['all_have_evidence_refs']}",
        f"- all_have_source_refs: {stats['all_have_source_refs']}",
        f"- all_traceable: {stats['all_traceable']}",
        f"- disallowed_status_count: {stats['disallowed_status_count']}",
        "",
        "### Status Distribution",
        "",
    ]
    for status, count in stats["status_distribution"].items():
        lines.append(f"- `{status}`: {count}")
    lines += ["", "### Source System Distribution", ""]
    for source, count in stats["source_system_distribution"].items():
        lines.append(f"- `{source}`: {count}")
    lines += ["", "## Candidate Strategy", ""]
    lines.extend(f"- {item}" for item in preview["candidate_strategy"])
    lines += ["", "## Sample Candidates", ""]
    for candidate in preview["candidates"][:12]:
        lines.append(f"### {candidate['promotion_id']}")
        lines.append(f"- source_system: `{candidate['source_system']}`")
        lines.append(f"- source_candidate_id: `{candidate['source_candidate_id']}`")
        lines.append(f"- source_memory_id: `{candidate['source_memory_id']}`")
        lines.append(f"- session_id: `{candidate['session_id']}`")
        lines.append(f"- turn_id: `{candidate['turn_id']}`")
        lines.append(f"- relation_type: `{candidate['relation_type']}`")
        lines.append(f"- proposed_memory_type: `{candidate['proposed_memory_type']}`")
        lines.append(f"- promotion_status: `{candidate['promotion_status']}`")
        lines.append(f"- review_reason: {candidate['review_reason']}")
        lines.append(f"- proposed_subject: {candidate['proposed_subject']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_preview(preview: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_preview_md(preview), encoding="utf-8")


def run(
    *,
    db_path: Path,
    matrix_path: Path,
    preview_json: Path,
    preview_md: Path,
    write: bool,
    max_graph: int,
    max_legacy: int,
    evidence_per_legacy: int,
) -> dict[str, Any]:
    generated_at = utc_now()
    _ = max_legacy
    _ = evidence_per_legacy
    policy = load_mechanism_policy(matrix_path)
    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        source_tables = required_source_tables(con)
        candidates = build_candidates(
            con,
            policy,
            max_graph=max_graph,
            created_at=generated_at,
        )
        if write:
            write_candidates_table(con, candidates)
        preview = build_preview(
            mode="write" if write else "dry-run",
            db_path=db_path,
            policy=policy,
            source_tables=source_tables,
            candidates=candidates,
            table_written=write,
            generated_at=generated_at,
        )
    return preview


def print_summary(preview: dict[str, Any]) -> None:
    stats = preview["stats"]
    print(f"mode: {preview['mode']}")
    print(f"table_written: {preview['table_written']}")
    print(f"candidate_count: {stats['total']}")
    print(f"status_distribution: {stats['status_distribution']}")
    print(f"source_system_distribution: {stats['source_system_distribution']}")
    print(f"all_have_evidence_refs: {stats['all_have_evidence_refs']}")
    print(f"all_have_source_refs: {stats['all_have_source_refs']}")
    print(f"all_traceable: {stats['all_traceable']}")
    print(f"disallowed_status_count: {stats['disallowed_status_count']}")
    print("preview_files_written: False")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 09 Wave 3 memory promotion candidates.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="generate preview files only; this is the default")
    mode.add_argument("--write", action="store_true", help="rebuild and populate memory_promotion_candidates")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--matrix", type=Path, default=MATRIX_JSON)
    parser.add_argument("--preview-json", type=Path, default=PREVIEW_JSON, help="deprecated ignored")
    parser.add_argument("--preview-md", type=Path, default=PREVIEW_MD, help="deprecated ignored")
    parser.add_argument("--max-graph", type=int, default=50)
    parser.add_argument("--max-legacy", type=int, default=20, help="deprecated ignored")
    parser.add_argument("--evidence-per-legacy", type=int, default=5, help="deprecated ignored")
    args = parser.parse_args(argv)

    preview = run(
        db_path=args.db,
        matrix_path=args.matrix,
        preview_json=args.preview_json,
        preview_md=args.preview_md,
        write=args.write,
        max_graph=args.max_graph,
        max_legacy=args.max_legacy,
        evidence_per_legacy=args.evidence_per_legacy,
    )
    print_summary(preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
