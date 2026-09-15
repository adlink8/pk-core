"""Phase 09 Wave 4: extract memory promotion candidates from evidence bundles.

This script is the only active path from structured evidence bundles into
`memory_promotion_candidates` with `source_system='llm_memory_candidate'`.

Rules:
- LLM never writes long-term memory tables directly.
- Without a live OpenAI-compatible runtime, the script emits a blocked report
  and writes nothing.
- No deterministic fallback claim generation is allowed.
- Every candidate must trace back to refs that already exist in the bundle
  input package.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_knowledge.core import llm as llm_mod
ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
PROMPT_DIR = ROOT / "assets" / "prompts" / "memory_candidate_extraction"
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
REPORT_JSON = AI_DIR / "memory_candidate_extraction_report.json"
REPORT_MD = AI_DIR / "memory_candidate_extraction_report.md"

TABLE_NAME = "memory_promotion_candidates"
BUNDLE_TABLE = "memory_evidence_bundles"
PROGRESS_TABLE = "memory_candidate_extraction_progress"
PROMPT_VERSION = "memory_candidate_extraction/v1"
DEFAULT_MODEL = os.environ.get("MEM0_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4"
DEFAULT_TEMPERATURE = 0.2
CALL_TIMEOUT = 120

ALLOWED_MEMORY_TYPES = {"preference", "fact", "project", "habit", "capability", "tooling"}
ALLOWED_SUBJECTS = {"user", "assistant", "project", "system"}
ALLOWED_EXTRACTION_STATUSES = {"proposed", "downgrade", "reject", "needs_human_review"}
ALLOWED_ONE_TIME_RISK = {"none", "low", "medium", "high"}
REVIEW_REQUIRED_STATUSES = {"proposed", "downgrade", "needs_human_review"}
RISK_REJECT_FLAGS = {
    "too_task_specific",
    "one_time_only",
    "temporary_error_context",
    "insufficient_evidence",
}

PROMOTION_FIELDS = [
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

CREATE_PROMOTION_SQL = f"""
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
);
CREATE INDEX IF NOT EXISTS idx_mpc_source_system ON {TABLE_NAME}(source_system);
CREATE INDEX IF NOT EXISTS idx_mpc_status ON {TABLE_NAME}(promotion_status);
CREATE INDEX IF NOT EXISTS idx_mpc_session_turn ON {TABLE_NAME}(session_id, turn_id);
"""

CREATE_PROGRESS_SQL = f"""
CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (
    bundle_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    schema_rejected INTEGER NOT NULL,
    evidence_rejected INTEGER NOT NULL,
    blocked_reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bundle_id, prompt_version, model)
);
CREATE INDEX IF NOT EXISTS idx_mcep_status ON {PROGRESS_TABLE}(status);
"""


@dataclass
class LLMRuntime:
    llm_status: str
    model: str
    temperature: float
    client: Any = None
    blocked_reason: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def stable_id(prefix: str, *parts: object) -> str:
    text = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


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


def unique_strs(values: list[Any] | tuple[Any, ...] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def unique_refs(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else str(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def clamp_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return round(max(0.0, min(0.99, confidence)), 4)


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def extract_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_prompt_assets() -> tuple[str, str]:
    return (
        (PROMPT_DIR / "v1_main.md").read_text(encoding="utf-8").strip(),
        (PROMPT_DIR / "v1_schema.md").read_text(encoding="utf-8").strip(),
    )


def resolve_llm_runtime(*, model: str = DEFAULT_MODEL, temperature: float = DEFAULT_TEMPERATURE) -> LLMRuntime:
    pi_enabled = (
        os.environ.get("PI_KERNEL_AI_WORKFLOW", "").strip() == "1"
        or bool(os.environ.get("PI_KERNEL_INTERNAL_CAPABILITY"))
        or os.environ.get("PI_KERNEL_LEGACY_MODE", "").strip() == "1"
    )
    if not pi_enabled:
        return LLMRuntime(
            llm_status="blocked:no_live_llm",
            model=model,
            temperature=temperature,
            blocked_reason="Pi Kernel 未启动或缺少内部能力",
        )
    try:
        client = llm_mod.make_llm_client(purpose="memory_candidate_extraction")
    except SystemExit as exc:
        return LLMRuntime(
            llm_status="blocked:no_live_llm",
            model=model,
            temperature=temperature,
            blocked_reason=str(exc) or "failed to create OpenAI-compatible client",
        )
    except Exception as exc:  # pragma: no cover - defensive path
        return LLMRuntime(
            llm_status="blocked:no_live_llm",
            model=model,
            temperature=temperature,
            blocked_reason=f"failed to create OpenAI-compatible client: {type(exc).__name__}",
        )
    return LLMRuntime(
        llm_status="live_api_key_present",
        model=model,
        temperature=temperature,
        client=client,
    )


def canonical_ref_token(ref: Any) -> str | None:
    if isinstance(ref, str):
        text = ref.strip()
        return text or None
    if not isinstance(ref, dict):
        return None
    table = str(ref.get("table") or "").strip()
    if not table:
        return None
    if ref.get("bundle_id"):
        return f"{table}:bundle_id/{ref['bundle_id']}"
    if ref.get("candidate_id"):
        return f"{table}:candidate_id/{ref['candidate_id']}"
    if ref.get("event_id"):
        return f"{table}:event_id/{ref['event_id']}"
    if ref.get("turn_id"):
        return f"{table}:turn_id/{ref['turn_id']}"
    if ref.get("memory_id"):
        return f"{table}:memory_id/{ref['memory_id']}"
    if ref.get("session_id") and ref.get("turn_no") is not None:
        return f"{table}:session_turn/{ref['session_id']}:{ref['turn_no']}"
    if ref.get("session_id"):
        return f"{table}:session_id/{ref['session_id']}"
    if ref.get("link_id") is not None:
        return f"{table}:link_id/{ref['link_id']}"
    return None


def maybe_append_id(container: set[str], value: object) -> None:
    text = str(value or "").strip()
    if text:
        container.add(text)


def resolve_context_record(con: sqlite3.Connection, ref: dict[str, Any]) -> dict[str, Any] | None:
    table = ref.get("table")
    if table == "unified_events_rich" and ref.get("event_id") and table_exists(con, table):
        row = con.execute(
            "SELECT event_id, content_rich, content_rich_source FROM unified_events_rich WHERE event_id = ?",
            (ref["event_id"],),
        ).fetchone()
        return dict(row) if row else None
    if table == "conversation_turns_summary" and table_exists(con, table):
        if ref.get("session_id") and ref.get("turn_id"):
            row = con.execute(
                """
                SELECT session_id, turn_no, turn_id, narrative, source_ref, main_topic
                FROM conversation_turns_summary
                WHERE session_id = ? AND turn_id = ?
                LIMIT 1
                """,
                (ref["session_id"], ref["turn_id"]),
            ).fetchone()
            return dict(row) if row else None
        if ref.get("session_id") and ref.get("turn_no") is not None:
            row = con.execute(
                """
                SELECT session_id, turn_no, turn_id, narrative, source_ref, main_topic
                FROM conversation_turns_summary
                WHERE session_id = ? AND turn_no = ?
                LIMIT 1
                """,
                (ref["session_id"], ref["turn_no"]),
            ).fetchone()
            return dict(row) if row else None
    if table in {"graph_relation_candidates", "graph_relation_judgments"} and ref.get("candidate_id") and table_exists(con, table):
        row = con.execute(f"SELECT * FROM {table} WHERE candidate_id = ? LIMIT 1", (ref["candidate_id"],)).fetchone()
        return dict(row) if row else None
    return None


def build_bundle_input(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    evidence_refs = json_loads_list(row["evidence_refs_json"])
    source_refs = json_loads_list(row["source_refs_json"])
    duplicate_targets = unique_strs(json_loads_list(row["duplicate_check_targets_json"]))
    conflict_targets = unique_strs(json_loads_list(row["conflict_check_targets_json"]))

    bundle_ref = f"memory_evidence_bundles:bundle_id/{row['bundle_id']}"
    ref_map: dict[str, Any] = {bundle_ref: bundle_ref}
    event_ids: set[str] = set()
    session_ids: set[str] = set()
    turn_ids: set[str] = set()
    resolved_context: list[dict[str, Any]] = []

    for ref in evidence_refs + source_refs:
        token = canonical_ref_token(ref)
        if token:
            ref_map[token] = ref
        if isinstance(ref, dict):
            maybe_append_id(event_ids, ref.get("event_id"))
            maybe_append_id(session_ids, ref.get("session_id"))
            maybe_append_id(turn_ids, ref.get("turn_id"))
            resolved = resolve_context_record(con, ref)
            if resolved is not None:
                resolved_context.append({"table": ref["table"], "record": resolved})
                maybe_append_id(event_ids, resolved.get("event_id"))
                maybe_append_id(session_ids, resolved.get("session_id"))
                maybe_append_id(turn_ids, resolved.get("turn_id"))
                if resolved.get("source_ref"):
                    token = canonical_ref_token(str(resolved["source_ref"]))
                    if token:
                        ref_map[token] = str(resolved["source_ref"])

    return {
        "bundle_id": row["bundle_id"],
        "bundle_type": row["bundle_type"],
        "source_system": row["source_system"],
        "bundle_label": row["bundle_label"],
        "bundle_summary": row["bundle_summary"],
        "primary_ref": row["primary_ref"],
        "evidence_refs": evidence_refs,
        "source_refs": source_refs,
        "duplicate_targets": duplicate_targets,
        "conflict_targets": conflict_targets,
        "allowed_ref_map": ref_map,
        "allowed_ref_tokens": sorted(ref_map),
        "allowed_event_ids": sorted(event_ids),
        "allowed_session_ids": sorted(session_ids),
        "allowed_turn_ids": sorted(turn_ids),
        "resolved_context": resolved_context,
    }


def load_bundle_inputs(con: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    if not table_exists(con, BUNDLE_TABLE):
        raise RuntimeError(f"missing table: {BUNDLE_TABLE}")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""
        SELECT bundle_id, bundle_type, source_system, bundle_label, bundle_summary, primary_ref,
               evidence_refs_json, source_refs_json, duplicate_check_targets_json, conflict_check_targets_json
        FROM {BUNDLE_TABLE}
        ORDER BY created_at DESC, bundle_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [build_bundle_input(con, row) for row in rows]


def build_llm_messages(bundle_input: dict[str, Any], main_prompt: str, schema_text: str) -> list[dict[str, str]]:
    system_prompt = (
        f"{main_prompt}\n\n"
        "下面是输出 schema。你必须严格遵守，只能引用输入里给出的 allowed_ref_tokens、event_ids、session_ids、turn_ids。\n\n"
        f"{schema_text}"
    )
    payload = {
        "bundle_id": bundle_input["bundle_id"],
        "prompt_version": PROMPT_VERSION,
        "llm_status": "live_api_key_present",
        "bundle": {
            "bundle_type": bundle_input["bundle_type"],
            "source_system": bundle_input["source_system"],
            "bundle_label": bundle_input["bundle_label"],
            "bundle_summary": bundle_input["bundle_summary"],
            "primary_ref": bundle_input["primary_ref"],
            "evidence_refs": bundle_input["evidence_refs"],
            "source_refs": bundle_input["source_refs"],
        },
        "allowed_ref_tokens": bundle_input["allowed_ref_tokens"],
        "event_ids": bundle_input["allowed_event_ids"],
        "session_ids": bundle_input["allowed_session_ids"],
        "turn_ids": bundle_input["allowed_turn_ids"],
        "duplicate_check_targets": bundle_input["duplicate_targets"],
        "conflict_check_targets": bundle_input["conflict_targets"],
        "resolved_context": bundle_input["resolved_context"],
    }
    user_prompt = (
        "请基于下面 evidence bundle 提炼长期记忆候选。只能输出 JSON，不要附加解释。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def call_llm(runtime: LLMRuntime, messages: list[dict[str, str]]) -> dict[str, Any] | None:
    resp = runtime.client.chat.completions.create(
        model=runtime.model,
        messages=messages,
        temperature=runtime.temperature,
        timeout=CALL_TIMEOUT,
    )
    raw = resp.choices[0].message.content
    return extract_json(raw)


def validate_claim_fields(claim: dict[str, Any], bundle_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not isinstance(claim, dict):
        return None, "claim is not an object", None

    candidate_id = str(claim.get("candidate_id") or "").strip()
    candidate_claim = str(claim.get("candidate_claim") or "").strip()
    memory_type = str(claim.get("memory_type") or "").strip()
    subject = str(claim.get("subject") or "").strip()
    extraction_status = str(claim.get("extraction_status") or "").strip()
    long_term_value_reason = str(claim.get("long_term_value_reason") or "").strip()
    one_time_task_risk = str(claim.get("one_time_task_risk") or "").strip()
    duplicate_check_hint = str(claim.get("duplicate_check_hint") or "").strip()
    conflict_check_hint = str(claim.get("conflict_check_hint") or "").strip()

    if not all(
        [
            candidate_id,
            candidate_claim,
            memory_type,
            subject,
            extraction_status,
            long_term_value_reason,
            one_time_task_risk,
        ]
    ):
        return None, "missing required claim fields", None
    if memory_type not in ALLOWED_MEMORY_TYPES:
        return None, f"invalid memory_type={memory_type}", None
    if subject not in ALLOWED_SUBJECTS:
        return None, f"invalid subject={subject}", None
    if extraction_status not in ALLOWED_EXTRACTION_STATUSES:
        return None, f"invalid extraction_status={extraction_status}", None
    if one_time_task_risk not in ALLOWED_ONE_TIME_RISK:
        return None, f"invalid one_time_task_risk={one_time_task_risk}", None

    evidence_refs = unique_strs(claim.get("evidence_refs") or [])
    source_refs = unique_strs(claim.get("source_refs") or [])
    event_ids = unique_strs(claim.get("event_ids") or [])
    session_ids = unique_strs(claim.get("session_ids") or [])
    turn_ids = unique_strs(claim.get("turn_ids") or [])
    risk_flags = unique_strs(claim.get("risk_flags") or [])
    needs_human_review = bool(claim.get("needs_human_review", False))

    if needs_human_review and extraction_status not in {"needs_human_review", "downgrade"}:
        return None, "needs_human_review=true requires needs_human_review or downgrade", None
    if extraction_status == "proposed":
        if one_time_task_risk == "high":
            return None, "proposed claim cannot use one_time_task_risk=high", None
        if not evidence_refs or not source_refs:
            return None, "proposed claim requires evidence_refs and source_refs", None

    allowed_refs = set(bundle_input["allowed_ref_tokens"])
    allowed_event_ids = set(bundle_input["allowed_event_ids"])
    allowed_session_ids = set(bundle_input["allowed_session_ids"])
    allowed_turn_ids = set(bundle_input["allowed_turn_ids"])
    if any(ref not in allowed_refs for ref in evidence_refs):
        return None, None, "evidence_refs contain values outside bundle input"
    if any(ref not in allowed_refs for ref in source_refs):
        return None, None, "source_refs contain values outside bundle input"
    if any(value not in allowed_event_ids for value in event_ids):
        return None, None, "event_ids contain values outside bundle input"
    if any(value not in allowed_session_ids for value in session_ids):
        return None, None, "session_ids contain values outside bundle input"
    if any(value not in allowed_turn_ids for value in turn_ids):
        return None, None, "turn_ids contain values outside bundle input"

    return (
        {
            "candidate_id": candidate_id,
            "candidate_claim": candidate_claim,
            "memory_type": memory_type,
            "subject": subject,
            "extraction_status": extraction_status,
            "long_term_value_reason": long_term_value_reason,
            "one_time_task_risk": one_time_task_risk,
            "duplicate_check_hint": duplicate_check_hint,
            "conflict_check_hint": conflict_check_hint,
            "evidence_refs": evidence_refs,
            "source_refs": source_refs,
            "event_ids": event_ids,
            "session_ids": session_ids,
            "turn_ids": turn_ids,
            "confidence": clamp_confidence(claim.get("confidence")),
            "risk_flags": risk_flags,
            "needs_human_review": needs_human_review,
        },
        None,
        None,
    )


def normalize_llm_response(parsed: dict[str, Any] | None, bundle_input: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int, str | None]:
    if not parsed:
        return [], 1, 0, "LLM output is not valid JSON"
    if str(parsed.get("bundle_id") or "").strip() != bundle_input["bundle_id"]:
        return [], 1, 0, "bundle_id mismatch"
    claims = parsed.get("candidate_claims")
    if not isinstance(claims, list) or not claims:
        return [], 1, 0, "candidate_claims is empty"

    normalized_claims: list[dict[str, Any]] = []
    schema_rejected = 0
    evidence_rejected = 0
    for claim in claims:
        normalized, schema_error, evidence_error = validate_claim_fields(claim, bundle_input)
        if schema_error:
            schema_rejected += 1
            continue
        if evidence_error:
            evidence_rejected += 1
            continue
        assert normalized is not None
        normalized_claims.append(normalized)
    return normalized_claims, schema_rejected, evidence_rejected, None


def parse_hint_memory_id(hint: str, allowed_ids: list[str]) -> str | None:
    if not hint or not allowed_ids:
        return None
    for memory_id in allowed_ids:
        if memory_id in hint:
            return memory_id
    return None


def refs_from_tokens(tokens: list[str], bundle_input: dict[str, Any]) -> list[Any]:
    mapped = [bundle_input["allowed_ref_map"][token] for token in tokens if token in bundle_input["allowed_ref_map"]]
    return unique_refs(mapped)


def candidate_is_traceable(candidate: dict[str, Any]) -> bool:
    evidence_refs = json_loads_list(candidate["evidence_refs_json"])
    source_refs = json_loads_list(candidate["source_refs_json"])
    has_event = any(isinstance(ref, dict) and ref.get("event_id") for ref in evidence_refs + source_refs)
    has_turn = bool(candidate.get("session_id") and candidate.get("turn_id") and source_refs)
    return has_event or has_turn


def build_candidate_row(claim: dict[str, Any], bundle_input: dict[str, Any], *, created_at: str) -> dict[str, Any] | None:
    if claim["extraction_status"] not in REVIEW_REQUIRED_STATUSES:
        return None

    evidence_refs = refs_from_tokens(claim["evidence_refs"], bundle_input)
    source_refs = refs_from_tokens(claim["source_refs"], bundle_input)
    source_refs = unique_refs([f"memory_evidence_bundles:bundle_id/{bundle_input['bundle_id']}", *source_refs])
    if not evidence_refs or not source_refs:
        return None
    if any(isinstance(ref, dict) and ref.get("table") == "memory_items" for ref in evidence_refs + source_refs):
        return None

    duplicate_id = parse_hint_memory_id(claim["duplicate_check_hint"], bundle_input["duplicate_targets"])
    conflict_id = parse_hint_memory_id(claim["conflict_check_hint"], bundle_input["conflict_targets"])
    promotion_status = "review_required"
    if (
        claim["extraction_status"] != "proposed"
        or claim["one_time_task_risk"] in {"medium", "high"}
        or any(flag in RISK_REJECT_FLAGS for flag in claim["risk_flags"])
        or claim["needs_human_review"]
        or conflict_id
    ):
        promotion_status = "reject_or_review"

    session_id = claim["session_ids"][0] if claim["session_ids"] else None
    turn_id = claim["turn_ids"][0] if claim["turn_ids"] else None
    candidate = {
        "promotion_id": stable_id("mpc", bundle_input["bundle_id"], claim["candidate_id"]),
        "source_system": "llm_memory_candidate",
        "source_candidate_id": claim["candidate_id"],
        "source_memory_id": None,
        "session_id": session_id,
        "turn_id": turn_id,
        "relation_type": None,
        "proposed_memory_type": claim["memory_type"],
        "proposed_subject": claim["subject"],
        "proposed_claim": claim["candidate_claim"],
        "confidence": claim["confidence"],
        "evidence_refs_json": json_dumps(evidence_refs),
        "source_refs_json": json_dumps(source_refs),
        "duplicate_of_memory_id": duplicate_id,
        "conflict_with_memory_id": conflict_id,
        "promotion_status": promotion_status,
        "review_reason": (
            f"LLM extracted from evidence bundle {bundle_input['bundle_id']} "
            f"with extraction_status={claim['extraction_status']}; "
            f"long_term_value_reason={claim['long_term_value_reason']}"
        ),
        "created_at": created_at,
    }
    if not candidate_is_traceable(candidate):
        return None
    return candidate


def clear_llm_outputs(con: sqlite3.Connection, *, model: str) -> None:
    con.executescript(CREATE_PROMOTION_SQL)
    con.execute(f"DELETE FROM {TABLE_NAME} WHERE source_system = 'llm_memory_candidate'")
    con.executescript(CREATE_PROGRESS_SQL)
    con.execute(
        f"DELETE FROM {PROGRESS_TABLE} WHERE prompt_version=? AND model=?",
        (PROMPT_VERSION, model),
    )
    con.commit()


def load_completed_bundle_ids(con: sqlite3.Connection, *, model: str) -> set[str]:
    con.executescript(CREATE_PROGRESS_SQL)
    rows = con.execute(
        f"""
        SELECT bundle_id
        FROM {PROGRESS_TABLE}
        WHERE prompt_version=? AND model=? AND status IN ('ok', 'rejected')
        """,
        (PROMPT_VERSION, model),
    ).fetchall()
    return {str(row[0]) for row in rows}


def write_progress(con: sqlite3.Connection, result: dict[str, Any], *, model: str) -> None:
    con.executescript(CREATE_PROGRESS_SQL)
    con.execute(
        f"""
        INSERT OR REPLACE INTO {PROGRESS_TABLE} (
            bundle_id, prompt_version, model, status, candidate_count,
            schema_rejected, evidence_rejected, blocked_reason, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result["bundle_id"],
            PROMPT_VERSION,
            model,
            result["status"],
            int(result["candidate_count"]),
            int(result["schema_rejected"]),
            int(result["evidence_rejected"]),
            result.get("blocked_reason") or "",
            utc_now(),
        ),
    )


