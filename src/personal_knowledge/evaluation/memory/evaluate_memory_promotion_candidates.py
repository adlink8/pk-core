"""Phase 09 Wave 5: weighted gate for memory promotion candidates.

The script reads `memory_promotion_candidates`, validates evidence/source refs,
computes a weighted score, and emits an auditable report. It never writes to
long-term memory tables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
OUT_JSON = AI_DIR / "memory_promotion_report.json"
OUT_MD = AI_DIR / "memory_promotion_report.md"

PHASE = "09"
WAVE = "5"
PROMPT_VERSION = "memory_promotion_gate/v2"
SCHEMA_VERSION = "v2"
TABLE_NAME = "memory_promotion_candidates"

AUTO_APPROVAL_MIN_SCORE = 0.85
REVIEW_MIN_SCORE = 0.60

UPSTREAM_CONSERVATIVE_STATUSES = {"needs_live_llm_review", "reject_or_review", "review_required"}
ALLOWED_MEMORY_TYPES = {"preference", "fact", "project", "habit", "capability", "tooling"}
EXPLAINABLE_RELATION_TYPES = {
    None,
    "",
    "evidenced_by_event",
    "preference_signal",
    "capability_signal",
    "tooling_signal",
    "follow_up",
    "enables",
    "same_problem",
    "subproblem_of",
    "tool_used_for",
    "contradiction",
    "temporal_next",
}
ONE_TIME_RELATION_TYPES = {"same_problem", "follow_up", "temporal_next"}
ONE_TIME_HINTS = (
    "同一具体任务",
    "同一具体问题",
    "具体任务",
    "一次性任务",
    "本次作业",
    "报错",
    "临时",
    "temporary",
    "one-off",
    "one time",
)
NEGATED_ONE_TIME_HINTS = ("非一次性", "不是一次性", "不属于一次性")
WEIGHT_SPECS = [
    ("evidence_completeness", 0.25),
    ("traceability", 0.20),
    ("cross_session_recurrence", 0.20),
    ("long_term_usefulness", 0.15),
    ("non_one_time_confidence", 0.10),
    ("consistency_with_existing_memory", 0.10),
]
RISK_PENALTY_WEIGHTS = {
    "no_live_llm": 0.10,
    "upstream_requires_review": 0.05,
    "duplicate_candidate": 0.05,
    "mid_confidence": 0.05,
    "low_confidence": 0.15,
    "invalid_confidence": 0.10,
    "inactive_candidate_source": 0.15,
    "traceability_gap": 0.10,
    "evidence_resolution_gap": 0.10,
}
HARD_RISK_CODES = {
    "missing_source_refs",
    "missing_evidence_refs",
    "one_time_task",
    "conflict_with_existing_memory",
    "schema_invalid",
    "unresolved_risk_flags",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def clamp01(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def decode_ref_container(value: object) -> tuple[list[Any], str | None]:
    if value is None:
        return [], None
    if isinstance(value, list):
        return value, None
    if isinstance(value, tuple):
        return list(value), None
    if isinstance(value, dict):
        return [value], None
    if not isinstance(value, str):
        return [], "unsupported_ref_container"
    if not value.strip():
        return [], None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [], "invalid_json"
    if isinstance(parsed, list):
        return parsed, None
    if isinstance(parsed, dict):
        return [parsed], None
    return [], "invalid_json_shape"


def json_loads_list(value: object) -> list[Any]:
    refs, _ = decode_ref_container(value)
    return refs


def json_loads_any(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def detect_llm_status() -> str:
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("MEM0_API_KEY"):
        return "live_api_key_present"
    return "fallback:no_api_key"


def has_live_llm(llm_status: str) -> bool:
    return llm_status == "live_api_key_present"


def parse_string_ref(ref: str) -> dict[str, Any]:
    text = ref.strip()
    if not text:
        return {"parsed": False, "resolved": False, "kind": "empty"}
    if re.match(r"^[A-Za-z0-9_./\\ -]+:[A-Za-z_]+/[A-Za-z0-9_.:-]+$", text):
        return {"parsed": True, "resolved": True, "kind": "db_ref", "ref": text}
    if ":" in text:
        head, tail = text.rsplit(":", 1)
        if head and tail:
            return {"parsed": True, "resolved": True, "kind": "path_or_source_ref", "ref": text}
    return {"parsed": False, "resolved": False, "kind": "unrecognized_string_ref", "ref": text}


def sqlite_exists(con: sqlite3.Connection, table: str, where: str, args: tuple[Any, ...]) -> bool:
    if not table_exists(con, table):
        return False
    row = con.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", args).fetchone()
    return row is not None


def parse_dict_ref(con: sqlite3.Connection, ref: dict[str, Any]) -> dict[str, Any]:
    table = ref.get("table")
    if not table:
        return {"parsed": False, "resolved": False, "kind": "dict_without_table"}

    resolved = False
    if table == "unified_events_rich" and ref.get("event_id"):
        resolved = sqlite_exists(con, table, "event_id=?", (ref["event_id"],))
    elif table == "memory_items" and ref.get("memory_id"):
        resolved = sqlite_exists(con, table, "memory_id=?", (ref["memory_id"],))
    elif table == "memory_links" and ref.get("link_id") is not None:
        resolved = sqlite_exists(con, table, "id=?", (ref["link_id"],))
    elif table in {"graph_relation_candidates", "graph_relation_judgments"} and ref.get("candidate_id"):
        resolved = sqlite_exists(con, table, "candidate_id=?", (ref["candidate_id"],))
    elif table == "conversation_turns_summary":
        if ref.get("session_id") and ref.get("turn_id"):
            resolved = sqlite_exists(con, table, "session_id=? AND turn_id=?", (ref["session_id"], ref["turn_id"]))
        elif ref.get("session_id") and ref.get("turn_no") is not None:
            resolved = sqlite_exists(con, table, "session_id=? AND turn_no=?", (ref["session_id"], ref["turn_no"]))
    else:
        return {
            "parsed": True,
            "resolved": False,
            "kind": "unknown_or_incomplete_table_ref",
            "table": table,
        }

    return {"parsed": True, "resolved": resolved, "kind": "table_ref", "table": table}


def parse_refs(con: sqlite3.Connection, raw: object) -> dict[str, Any]:
    refs, decode_error = decode_ref_container(raw)
    details = []
    session_ids: set[str] = set()
    turn_ids: set[str] = set()
    event_ids: set[str] = set()
    for ref in refs:
        if isinstance(ref, str):
            details.append(parse_string_ref(ref))
        elif isinstance(ref, dict):
            if ref.get("session_id"):
                session_ids.add(str(ref["session_id"]))
            if ref.get("turn_id"):
                turn_ids.add(str(ref["turn_id"]))
            if ref.get("event_id"):
                event_ids.add(str(ref["event_id"]))
            details.append(parse_dict_ref(con, ref))
        else:
            details.append({"parsed": False, "resolved": False, "kind": "unsupported_ref_type"})

    parsed_count = sum(1 for item in details if item.get("parsed"))
    resolved_count = sum(1 for item in details if item.get("resolved"))
    return {
        "refs": refs,
        "details": details,
        "decode_error": decode_error,
        "non_empty": bool(refs),
        "all_parseable": bool(refs) and decode_error is None and parsed_count == len(refs),
        "all_resolved": bool(refs) and decode_error is None and resolved_count == len(refs),
        "session_ids": sorted(session_ids),
        "turn_ids": sorted(turn_ids),
        "event_ids": sorted(event_ids),
    }


def claim_is_one_time(claim: str, relation_type: str | None) -> bool:
    if relation_type in ONE_TIME_RELATION_TYPES:
        return True
    text = claim or ""
    if any(hint in text for hint in NEGATED_ONE_TIME_HINTS):
        return False
    lowered = text.lower()
    return any(hint.lower() in lowered for hint in ONE_TIME_HINTS)


def canonical_claim(claim: str) -> str:
    return " ".join((claim or "").split())


def merge_or_replace_target(row: dict[str, Any]) -> dict[str, Any]:
    duplicate = row.get("duplicate_of_memory_id")
    conflict = row.get("conflict_with_memory_id")
    if conflict:
        return {"action": "replace", "memory_id": conflict, "reason": "candidate conflicts with existing memory"}
    if duplicate:
        return {"action": "merge", "memory_id": duplicate, "reason": "candidate duplicates existing memory"}
    return {"action": "none", "memory_id": None, "reason": "no duplicate or conflict target"}


def load_candidates(con: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(con, TABLE_NAME):
        raise RuntimeError(f"missing table: {TABLE_NAME}")
    con.row_factory = sqlite3.Row
    return [dict(row) for row in con.execute(f"SELECT * FROM {TABLE_NAME} ORDER BY promotion_id")]


def load_upstream_risk_flags(row: dict[str, Any]) -> list[str]:
    for key in ("risk_flags_json", "risk_flags", "candidate_risk_flags_json", "candidate_risk_flags"):
        if key not in row:
            continue
        value = row.get(key)
        parsed = json_loads_any(value)
        if isinstance(parsed, list):
            return sorted({str(item).strip() for item in parsed if str(item).strip()})
        if isinstance(parsed, str) and parsed.strip():
            return [parsed.strip()]
    review_reason = str(row.get("review_reason") or "")
    if "risk_flag:" in review_reason:
        return sorted(set(re.findall(r"risk_flag:([A-Za-z0-9_.-]+)", review_reason)))
    return []


def build_failure_reason(code: str, severity: str, message: str, *, field: str | None = None) -> dict[str, Any]:
    reason = {"code": code, "severity": severity, "message": message}
    if field:
        reason["field"] = field
    return reason


def collect_failure_reasons(
    *,
    llm_status: str,
    upstream_status: str | None,
    source_system: str | None,
    claim: str,
    relation_type: str | None,
    memory_type: str | None,
    evidence: dict[str, Any],
    source: dict[str, Any],
    confidence: float,
    confidence_invalid: bool,
    duplicate_id: str | None,
    conflict_id: str | None,
    one_time_task: bool,
    upstream_risk_flags: list[str],
    traceability_score: float,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    failure_reasons: list[dict[str, Any]] = []
    hard_risk_flags: list[str] = []
    soft_risk_flags: list[str] = []
    risk_penalties: list[str] = []

    def add_hard(code: str, message: str, *, field: str | None = None) -> None:
        if code not in hard_risk_flags:
            hard_risk_flags.append(code)
        failure_reasons.append(build_failure_reason(code, "hard", message, field=field))

    def add_soft(code: str, message: str, *, field: str | None = None, penalty: bool = True) -> None:
        if code not in soft_risk_flags:
            soft_risk_flags.append(code)
        failure_reasons.append(build_failure_reason(code, "soft", message, field=field))
        if penalty and code in RISK_PENALTY_WEIGHTS and code not in risk_penalties:
            risk_penalties.append(code)

    if evidence["decode_error"]:
        add_hard("schema_invalid", "evidence refs JSON is invalid", field="evidence_refs_json")
        add_soft("evidence_resolution_gap", "evidence refs could not be decoded", field="evidence_refs_json")
    elif not evidence["non_empty"]:
        add_hard("missing_evidence_refs", "candidate has no evidence refs", field="evidence_refs_json")
    else:
        if not evidence["all_parseable"]:
            add_hard("schema_invalid", "evidence refs contain unparseable entries", field="evidence_refs_json")
        if not evidence["all_resolved"]:
            add_soft("evidence_resolution_gap", "evidence refs did not fully resolve", field="evidence_refs_json")

    if source["decode_error"]:
        add_hard("schema_invalid", "source refs JSON is invalid", field="source_refs_json")
        add_soft("traceability_gap", "source refs could not be decoded", field="source_refs_json")
    elif not source["non_empty"]:
        add_hard("missing_source_refs", "candidate has no source refs", field="source_refs_json")
    else:
        if not source["all_parseable"]:
            add_hard("schema_invalid", "source refs contain unparseable entries", field="source_refs_json")
        if not source["all_resolved"]:
            add_soft("traceability_gap", "source refs did not fully resolve", field="source_refs_json")

    if not claim:
        add_hard("schema_invalid", "candidate claim is empty", field="proposed_claim")
    if relation_type not in EXPLAINABLE_RELATION_TYPES:
        add_hard("schema_invalid", f"unsupported relation_type={relation_type}", field="relation_type")
    if memory_type not in ALLOWED_MEMORY_TYPES:
        add_hard("schema_invalid", f"invalid proposed_memory_type={memory_type}", field="proposed_memory_type")
    if one_time_task:
        add_hard("one_time_task", "candidate is task-specific or one-time")
    if conflict_id:
        add_hard("conflict_with_existing_memory", "candidate conflicts with existing memory", field="conflict_with_memory_id")
    if upstream_risk_flags:
        add_hard("unresolved_risk_flags", f"upstream risk flags remain unresolved: {', '.join(upstream_risk_flags)}")
    if source_system and source_system not in {"graph_relation_candidate", "llm_memory_candidate", "manual_review_import"}:
        add_soft("inactive_candidate_source", f"source_system is outside the active promotion pipeline: {source_system}")
    if duplicate_id:
        add_soft("duplicate_candidate", "candidate duplicates an existing memory", field="duplicate_of_memory_id")
    if upstream_status in UPSTREAM_CONSERVATIVE_STATUSES:
        add_soft("upstream_requires_review", f"upstream status requires review: {upstream_status}", field="promotion_status")
    if not has_live_llm(llm_status):
        add_soft("no_live_llm", "no live LLM/API key; auto approval is disabled")
    if confidence_invalid:
        add_soft("invalid_confidence", "candidate confidence is missing or invalid", field="confidence")
    elif confidence < 0.55:
        add_soft("low_confidence", f"candidate confidence is below 0.55: {confidence}", field="confidence")
    elif confidence < 0.75:
        add_soft("mid_confidence", f"candidate confidence is below 0.75: {confidence}", field="confidence")
    if traceability_score < 0.6:
        add_soft("traceability_gap", "traceability signal is too weak for auto approval")

    return failure_reasons, sorted(set(hard_risk_flags)), sorted(set(soft_risk_flags)), risk_penalties


def gather_session_ids(row: dict[str, Any], evidence: dict[str, Any], source: dict[str, Any]) -> list[str]:
    sessions = set(evidence["session_ids"]) | set(source["session_ids"])
    if row.get("session_id"):
        sessions.add(str(row["session_id"]))
    return sorted(sessions)


def score_components(
    row: dict[str, Any],
    *,
    claim: str,
    evidence: dict[str, Any],
    source: dict[str, Any],
    confidence: float,
    duplicate_id: str | None,
    conflict_id: str | None,
    one_time_task: bool,
    live_llm: bool,
) -> dict[str, Any]:
    evidence_quality = 0.0
    if evidence["non_empty"]:
        evidence_quality = 0.4
        if evidence["all_parseable"]:
            evidence_quality += 0.3
        if evidence["all_resolved"]:
            evidence_quality += 0.3

    traceability_quality = 0.0
    if source["non_empty"]:
        traceability_quality = 0.4
        if source["all_parseable"]:
            traceability_quality += 0.3
        if source["all_resolved"]:
            traceability_quality += 0.2
        if row.get("session_id") and row.get("turn_id"):
            traceability_quality += 0.1

    session_count = len(gather_session_ids(row, evidence, source))
    if session_count >= 2:
        recurrence_quality = 1.0
    elif session_count == 1:
        recurrence_quality = 0.2
    else:
        recurrence_quality = 0.0

    if one_time_task or not claim:
        usefulness_quality = 0.0
    elif len(claim) >= 20:
        usefulness_quality = 1.0
    elif len(claim) >= 10:
        usefulness_quality = 0.7
    else:
        usefulness_quality = 0.4

    if one_time_task:
        non_one_time_confidence = 0.0
    else:
        non_one_time_confidence = clamp01(confidence)

    if conflict_id:
        consistency = 0.0
    elif duplicate_id:
        consistency = 0.5
    else:
        consistency = 1.0

    raw_scores = {
        "evidence_completeness": clamp01(evidence_quality),
        "traceability": clamp01(traceability_quality),
        "cross_session_recurrence": clamp01(recurrence_quality),
        "long_term_usefulness": clamp01(usefulness_quality),
        "non_one_time_confidence": clamp01(non_one_time_confidence),
        "consistency_with_existing_memory": clamp01(consistency),
    }
    components = {}
    for name, weight in WEIGHT_SPECS:
        components[name] = {
            "weight": weight,
            "raw_score": raw_scores[name],
            "weighted_score": round(raw_scores[name] * weight, 4),
        }
    components["live_llm_chain_credible"] = live_llm and evidence["all_resolved"] and source["all_resolved"]
    components["session_count"] = session_count
    return components


def calculate_penalty_breakdown(codes: list[str]) -> tuple[list[dict[str, Any]], float]:
    breakdown = []
    total = 0.0
    for code in sorted(set(codes)):
        weight = RISK_PENALTY_WEIGHTS.get(code)
        if weight is None:
            continue
        breakdown.append({"code": code, "penalty": weight})
        total += weight
    return breakdown, round(total, 4)


def evaluate_candidate(row: dict[str, Any], con: sqlite3.Connection, llm_status: str) -> dict[str, Any]:
    evidence = parse_refs(con, row.get("evidence_refs_json"))
    source = parse_refs(con, row.get("source_refs_json"))
    relation_type = row.get("relation_type")
    memory_type = row.get("proposed_memory_type")
    claim = canonical_claim(str(row.get("proposed_claim") or ""))
    upstream_status = row.get("promotion_status")
    duplicate_id = row.get("duplicate_of_memory_id")
    conflict_id = row.get("conflict_with_memory_id")
    upstream_risk_flags = load_upstream_risk_flags(row)

    confidence_invalid = False
    try:
        confidence = float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
        confidence_invalid = True
    confidence = clamp01(confidence)
    one_time_task = claim_is_one_time(claim, relation_type)

    base_components = score_components(
        row,
        claim=claim,
        evidence=evidence,
        source=source,
        confidence=confidence,
        duplicate_id=duplicate_id,
        conflict_id=conflict_id,
        one_time_task=one_time_task,
        live_llm=has_live_llm(llm_status),
    )
    failure_reasons, hard_risk_flags, soft_risk_flags, penalty_codes = collect_failure_reasons(
        llm_status=llm_status,
        upstream_status=upstream_status,
        source_system=row.get("source_system"),
        claim=claim,
        relation_type=relation_type,
        memory_type=memory_type,
        evidence=evidence,
        source=source,
        confidence=confidence,
        confidence_invalid=confidence_invalid,
        duplicate_id=duplicate_id,
        conflict_id=conflict_id,
        one_time_task=one_time_task,
        upstream_risk_flags=upstream_risk_flags,
        traceability_score=base_components["traceability"]["raw_score"],
    )
    penalty_breakdown, penalty_total = calculate_penalty_breakdown(penalty_codes)
    weighted_total = round(
        sum(base_components[name]["weighted_score"] for name, _ in WEIGHT_SPECS),
        4,
    )
    final_score = clamp01(weighted_total - penalty_total)
    hard_risk_blocked = bool(set(hard_risk_flags) & HARD_RISK_CODES)
    live_llm_chain_credible = bool(base_components["live_llm_chain_credible"])
    auto_approval_eligible = (
        final_score >= AUTO_APPROVAL_MIN_SCORE
        and not hard_risk_blocked
        and has_live_llm(llm_status)
        and live_llm_chain_credible
        and not soft_risk_flags
    )
    if hard_risk_blocked or final_score < REVIEW_MIN_SCORE:
        promotion_status = "rejected"
    elif auto_approval_eligible:
        promotion_status = "approved"
    else:
        promotion_status = "review_required"
    human_review_required = not auto_approval_eligible

    gate_reasons = [reason["code"] for reason in failure_reasons] or ["approved"]
    reason = ", ".join(gate_reasons)
    return {
        "promotion_id": row["promotion_id"],
        "source_system": row.get("source_system"),
        "source_candidate_id": row.get("source_candidate_id"),
        "source_memory_id": row.get("source_memory_id"),
        "upstream_promotion_status": upstream_status,
        "promotion_status": promotion_status,
        "memory_type": memory_type,
        "canonical_claim": claim,
        "merge_or_replace_target": merge_or_replace_target(row),
        "risk_flags": sorted(set(hard_risk_flags + soft_risk_flags)),
        "hard_risk_flags": hard_risk_flags,
        "hard_risk_blocked": hard_risk_blocked,
        "human_review_required": human_review_required,
        "reason": reason,
        "gate_reasons": gate_reasons,
        "failure_reasons": failure_reasons,
        "confidence": confidence,
        "relation_type": relation_type,
        "evidence_ref_count": len(evidence["refs"]),
        "source_ref_count": len(source["refs"]),
        "evidence_all_parseable": evidence["all_parseable"],
        "source_refs_all_parseable": source["all_parseable"],
        "evidence_refs_resolved": evidence["all_resolved"],
        "source_refs_resolved": source["all_resolved"],
        "score_components": {
            key: value
            for key, value in base_components.items()
            if isinstance(value, dict)
        },
        "risk_penalties": penalty_breakdown,
        "weighted_score_total": weighted_total,
        "final_score": final_score,
        "auto_approval_eligible": auto_approval_eligible,
        "live_llm_chain_credible": live_llm_chain_credible,
        "evidence_refs": evidence["refs"],
        "source_refs": source["refs"],
        "allowed_event_ids": sorted(set(evidence["event_ids"]) | set(source["event_ids"])),
        "allowed_session_ids": gather_session_ids(row, evidence, source),
        "allowed_turn_ids": sorted(set(evidence["turn_ids"]) | set(source["turn_ids"]) | ({str(row["turn_id"])} if row.get("turn_id") else set())),
        "upstream_risk_flags": upstream_risk_flags,
    }


def long_term_counts(con: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("memory_items", "memory_links", "memory_relations"):
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def build_report(con: sqlite3.Connection, llm_status: str | None = None) -> dict[str, Any]:
    status = llm_status or detect_llm_status()
    candidates = load_candidates(con)
    reviews = [evaluate_candidate(row, con, status) for row in candidates]
    status_distribution = Counter(item["promotion_status"] for item in reviews)
    reason_distribution = Counter(reason for item in reviews for reason in item["gate_reasons"])
    risk_distribution = Counter(flag for item in reviews for flag in item["risk_flags"])
    hard_risk_distribution = Counter(flag for item in reviews for flag in item["hard_risk_flags"])
    approved = status_distribution.get("approved", 0)
    auto_eligible = sum(1 for item in reviews if item["auto_approval_eligible"])

    return {
        "generated_at": utc_now(),
        "phase": PHASE,
        "wave": WAVE,
        "scope": "weighted_gate_and_repair_loop_inputs",
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "llm_status": status,
        "db_path": rel(DB_PATH),
        "input_candidate_count": len(candidates),
        "status_distribution": dict(sorted(status_distribution.items())),
        "reason_distribution": dict(sorted(reason_distribution.items())),
        "risk_flag_distribution": dict(sorted(risk_distribution.items())),
        "hard_risk_distribution": dict(sorted(hard_risk_distribution.items())),
        "human_review_required_count": sum(1 for item in reviews if item["human_review_required"]),
        "approved_count": approved,
        "auto_approval_eligible_count": auto_eligible,
        "zero_approved_explanation": (
            "No candidates are eligible for automatic approval because live LLM evidence is unavailable "
            "or deterministic gate risks keep every candidate below the Wave 5 auto-approval bar."
            if approved == 0
            else ""
        ),
        "long_term_counts": long_term_counts(con),
        "reviews": reviews,
    }


def render_md(report: dict[str, Any]) -> str:
    lines = [
        "# Memory Promotion Review Report",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- phase: {report['phase']}",
        f"- wave: {report['wave']}",
        f"- prompt_version: `{report['prompt_version']}`",
        f"- llm_status: `{report['llm_status']}`",
        f"- input_candidate_count: {report['input_candidate_count']}",
        f"- approved_count: {report['approved_count']}",
        f"- auto_approval_eligible_count: {report['auto_approval_eligible_count']}",
        f"- human_review_required_count: {report['human_review_required_count']}",
        "",
        "## Status Distribution",
        "",
    ]
    for status, count in report["status_distribution"].items():
        lines.append(f"- `{status}`: {count}")
    lines += ["", "## Failure Reasons", ""]
    for reason, count in report["reason_distribution"].items():
        lines.append(f"- `{reason}`: {count}")
    lines += ["", "## Hard Risk Flags", ""]
    for flag, count in report["hard_risk_distribution"].items():
        lines.append(f"- `{flag}`: {count}")
    lines += ["", "## Risk Flags", ""]
    for flag, count in report["risk_flag_distribution"].items():
        lines.append(f"- `{flag}`: {count}")
    if report.get("zero_approved_explanation"):
        lines += ["", "## Why Approved Is 0", "", report["zero_approved_explanation"]]
    lines += ["", "## Long-Term Memory Counts", ""]
    for table, count in report["long_term_counts"].items():
        lines.append(f"- `{table}`: {count}")
    lines += ["", "## Sample Reviews", ""]
    for item in report["reviews"][:12]:
        lines.append(f"### {item['promotion_id']}")
        lines.append(f"- promotion_status: `{item['promotion_status']}`")
        lines.append(f"- final_score: {item['final_score']}")
        lines.append(f"- auto_approval_eligible: `{item['auto_approval_eligible']}`")
        lines.append(f"- hard_risk_blocked: `{item['hard_risk_blocked']}`")
        lines.append(f"- human_review_required: `{item['human_review_required']}`")
        lines.append(f"- hard_risk_flags: {', '.join(item['hard_risk_flags']) or '(none)'}")
        lines.append(f"- reason: {item['reason']}")
        lines.append(f"- canonical_claim: {item['canonical_claim']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")


def print_summary(report: dict[str, Any]) -> None:
    print(f"candidate_count: {report['input_candidate_count']}")
    print(f"status_distribution: {report['status_distribution']}")
    print(f"approved_count: {report['approved_count']}")
    print(f"auto_approval_eligible_count: {report['auto_approval_eligible_count']}")
    print(f"human_review_required_count: {report['human_review_required_count']}")
    print(f"long_term_counts: {report['long_term_counts']}")
    if report.get("zero_approved_explanation"):
        print(f"zero_approved_explanation: {report['zero_approved_explanation']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate memory promotion candidates.")
    parser.add_argument("--write", action="store_true", help="write JSON and Markdown reports")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--report-json", type=Path, default=OUT_JSON)
    parser.add_argument("--report-md", type=Path, default=OUT_MD)
    args = parser.parse_args(argv)

    with closing(sqlite3.connect(args.db)) as con:
        con.row_factory = sqlite3.Row
        report = build_report(con)

    print_summary(report)
    if args.write:
        write_report(report, args.report_json, args.report_md)
        print(f"[write] {rel(args.report_json)}")
        print(f"[write] {rel(args.report_md)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
