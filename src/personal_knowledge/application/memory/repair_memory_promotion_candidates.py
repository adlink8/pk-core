"""Phase 09 Wave 5: repair-loop audit for memory promotion candidates.

The repair loop never writes to long-term memory tables. It consumes the
weighted gate report, asks the LLM for conservative repair/downgrade/reject
advice, and writes an audit report only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_knowledge.core import llm as llm_mod
from personal_knowledge.evaluation.memory import evaluate_memory_promotion_candidates as eval_mod
ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
PROMPT_DIR = ROOT / "assets" / "prompts" / "gate_repair_loop"
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
PROMOTION_REPORT_JSON = AI_DIR / "memory_promotion_report.json"
REPORT_JSON = AI_DIR / "memory_gate_repair_report.json"
REPORT_MD = AI_DIR / "memory_gate_repair_report.md"

PROMPT_VERSION = "gate_repair_loop/v1"
DEFAULT_MODEL = os.environ.get("MEM0_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4"
DEFAULT_TEMPERATURE = 0.0
CALL_TIMEOUT = 120
MAX_REPAIR_ROUNDS = 2
ALLOWED_REPAIR_ACTIONS = {"repair", "downgrade", "reject"}
ALLOWED_REPAIRED_STATUSES = {"proposed", "downgrade", "reject", "needs_human_review"}
ALLOWED_REPAIRED_FIELDS = {
    "candidate_claim",
    "canonical_claim",
    "proposed_claim",
    "memory_type",
    "proposed_memory_type",
    "relation_type",
    "risk_flags",
    "needs_human_review",
    "review_reason",
    "confidence",
    "final_score",
}


@dataclass
class LLMRuntime:
    llm_status: str
    model: str
    temperature: float
    client: Any = None
    blocked_reason: str = ""


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


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
        client = llm_mod.make_llm_client(purpose="memory_repair")
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


def load_report_or_evaluate(db_path: Path, report_path: Path) -> dict[str, Any]:
    if report_path.exists():
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("reviews"), list):
            return data
    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        return eval_mod.build_report(con)


def build_repair_payload(review: dict[str, Any]) -> dict[str, Any]:
    allowed_ref_tokens = sorted(
        {
            token
            for token in [canonical_ref_token(ref) for ref in review.get("evidence_refs", []) + review.get("source_refs", [])]
            if token
        }
    )
    return {
        "candidate_kind": "memory_candidate",
        "candidate_id": review["promotion_id"],
        "candidate": {
            "promotion_id": review["promotion_id"],
            "source_system": review.get("source_system"),
            "source_candidate_id": review.get("source_candidate_id"),
            "memory_type": review.get("memory_type"),
            "canonical_claim": review.get("canonical_claim"),
            "relation_type": review.get("relation_type"),
            "final_score": review.get("final_score"),
            "auto_approval_eligible": review.get("auto_approval_eligible"),
            "human_review_required": review.get("human_review_required"),
            "hard_risk_flags": review.get("hard_risk_flags", []),
            "risk_flags": review.get("risk_flags", []),
        },
        "failure_reasons": review.get("failure_reasons", []),
        "allowed_evidence_refs": review.get("evidence_refs", []),
        "allowed_source_refs": review.get("source_refs", []),
        "allowed_ref_tokens": allowed_ref_tokens,
        "allowed_event_ids": review.get("allowed_event_ids", []),
        "allowed_session_ids": review.get("allowed_session_ids", []),
        "allowed_turn_ids": review.get("allowed_turn_ids", []),
        "duplicate_or_conflict_hint": review.get("merge_or_replace_target"),
        "upstream_risk_flags": review.get("upstream_risk_flags", []),
    }


def build_messages(payload: dict[str, Any], main_prompt: str, schema_text: str, validation_error: str | None = None) -> list[dict[str, str]]:
    system_prompt = (
        f"{main_prompt}\n\n"
        "下面是输出 schema。你必须严格遵守，只能引用输入里已经提供的 refs 和 ids。\n\n"
        f"{schema_text}"
    )
    user_lines = [
        "请基于下面失败候选输出 repair/downgrade/reject 建议，只能输出 JSON。",
        json.dumps(payload, ensure_ascii=False, indent=2),
    ]
    if validation_error:
        user_lines.insert(1, f"上一次输出无效，原因：{validation_error}。请修正并严格返回 JSON。")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_lines)},
    ]


def call_llm(runtime: LLMRuntime, messages: list[dict[str, str]]) -> dict[str, Any] | None:
    resp = runtime.client.chat.completions.create(
        model=runtime.model,
        messages=messages,
        temperature=runtime.temperature,
        timeout=CALL_TIMEOUT,
    )
    return extract_json(resp.choices[0].message.content)


def validate_repair_output(parsed: dict[str, Any] | None, payload: dict[str, Any], review: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not parsed:
        return None, "LLM output is not valid JSON"

    required_fields = {
        "prompt_version",
        "model",
        "temperature",
        "llm_status",
        "candidate_kind",
        "candidate_id",
        "repair_action",
        "repaired_status",
        "kept_evidence_refs",
        "kept_source_refs",
        "repaired_fields",
        "unresolved_gate_reasons",
        "repair_reason",
    }
    missing = sorted(required_fields - set(parsed))
    if missing:
        return None, f"missing required fields: {', '.join(missing)}"
    if parsed["prompt_version"] != PROMPT_VERSION:
        return None, "prompt_version mismatch"
    if parsed["candidate_kind"] != "memory_candidate":
        return None, "candidate_kind must be memory_candidate"
    if parsed["candidate_id"] != review["promotion_id"]:
        return None, "candidate_id mismatch"
    if parsed["repair_action"] not in ALLOWED_REPAIR_ACTIONS:
        return None, "invalid repair_action"
    if parsed["repaired_status"] not in ALLOWED_REPAIRED_STATUSES:
        return None, "invalid repaired_status"
    if parsed["repair_action"] == "reject" and parsed["repaired_status"] != "reject":
        return None, "reject action must output repaired_status=reject"
    if parsed["repair_action"] == "downgrade" and parsed["repaired_status"] not in {"downgrade", "needs_human_review"}:
        return None, "downgrade action must output downgrade or needs_human_review"
    if review.get("hard_risk_flags") and parsed["repair_action"] == "repair":
        return None, "hard-risk candidates cannot be marked as repair"

    evidence_refs = parsed.get("kept_evidence_refs")
    source_refs = parsed.get("kept_source_refs")
    if not isinstance(evidence_refs, list) or not isinstance(source_refs, list):
        return None, "kept_evidence_refs and kept_source_refs must be arrays"
    allowed_tokens = set(payload["allowed_ref_tokens"])
    evidence_ref_tokens = [canonical_ref_token(ref) or str(ref).strip() for ref in evidence_refs]
    source_ref_tokens = [canonical_ref_token(ref) or str(ref).strip() for ref in source_refs]
    if any(ref not in allowed_tokens for ref in evidence_ref_tokens):
        return None, "kept_evidence_refs contain refs outside the candidate input"
    if any(ref not in allowed_tokens for ref in source_ref_tokens):
        return None, "kept_source_refs contain refs outside the candidate input"

    for field_name, allowed_key in (
        ("event_ids", "allowed_event_ids"),
        ("session_ids", "allowed_session_ids"),
        ("turn_ids", "allowed_turn_ids"),
    ):
        values = parsed.get(field_name, [])
        if not isinstance(values, list):
            return None, f"{field_name} must be an array"
        allowed_values = {str(item) for item in payload[allowed_key]}
        if any(str(item) not in allowed_values for item in values):
            return None, f"{field_name} contain ids outside the candidate input"

    unresolved = parsed.get("unresolved_gate_reasons")
    if not isinstance(unresolved, list):
        return None, "unresolved_gate_reasons must be an array"
    allowed_reason_codes = {reason["code"] for reason in review.get("failure_reasons", []) if isinstance(reason, dict) and reason.get("code")}
    if any(str(item) not in allowed_reason_codes for item in unresolved):
        return None, "unresolved_gate_reasons must come from existing failure reasons"

    repaired_fields = parsed.get("repaired_fields")
    if not isinstance(repaired_fields, dict):
        return None, "repaired_fields must be an object"
    unknown_fields = sorted(set(repaired_fields) - ALLOWED_REPAIRED_FIELDS)
    if unknown_fields:
        return None, f"repaired_fields contain unsupported keys: {', '.join(unknown_fields)}"
    if "confidence" in repaired_fields:
        try:
            new_confidence = float(repaired_fields["confidence"])
        except (TypeError, ValueError):
            return None, "repaired_fields.confidence must be numeric"
        if new_confidence > float(review.get("confidence") or 0.0):
            return None, "repair output cannot raise confidence above the original candidate"
    if "final_score" in repaired_fields:
        try:
            new_final_score = float(repaired_fields["final_score"])
        except (TypeError, ValueError):
            return None, "repaired_fields.final_score must be numeric"
        if new_final_score > float(review.get("final_score") or 0.0):
            return None, "repair output cannot raise final_score above the original candidate"
    if "risk_flags" in repaired_fields and not isinstance(repaired_fields["risk_flags"], list):
        return None, "repaired_fields.risk_flags must be an array"
    if "needs_human_review" in repaired_fields and not isinstance(repaired_fields["needs_human_review"], bool):
        return None, "repaired_fields.needs_human_review must be boolean"
    if not str(parsed.get("repair_reason") or "").strip():
        return None, "repair_reason is required"

    return parsed, None


def select_candidates(report: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    reviews = report.get("reviews") or []
    selected = [
        item
        for item in reviews
        if not item.get("auto_approval_eligible")
        and item.get("promotion_status") in {"review_required", "rejected"}
    ]
    return selected[: max(1, limit)]


def render_md(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# Memory Gate Repair Report",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- mode: {report['mode']}",
        f"- llm_status: `{report['llm_status']}`",
        f"- blocked_reason: {report['blocked_reason'] or '(none)'}",
        f"- source_report_path: `{report['source_report_path']}`",
        "",
        "## Counts",
        "",
        f"- input_candidate_count: {counts['input_candidate_count']}",
        f"- processed_count: {counts['processed_count']}",
        f"- repair_count: {counts['repair_count']}",
        f"- downgrade_count: {counts['downgrade_count']}",
        f"- reject_count: {counts['reject_count']}",
        f"- blocked_count: {counts['blocked_count']}",
        f"- invalid_output_count: {counts['invalid_output_count']}",
        "",
        "## Candidate Results",
        "",
    ]
    for item in report["candidate_results"][:20]:
        lines.append(f"### {item['promotion_id']}")
        lines.append(f"- status: `{item['status']}`")
        lines.append(f"- attempt_count: {item['attempt_count']}")
        lines.append(f"- repair_action: `{item['repair_action'] or '(none)'}`")
        lines.append(f"- repaired_status: `{item['repaired_status'] or '(none)'}`")
        lines.append(f"- validation_error: {item['validation_error'] or '(none)'}")
        lines.append(f"- repair_reason: {item['repair_reason'] or '(none)'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")


def run(
    *,
    db_path: Path,
    promotion_report_json: Path,
    report_json: Path,
    report_md: Path,
    dry_run: bool,
    write: bool,
    limit: int,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> dict[str, Any]:
    runtime = resolve_llm_runtime(model=model, temperature=temperature)
    main_prompt, schema_text = load_prompt_assets()
    source_report = load_report_or_evaluate(db_path, promotion_report_json)
    candidates = select_candidates(source_report, limit)

    candidate_results: list[dict[str, Any]] = []
    repair_count = 0
    downgrade_count = 0
    reject_count = 0
    blocked_count = 0
    invalid_output_count = 0

    for review in candidates:
        payload = build_repair_payload(review)
        if runtime.llm_status != "live_api_key_present":
            blocked_count += 1
            candidate_results.append(
                {
                    "promotion_id": review["promotion_id"],
                    "status": "blocked",
                    "attempt_count": 0,
                    "repair_action": None,
                    "repaired_status": None,
                    "validation_error": None,
                    "repair_reason": runtime.blocked_reason,
                    "repair_output": None,
                }
            )
            continue

        last_error: str | None = None
        parsed_output: dict[str, Any] | None = None
        validated_output: dict[str, Any] | None = None
        attempt_count = 0
        for round_no in range(1, MAX_REPAIR_ROUNDS + 1):
            attempt_count = round_no
            messages = build_messages(payload, main_prompt, schema_text, validation_error=last_error)
            try:
                parsed_output = call_llm(runtime, messages)
            except Exception as exc:  # pragma: no cover - defensive path
                last_error = f"LLM call failed: {type(exc).__name__}"
                continue
            validated_output, last_error = validate_repair_output(parsed_output, payload, review)
            if validated_output is not None:
                break

        if validated_output is None:
            invalid_output_count += 1
            candidate_results.append(
                {
                    "promotion_id": review["promotion_id"],
                    "status": "invalid_output",
                    "attempt_count": attempt_count,
                    "repair_action": None,
                    "repaired_status": None,
                    "validation_error": last_error,
                    "repair_reason": None,
                    "repair_output": parsed_output,
                }
            )
            continue

        action = validated_output["repair_action"]
        status = action
        if action == "repair":
            repair_count += 1
        elif action == "downgrade":
            downgrade_count += 1
        else:
            reject_count += 1
        candidate_results.append(
            {
                "promotion_id": review["promotion_id"],
                "status": status,
                "attempt_count": attempt_count,
                "repair_action": action,
                "repaired_status": validated_output["repaired_status"],
                "validation_error": None,
                "repair_reason": validated_output["repair_reason"],
                "repair_output": validated_output,
            }
        )

    report = {
        "generated_at": eval_mod.utc_now(),
        "mode": "write" if write else "dry-run",
        "llm_status": runtime.llm_status,
        "blocked_reason": runtime.blocked_reason,
        "source_report_path": rel(promotion_report_json),
        "counts": {
            "input_candidate_count": len(candidates),
            "processed_count": len(candidate_results),
            "repair_count": repair_count,
            "downgrade_count": downgrade_count,
            "reject_count": reject_count,
            "blocked_count": blocked_count,
            "invalid_output_count": invalid_output_count,
        },
        "candidate_results": candidate_results,
    }
    write_report(report, report_json, report_md)
    return report


def print_summary(report: dict[str, Any]) -> None:
    counts = report["counts"]
    print(f"mode: {report['mode']}")
    print(f"llm_status: {report['llm_status']}")
    print(f"blocked_reason: {report['blocked_reason'] or '(none)'}")
    print(f"input_candidate_count: {counts['input_candidate_count']}")
    print(f"processed_count: {counts['processed_count']}")
    print(f"repair_count: {counts['repair_count']}")
    print(f"downgrade_count: {counts['downgrade_count']}")
    print(f"reject_count: {counts['reject_count']}")
    print(f"blocked_count: {counts['blocked_count']}")
    print(f"invalid_output_count: {counts['invalid_output_count']}")
    print(f"report_json: {rel(REPORT_JSON)}")
    print(f"report_md: {rel(REPORT_MD)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the gate repair loop for memory promotion candidates.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="generate repair audit only; default")
    mode.add_argument("--write", action="store_true", help="write repair audit report only; no DB writes")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--promotion-report-json", type=Path, default=PROMOTION_REPORT_JSON)
    parser.add_argument("--report-json", type=Path, default=REPORT_JSON)
    parser.add_argument("--report-md", type=Path, default=REPORT_MD)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    args = parser.parse_args(argv)

    report = run(
        db_path=args.db,
        promotion_report_json=args.promotion_report_json,
        report_json=args.report_json,
        report_md=args.report_md,
        dry_run=not args.write,
        write=args.write,
        limit=args.limit,
        model=args.model,
        temperature=args.temperature,
    )
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