def write_candidates(con: sqlite3.Connection, candidates: list[dict[str, Any]], *, replace_existing: bool = True) -> int:
    con.executescript(CREATE_PROMOTION_SQL)
    if replace_existing:
        con.execute(f"DELETE FROM {TABLE_NAME} WHERE source_system = 'llm_memory_candidate'")
    if not candidates:
        con.commit()
        return 0
    rows = [tuple(candidate[field] for field in PROMOTION_FIELDS) for candidate in candidates]
    placeholders = ",".join("?" for _ in PROMOTION_FIELDS)
    con.executemany(
        f"INSERT OR REPLACE INTO {TABLE_NAME} ({','.join(PROMOTION_FIELDS)}) VALUES ({placeholders})",
        rows,
    )
    con.commit()
    return len(rows)


def process_bundle(
    bundle: dict[str, Any],
    runtime: LLMRuntime,
    main_prompt: str,
    schema_text: str,
    *,
    created_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, int, int, int]:
    messages = build_llm_messages(bundle, main_prompt, schema_text)
    try:
        parsed = call_llm(runtime, messages)
    except Exception as exc:
        return (
            {
                "bundle_id": bundle["bundle_id"],
                "status": "blocked",
                "candidate_count": 0,
                "schema_rejected": 0,
                "evidence_rejected": 0,
                "blocked_reason": f"LLM call failed: {type(exc).__name__}",
            },
            [],
            0,
            0,
            1,
            0,
        )

    normalized_claims, schema_rejected, evidence_rejected, top_level_error = normalize_llm_response(parsed, bundle)
    if top_level_error:
        return (
            {
                "bundle_id": bundle["bundle_id"],
                "status": "rejected",
                "candidate_count": 0,
                "schema_rejected": schema_rejected or 1,
                "evidence_rejected": evidence_rejected,
                "blocked_reason": top_level_error,
            },
            [],
            schema_rejected,
            evidence_rejected,
            1,
            0,
        )

    rows: list[dict[str, Any]] = []
    skipped_reject_count = 0
    for claim in normalized_claims:
        row = build_candidate_row(claim, bundle, created_at=created_at)
        if row is None:
            skipped_reject_count += 1
            continue
        rows.append(row)

    result = {
        "bundle_id": bundle["bundle_id"],
        "status": "ok" if rows else "rejected",
        "candidate_count": len(rows),
        "schema_rejected": schema_rejected,
        "evidence_rejected": evidence_rejected,
        "blocked_reason": "",
    }
    return result, rows, schema_rejected, evidence_rejected, 0, skipped_reject_count


def build_report_md(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# Memory Candidate Extraction Report",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- mode: {report['mode']}",
        f"- llm_status: `{report['llm_status']}`",
        f"- blocked_reason: {report['blocked_reason'] or '(none)'}",
        f"- db_path: `{report['db_path']}`",
        "",
        "## Counts",
        "",
        f"- bundle_count: {counts['bundle_count']}",
        f"- candidate_count: {counts['candidate_count']}",
        f"- written_count: {counts['written_count']}",
        f"- schema_rejected_count: {counts['schema_rejected_count']}",
        f"- evidence_rejected_count: {counts['evidence_rejected_count']}",
        f"- llm_error_count: {counts['llm_error_count']}",
        f"- skipped_reject_count: {counts['skipped_reject_count']}",
        "",
        "## Bundle Results",
        "",
    ]
    for item in report["bundle_results"][:20]:
        lines.append(f"### {item['bundle_id']}")
        lines.append(f"- status: `{item['status']}`")
        lines.append(f"- candidate_count: {item['candidate_count']}")
        lines.append(f"- schema_rejected: {item['schema_rejected']}")
        lines.append(f"- evidence_rejected: {item['evidence_rejected']}")
        lines.append(f"- blocked_reason: {item['blocked_reason'] or '(none)'}")
        lines.append("")
    if report["candidates"]:
        lines += ["## Sample Candidates", ""]
        for candidate in report["candidates"][:12]:
            lines.append(f"### {candidate['promotion_id']}")
            lines.append(f"- source_system: `{candidate['source_system']}`")
            lines.append(f"- source_candidate_id: `{candidate['source_candidate_id']}`")
            lines.append(f"- proposed_memory_type: `{candidate['proposed_memory_type']}`")
            lines.append(f"- proposed_subject: `{candidate['proposed_subject']}`")
            lines.append(f"- promotion_status: `{candidate['promotion_status']}`")
            lines.append(f"- proposed_claim: {candidate['proposed_claim']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(build_report_md(report), encoding="utf-8")


def run(
    *,
    db_path: Path,
    report_json: Path,
    report_md: Path,
    write: bool,
    limit: int,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    resume: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    generated_at = utc_now()
    runtime = resolve_llm_runtime(model=model, temperature=temperature)
    main_prompt, schema_text = load_prompt_assets()

    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        bundles = load_bundle_inputs(con, limit=max(1, limit))
        if write and runtime.llm_status == "live_api_key_present" and not resume:
            # Don't clear existing candidates until we have new ones to replace them.
            # If LLM fails on all bundles, existing candidates are preserved.
            pass
        if resume:
            completed = load_completed_bundle_ids(con, model=runtime.model)
            if completed:
                bundles = [bundle for bundle in bundles if bundle["bundle_id"] not in completed]

        bundle_results: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        schema_rejected_count = 0
        evidence_rejected_count = 0
        llm_error_count = 0
        skipped_reject_count = 0

        if runtime.llm_status != "live_api_key_present":
            for bundle in bundles:
                bundle_results.append(
                    {
                        "bundle_id": bundle["bundle_id"],
                        "status": "blocked",
                        "candidate_count": 0,
                        "schema_rejected": 0,
                        "evidence_rejected": 0,
                        "blocked_reason": runtime.blocked_reason,
                    }
                )
        else:
            workers = max(1, min(workers, len(bundles) or 1))
            done = 0
            if write:
                con.executescript(CREATE_PROMOTION_SQL)
                con.executescript(CREATE_PROGRESS_SQL)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        process_bundle,
                        bundle,
                        runtime,
                        main_prompt,
                        schema_text,
                        created_at=generated_at,
                    ): bundle
                    for bundle in bundles
                }
                for fut in as_completed(futures):
                    result, rows, schema_rejected, evidence_rejected, llm_errors, skipped = fut.result()
                    bundle_results.append(result)
                    candidates.extend(rows)
                    schema_rejected_count += schema_rejected
                    evidence_rejected_count += evidence_rejected
                    llm_error_count += llm_errors
                    skipped_reject_count += skipped
                    done += 1
                    if write:
                        if rows:
                            write_candidates(con, rows, replace_existing=False)
                        write_progress(con, result, model=runtime.model)
                        con.commit()
                    if done == 1 or done % 25 == 0 or done == len(bundles):
                        print(
                            f"[extract] {done}/{len(bundles)} bundles "
                            f"candidates={len(candidates)} errors={llm_error_count}",
                            flush=True,
                        )

        written_count = 0
        if write and runtime.llm_status == "live_api_key_present":
            failed_without_replacement = not candidates and bool(
                llm_error_count or schema_rejected_count or evidence_rejected_count
            )
            if not failed_without_replacement:
                if resume:
                    written_count = con.execute(
                        f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE source_system = 'llm_memory_candidate'"
                    ).fetchone()[0]
                else:
                    # New run: replace existing LLM candidates with new ones.
                    # Only clear if we have new candidates to replace them.
                    if candidates:
                        con.execute(f"DELETE FROM {TABLE_NAME} WHERE source_system = 'llm_memory_candidate'")
                        con.executemany(
                            f"INSERT OR REPLACE INTO {TABLE_NAME} ({','.join(PROMOTION_FIELDS)}) "
                            f"VALUES ({','.join('?' for _ in PROMOTION_FIELDS)})",
                            [tuple(c[f] for f in PROMOTION_FIELDS) for c in candidates],
                        )
                        con.commit()
                    written_count = len(candidates)

    report = {
        "generated_at": generated_at,
        "mode": "write" if write else "dry-run",
        "db_path": rel(db_path),
        "llm_status": runtime.llm_status,
        "blocked_reason": runtime.blocked_reason,
        "model": runtime.model,
        "temperature": runtime.temperature,
        "resume": resume,
        "workers": workers,
        "counts": {
            "bundle_count": len(bundles),
            "candidate_count": len(candidates),
            "written_count": written_count,
            "schema_rejected_count": schema_rejected_count,
            "evidence_rejected_count": evidence_rejected_count,
            "llm_error_count": llm_error_count,
            "skipped_reject_count": skipped_reject_count,
        },
        "bundle_results": bundle_results,
        "candidates": candidates,
    }
    write_report(report, report_json, report_md)
    return report


def print_summary(report: dict[str, Any]) -> None:
    counts = report["counts"]
    print(f"mode: {report['mode']}")
    print(f"llm_status: {report['llm_status']}")
    print(f"blocked_reason: {report['blocked_reason'] or '(none)'}")
    print(f"bundle_count: {counts['bundle_count']}")
    print(f"candidate_count: {counts['candidate_count']}")
    print(f"written_count: {counts['written_count']}")
    print(f"schema_rejected_count: {counts['schema_rejected_count']}")
    print(f"evidence_rejected_count: {counts['evidence_rejected_count']}")
    print(f"llm_error_count: {counts['llm_error_count']}")
    print(f"skipped_reject_count: {counts['skipped_reject_count']}")
    print(f"report_json: {rel(REPORT_JSON)}")
    print(f"report_md: {rel(REPORT_MD)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract LLM memory candidates from memory_evidence_bundles.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="generate report only; this is the default")
    mode.add_argument("--write", action="store_true", help="write llm_memory_candidate rows into memory_promotion_candidates")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--report-json", type=Path, default=REPORT_JSON)
    parser.add_argument("--report-md", type=Path, default=REPORT_MD)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--resume", action="store_true", help="skip bundles already completed for this prompt/model")
    parser.add_argument("--workers", type=int, default=1, help="parallel LLM calls; use small values to avoid rate limits")
    args = parser.parse_args(argv)

    report = run(
        db_path=args.db,
        report_json=args.report_json,
        report_md=args.report_md,
        write=args.write,
        limit=args.limit,
        model=args.model,
        temperature=args.temperature,
        resume=args.resume,
        workers=args.workers,
    )
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
