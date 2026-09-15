"""Phase 10 Wave 1: build auditable memory-memory relation candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from personal_knowledge.core import llm as llm_mod
ROOT = Path(__file__).resolve().parents[4]
DB_PATH = ROOT / "integration" / "db" / "personal_system.sqlite"
PROMPT_DIR = ROOT / "assets" / "prompts" / "memory_relation_proposal"
REPORT_JSON = ROOT / "integration" / "analysis" / "ai_context" / "memory_relation_candidate_proposals_report.json"
REPORT_MD = ROOT / "integration" / "analysis" / "ai_context" / "memory_relation_candidate_proposals_report.md"

PROMPT_VERSION = "memory_relation_proposal/v1"
DEFAULT_MODEL = os.environ.get("MEM0_LLM_MODEL", "gpt-5.4")
DEFAULT_TEMPERATURE = 0.2

ALLOWED_RELATION_TYPES = {
    "same_subject",
    "related_topic",
    "enables",
    "uses_tool",
    "embodies",
    "conflicts_with",
    "refines",
    "supports",
    "no_relation",
}
ALLOWED_PROPOSAL_STATUS = {"proposed", "downgrade", "reject", "needs_human_review", "blocked"}
ALLOWED_CANDIDATE_TYPES = {"semantic_relation_candidate", "weak_memory_signal"}

PROPOSAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_relation_candidate_proposals (
    proposal_id TEXT PRIMARY KEY,
    candidate_id TEXT,
    package_id TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    proposed_relation_type TEXT NOT NULL,
    proposal_status TEXT NOT NULL,
    candidate_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    why_candidate TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    temperature REAL NOT NULL,
    llm_status TEXT NOT NULL,
    package_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mrcp_package_id ON memory_relation_candidate_proposals(package_id);
CREATE INDEX IF NOT EXISTS idx_mrcp_status ON memory_relation_candidate_proposals(proposal_status);
"""

CANDIDATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_relation_candidates (
    candidate_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    candidate_reason TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    allowed_refs_json TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL,
    llm_status TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mrc_source_target ON memory_relation_candidates(source_memory_id, target_memory_id);
CREATE INDEX IF NOT EXISTS idx_mrc_relation_type ON memory_relation_candidates(relation_type);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def stable_id(prefix: str, *parts: object) -> str:
    text = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]}"


def unique_strs(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def tokenize_subject(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", (text or "").lower())
    return [tok for tok in tokens if len(tok) >= 2]


def canonical_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def parse_json_object(raw: object) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def detect_llm_status() -> tuple[str, bool]:
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("MEM0_API_KEY")):
        return "fallback:no_api_key", False
    return "live_api_key_present", True


def extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def load_prompt_assets() -> tuple[str, str]:
    main_prompt = (PROMPT_DIR / "v1_main.md").read_text(encoding="utf-8").strip()
    schema_prompt = (PROMPT_DIR / "v1_schema.md").read_text(encoding="utf-8").strip()
    return main_prompt, schema_prompt


def extract_linked_refs(metadata: Any) -> list[str]:
    refs: list[str] = []

    def walk(node: Any, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, str(key))
            return
        if isinstance(node, list):
            for value in node:
                walk(value, key_hint)
            return
        text = str(node or "").strip()
        hint = key_hint.lower()
        if not text:
            return
        if "ref" in hint or "event" in hint or "link" in hint:
            refs.append(text)

    walk(metadata)
    return unique_strs(refs)


def load_memories(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = con.execute(
        """
        SELECT memory_id, memory_type, memory_subtype, subject, description,
               confidence, evidence_count, metadata, created_at
        FROM memory_items
        ORDER BY memory_id
        """
    ).fetchall()
    memories: dict[str, dict[str, Any]] = {}
    for row in rows:
        metadata = parse_json_object(row[7])
        linked_refs = extract_linked_refs(metadata)
        tokens = tokenize_subject(row[3])
        evidence_refs = unique_strs(
            [
                f"memory_id:{row[0]}",
                f"memory_field:{row[0]}:subject",
                f"memory_field:{row[0]}:description",
                *[f"linked_ref:{ref}" for ref in linked_refs],
            ]
        )
        memories[row[0]] = {
            "memory_id": row[0],
            "memory_type": row[1],
            "memory_subtype": row[2],
            "subject": row[3],
            "description": row[4],
            "confidence": float(row[5] or 0.0),
            "evidence_count": int(row[6] or 0),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "linked_refs": linked_refs,
            "subject_tokens": tokens,
            "field_refs": evidence_refs,
            "created_at": row[8],
        }
    return memories


def load_rule_relations(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT from_memory_id, to_memory_id, relation, strength
        FROM memory_relations
        ORDER BY from_memory_id, to_memory_id, relation
        """
    ).fetchall()
    return [
        {
            "from_memory_id": row[0],
            "to_memory_id": row[1],
            "relation": row[2],
            "strength": float(row[3] or 0.0),
        }
        for row in rows
    ]


def score_signal(signal: str, weight: float) -> float:
    base = {
        "existing_rule_relation": 1.0,
        "shared_linked_ref": 0.9,
        "same_type_subtype": 0.6,
        "subject_token_overlap": 0.5,
    }.get(signal, 0.3)
    return round(base + min(0.3, weight), 4)


def build_recall_signals(
    memories: dict[str, dict[str, Any]],
    rule_relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    memory_rows = list(memories.values())
    for idx, left in enumerate(memory_rows):
        for right in memory_rows[idx + 1 :]:
            shared_refs = sorted(set(left["linked_refs"]) & set(right["linked_refs"]))
            shared_tokens = sorted(set(left["subject_tokens"]) & set(right["subject_tokens"]))
            if left["memory_type"] == right["memory_type"] and left["memory_subtype"] == right["memory_subtype"]:
                signals.append(
                    {
                        "source_memory_id": left["memory_id"],
                        "target_memory_id": right["memory_id"],
                        "signal": "same_type_subtype",
                        "signal_reason": f"same_type_subtype:{left['memory_type']}/{left['memory_subtype']}",
                        "signal_score": score_signal("same_type_subtype", 0.0),
                    }
                )
            if shared_tokens:
                signals.append(
                    {
                        "source_memory_id": left["memory_id"],
                        "target_memory_id": right["memory_id"],
                        "signal": "subject_token_overlap",
                        "signal_reason": f"shared_subject_tokens:{','.join(shared_tokens[:4])}",
                        "signal_score": score_signal("subject_token_overlap", min(0.3, 0.05 * len(shared_tokens))),
                    }
                )
            if shared_refs:
                signals.append(
                    {
                        "source_memory_id": left["memory_id"],
                        "target_memory_id": right["memory_id"],
                        "signal": "shared_linked_ref",
                        "signal_reason": f"shared_linked_refs:{','.join(shared_refs[:3])}",
                        "signal_score": score_signal("shared_linked_ref", min(0.3, 0.05 * len(shared_refs))),
                    }
                )
    for relation in rule_relations:
        src = relation["from_memory_id"]
        tgt = relation["to_memory_id"]
        if src == tgt or src not in memories or tgt not in memories:
            continue
        left, right = canonical_pair(src, tgt)
        signals.append(
            {
                "source_memory_id": left,
                "target_memory_id": right,
                "signal": "existing_rule_relation",
                "signal_reason": f"rule_relation:{relation['relation']}",
                "signal_score": score_signal("existing_rule_relation", relation["strength"]),
            }
        )
    return signals


def merge_packages(
    memories: dict[str, dict[str, Any]],
    rule_relations: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    relation_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for relation in rule_relations:
        left, right = canonical_pair(relation["from_memory_id"], relation["to_memory_id"])
        relation_map.setdefault((left, right), []).append(relation)

    packages: dict[tuple[str, str], dict[str, Any]] = {}
    for signal in signals:
        left, right = canonical_pair(signal["source_memory_id"], signal["target_memory_id"])
        if left == right or left not in memories or right not in memories:
            continue
        package = packages.get((left, right))
        if not package:
            src = memories[left]
            tgt = memories[right]
            shared_tokens = sorted(set(src["subject_tokens"]) & set(tgt["subject_tokens"]))
            shared_linked_refs = sorted(set(src["linked_refs"]) & set(tgt["linked_refs"]))
            pair_relations = relation_map.get((left, right), [])
            allowed_refs = unique_strs(
                src["field_refs"]
                + tgt["field_refs"]
                + [f"linked_ref:{ref}" for ref in shared_linked_refs]
                + [f"rule_relation:{row['relation']}" for row in pair_relations]
            )
            package = {
                "package_id": stable_id("mrpkg", left, right),
                "source_memory_id": left,
                "target_memory_id": right,
                "source_memory": {
                    "memory_id": src["memory_id"],
                    "memory_type": src["memory_type"],
                    "memory_subtype": src["memory_subtype"],
                    "subject": src["subject"],
                    "description": src["description"],
                    "confidence": src["confidence"],
                    "evidence_count": src["evidence_count"],
                    "linked_refs": src["linked_refs"],
                },
                "target_memory": {
                    "memory_id": tgt["memory_id"],
                    "memory_type": tgt["memory_type"],
                    "memory_subtype": tgt["memory_subtype"],
                    "subject": tgt["subject"],
                    "description": tgt["description"],
                    "confidence": tgt["confidence"],
                    "evidence_count": tgt["evidence_count"],
                    "linked_refs": tgt["linked_refs"],
                },
                "coarse_recall_signals": [],
                "signal_reasons": [],
                "signal_scores": {},
                "shared_tokens": shared_tokens,
                "shared_linked_refs": shared_linked_refs,
                "existing_rule_relations": [
                    {"relation": row["relation"], "strength": row["strength"]} for row in pair_relations
                ],
                "allowed_refs": allowed_refs,
            }
            packages[(left, right)] = package
        if signal["signal"] not in package["coarse_recall_signals"]:
            package["coarse_recall_signals"].append(signal["signal"])
        reason = str(signal["signal_reason"] or "").strip()
        if reason and reason not in package["signal_reasons"]:
            package["signal_reasons"].append(reason)
        package["signal_scores"][signal["signal"]] = max(
            float(package["signal_scores"].get(signal["signal"], 0.0)),
            float(signal["signal_score"] or 0.0),
        )

    ordered = sorted(
        packages.values(),
        key=lambda row: (
            -len(row["coarse_recall_signals"]),
            -max(row["signal_scores"].values() or [0.0]),
            row["package_id"],
        ),
    )
    if limit > 0:
        ordered = ordered[:limit]
    return ordered


def build_llm_messages(package: dict[str, Any], main_prompt: str, schema_text: str) -> list[dict[str, str]]:
    system_prompt = (
        f"{main_prompt}\n\n"
        "下面是输出 schema 约束。你必须严格遵守，并且只能引用 package 内的 allowed_refs。\n\n"
        f"{schema_text}"
    )
    package_payload = {
        "package_id": package["package_id"],
        "prompt_version": PROMPT_VERSION,
        "llm_status": "live_api_key_present",
        "coarse_recall_signals": package["coarse_recall_signals"],
        "signal_reasons": package["signal_reasons"],
        "pair": {
            "source_memory_id": package["source_memory_id"],
            "target_memory_id": package["target_memory_id"],
            "source_memory": package["source_memory"],
            "target_memory": package["target_memory"],
            "shared_tokens": package["shared_tokens"],
            "shared_linked_refs": package["shared_linked_refs"],
            "existing_rule_relations": package["existing_rule_relations"],
            "allowed_refs": package["allowed_refs"],
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": "请基于下面的 memory pair package 输出 JSON。只能输出 JSON。\n\n"
            + json.dumps(package_payload, ensure_ascii=False, indent=2),
        },
    ]


def make_proposal_id(package_id: str, source_memory_id: str, target_memory_id: str, relation_type: str, status: str) -> str:
    return stable_id("mrprop", package_id, source_memory_id, target_memory_id, relation_type, status)


def build_audit_row(
    *,
    package: dict[str, Any],
    source_memory_id: str,
    target_memory_id: str,
    relation_type: str,
    proposal_status: str,
    candidate_type: str,
    confidence: float,
    why_candidate: str,
    evidence_refs: list[str],
    source_refs: list[str],
    risk_flags: list[str],
    model: str,
    temperature: float,
    llm_status: str,
    prompt_version: str = PROMPT_VERSION,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    cid = candidate_id or stable_id("mrcand", package["package_id"], source_memory_id, target_memory_id, relation_type)
    return {
        "proposal_id": make_proposal_id(package["package_id"], source_memory_id, target_memory_id, relation_type, proposal_status),
        "candidate_id": cid,
        "package_id": package["package_id"],
        "source_memory_id": source_memory_id,
        "target_memory_id": target_memory_id,
        "proposed_relation_type": relation_type,
        "proposal_status": proposal_status,
        "candidate_type": candidate_type,
        "confidence": round(max(0.0, min(1.0, float(confidence or 0.0))), 4),
        "why_candidate": normalize_text(why_candidate),
        "evidence_refs_json": json.dumps(unique_strs(evidence_refs), ensure_ascii=False),
        "source_refs_json": json.dumps(unique_strs(source_refs), ensure_ascii=False),
        "risk_flags_json": json.dumps(unique_strs(risk_flags), ensure_ascii=False),
        "model": model,
        "prompt_version": prompt_version,
        "temperature": temperature,
        "llm_status": llm_status,
        "package_json": json.dumps(package, ensure_ascii=False),
        "created_at": now_iso(),
    }


def blocked_row_for_package(package: dict[str, Any], llm_status: str, model: str, temperature: float, reason: str) -> dict[str, Any]:
    return build_audit_row(
        package=package,
        source_memory_id=package["source_memory_id"],
        target_memory_id=package["target_memory_id"],
        relation_type="no_relation",
        proposal_status="blocked",
        candidate_type="weak_memory_signal",
        confidence=0.0,
        why_candidate=reason,
        evidence_refs=[],
        source_refs=package["allowed_refs"],
        risk_flags=["no_llm_proposal", llm_status.replace(":", "_")],
        model=model,
        temperature=temperature,
        llm_status=llm_status,
    )


def rejection_row_for_package(
    package: dict[str, Any],
    llm_status: str,
    model: str,
    temperature: float,
    reason: str,
    flag: str,
) -> dict[str, Any]:
    return build_audit_row(
        package=package,
        source_memory_id=package["source_memory_id"],
        target_memory_id=package["target_memory_id"],
        relation_type="no_relation",
        proposal_status="reject",
        candidate_type="weak_memory_signal",
        confidence=0.0,
        why_candidate=reason,
        evidence_refs=[],
        source_refs=package["allowed_refs"],
        risk_flags=[flag],
        model=model,
        temperature=temperature,
        llm_status=llm_status,
    )


def validate_proposal_fields(proposal: dict[str, Any], package: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not isinstance(proposal, dict):
        return None, "proposal is not an object", None

    candidate_type = str(proposal.get("candidate_type") or "").strip()
    source_memory_id = str(proposal.get("source_memory_id") or "").strip()
    target_memory_id = str(proposal.get("target_memory_id") or "").strip()
    relation_type = str(proposal.get("proposed_relation_type") or "").strip()
    proposal_status = str(proposal.get("proposal_status") or "").strip()
    why_candidate = normalize_text(proposal.get("why_candidate"))
    candidate_id = str(proposal.get("candidate_id") or "").strip() or None
    if not all([candidate_type, source_memory_id, target_memory_id, relation_type, proposal_status, why_candidate]):
        return None, "missing required proposal fields", None
    if candidate_type not in ALLOWED_CANDIDATE_TYPES:
        return None, f"invalid candidate_type={candidate_type}", None
    if relation_type not in ALLOWED_RELATION_TYPES:
        return None, f"invalid proposed_relation_type={relation_type}", None
    if proposal_status not in ALLOWED_PROPOSAL_STATUS - {"blocked"}:
        return None, f"invalid proposal_status={proposal_status}", None
    if source_memory_id == target_memory_id:
        return None, "self_loop_relation", None
    expected_ids = {package["source_memory_id"], package["target_memory_id"]}
    if {source_memory_id, target_memory_id} != expected_ids:
        return None, "proposal pair is outside package", None

    try:
        confidence = float(proposal.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None, "invalid confidence", None
    confidence = round(max(0.0, min(1.0, confidence)), 4)
    evidence_refs = unique_strs(proposal.get("evidence_refs") or [])
    source_refs = unique_strs(proposal.get("source_refs") or [])
    risk_flags = unique_strs(proposal.get("risk_flags") or [])
    needs_human_review = bool(proposal.get("needs_human_review", False))

    if proposal_status == "proposed":
        if relation_type == "no_relation":
            return None, "proposed proposal cannot use no_relation", None
        if confidence <= 0.0:
            return None, "proposed proposal requires positive confidence", None
        if not evidence_refs or not source_refs:
            return None, "proposed proposal requires evidence_refs and source_refs", None
    if needs_human_review and proposal_status not in {"needs_human_review", "downgrade"}:
        return None, "needs_human_review=true requires downgrade or needs_human_review", None

    allowed_refs = set(package["allowed_refs"])
    if any(ref not in allowed_refs for ref in evidence_refs):
        return None, None, "evidence_refs contain values outside package"
    if any(ref not in allowed_refs for ref in source_refs):
        return None, None, "source_refs contain values outside package"

    return (
        {
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
            "source_memory_id": source_memory_id,
            "target_memory_id": target_memory_id,
            "relation_type": relation_type,
            "proposal_status": proposal_status,
            "confidence": confidence,
            "why_candidate": why_candidate,
            "evidence_refs": evidence_refs,
            "source_refs": source_refs,
            "risk_flags": risk_flags,
            "needs_human_review": needs_human_review,
        },
        None,
        None,
    )


def normalize_llm_response(
    parsed: dict | None,
    package: dict[str, Any],
    llm_status: str,
    model: str,
    temperature: float,
) -> tuple[list[dict[str, Any]], int, int]:
    if not parsed:
        return [rejection_row_for_package(package, llm_status, model, temperature, "LLM 输出无法解析为 JSON", "schema_invalid")], 1, 0
    if str(parsed.get("package_id") or "").strip() != package["package_id"]:
        return [rejection_row_for_package(package, llm_status, model, temperature, "package_id 不匹配", "schema_invalid")], 1, 0
    proposals = parsed.get("candidate_proposals")
    if not isinstance(proposals, list) or not proposals:
        return [rejection_row_for_package(package, llm_status, model, temperature, "candidate_proposals 为空", "schema_invalid")], 1, 0

    rows: list[dict[str, Any]] = []
    schema_rejected = 0
    evidence_rejected = 0
    for proposal in proposals:
        normalized, schema_error, evidence_error = validate_proposal_fields(proposal, package)
        if schema_error:
            schema_rejected += 1
            rows.append(
                rejection_row_for_package(
                    package,
                    llm_status,
                    model,
                    temperature,
                    f"schema gate rejected: {schema_error}",
                    "schema_invalid",
                )
            )
            continue
        if evidence_error:
            evidence_rejected += 1
            rows.append(
                rejection_row_for_package(
                    package,
                    llm_status,
                    model,
                    temperature,
                    f"evidence gate rejected: {evidence_error}",
                    "evidence_invalid",
                )
            )
            continue
        assert normalized is not None
        rows.append(
            build_audit_row(
                package=package,
                source_memory_id=normalized["source_memory_id"],
                target_memory_id=normalized["target_memory_id"],
                relation_type=normalized["relation_type"],
                proposal_status=normalized["proposal_status"],
                candidate_type=normalized["candidate_type"],
                confidence=normalized["confidence"],
                why_candidate=normalized["why_candidate"],
                evidence_refs=normalized["evidence_refs"],
                source_refs=normalized["source_refs"],
                risk_flags=normalized["risk_flags"],
                model=model,
                temperature=temperature,
                llm_status=llm_status,
                candidate_id=normalized["candidate_id"],
                prompt_version=str(parsed.get("prompt_version") or PROMPT_VERSION).strip() or PROMPT_VERSION,
            )
        )
    return rows, schema_rejected, evidence_rejected


def candidate_row_from_proposal(proposal_row: dict[str, Any], package_by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if proposal_row["proposal_status"] != "proposed":
        return None
    if proposal_row["proposed_relation_type"] == "no_relation":
        return None
    evidence_refs = unique_strs(parse_json_object(proposal_row["evidence_refs_json"]))
    source_refs = unique_strs(parse_json_object(proposal_row["source_refs_json"]))
    risk_flags = unique_strs(parse_json_object(proposal_row["risk_flags_json"]))
    package = package_by_id.get(proposal_row["package_id"])
    if not package or not evidence_refs or not source_refs:
        return None
    return {
        "candidate_id": proposal_row["candidate_id"],
        "package_id": proposal_row["package_id"],
        "source_memory_id": proposal_row["source_memory_id"],
        "target_memory_id": proposal_row["target_memory_id"],
        "relation_type": proposal_row["proposed_relation_type"],
        "confidence": float(proposal_row["confidence"] or 0.0),
        "candidate_reason": proposal_row["why_candidate"],
        "evidence_refs_json": json.dumps(evidence_refs, ensure_ascii=False),
        "source_refs_json": json.dumps(source_refs, ensure_ascii=False),
        "allowed_refs_json": json.dumps(package["allowed_refs"], ensure_ascii=False),
        "risk_flags_json": json.dumps(risk_flags, ensure_ascii=False),
        "llm_status": proposal_row["llm_status"],
        "model": proposal_row["model"],
        "prompt_version": proposal_row["prompt_version"],
        "created_at": now_iso(),
    }


def write_proposals(rows: list[dict[str, Any]], db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as con:
        con.executescript(PROPOSAL_SCHEMA_SQL)
        con.executemany(
            """
            INSERT OR REPLACE INTO memory_relation_candidate_proposals (
                proposal_id, candidate_id, package_id, source_memory_id, target_memory_id,
                proposed_relation_type, proposal_status, candidate_type, confidence,
                why_candidate, evidence_refs_json, source_refs_json, risk_flags_json,
                model, prompt_version, temperature, llm_status, package_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row["proposal_id"],
                    row["candidate_id"],
                    row["package_id"],
                    row["source_memory_id"],
                    row["target_memory_id"],
                    row["proposed_relation_type"],
                    row["proposal_status"],
                    row["candidate_type"],
                    row["confidence"],
                    row["why_candidate"],
                    row["evidence_refs_json"],
                    row["source_refs_json"],
                    row["risk_flags_json"],
                    row["model"],
                    row["prompt_version"],
                    row["temperature"],
                    row["llm_status"],
                    row["package_json"],
                    row["created_at"],
                )
                for row in rows
            ],
        )
        con.commit()


def write_candidates(rows: list[dict[str, Any]], db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as con:
        con.executescript(CANDIDATE_SCHEMA_SQL)
        con.executemany(
            """
            INSERT OR REPLACE INTO memory_relation_candidates (
                candidate_id, package_id, source_memory_id, target_memory_id, relation_type,
                confidence, candidate_reason, evidence_refs_json, source_refs_json,
                allowed_refs_json, risk_flags_json, llm_status, model, prompt_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row["candidate_id"],
                    row["package_id"],
                    row["source_memory_id"],
                    row["target_memory_id"],
                    row["relation_type"],
                    row["confidence"],
                    row["candidate_reason"],
                    row["evidence_refs_json"],
                    row["source_refs_json"],
                    row["allowed_refs_json"],
                    row["risk_flags_json"],
                    row["llm_status"],
                    row["model"],
                    row["prompt_version"],
                    row["created_at"],
                )
                for row in rows
            ],
        )
        con.commit()


def build_report(
    *,
    packages: list[dict[str, Any]],
    proposal_rows: list[dict[str, Any]],
    written_candidates: int,
    schema_rejected: int,
    evidence_rejected: int,
    llm_status_counts: dict[str, int],
    limit: int,
    write: bool,
) -> dict[str, Any]:
    signal_counts: dict[str, int] = {}
    for package in packages:
        for signal in package["coarse_recall_signals"]:
            signal_counts[signal] = signal_counts.get(signal, 0) + 1
    proposal_status_counts: dict[str, int] = {}
    relation_type_counts: dict[str, int] = {}
    for row in proposal_rows:
        proposal_status_counts[row["proposal_status"]] = proposal_status_counts.get(row["proposal_status"], 0) + 1
        relation_type_counts[row["proposed_relation_type"]] = relation_type_counts.get(row["proposed_relation_type"], 0) + 1
    return {
        "prompt_version": PROMPT_VERSION,
        "mode": "write" if write else "dry-run",
        "limit": limit,
        "coarse_packages": len(packages),
        "coarse_signal_counts": dict(sorted(signal_counts.items())),
        "proposal_rows": len(proposal_rows),
        "proposal_status_counts": dict(sorted(proposal_status_counts.items())),
        "relation_type_counts": dict(sorted(relation_type_counts.items())),
        "schema_rejected": schema_rejected,
        "evidence_rejected": evidence_rejected,
        "written_candidates": written_candidates,
        "llm_status": dict(sorted(llm_status_counts.items())),
        "preview_packages": [
            {
                "package_id": package["package_id"],
                "source_memory_id": package["source_memory_id"],
                "target_memory_id": package["target_memory_id"],
                "signals": package["coarse_recall_signals"],
            }
            for package in packages[:10]
        ],
        "preview_proposals": [
            {
                "proposal_id": row["proposal_id"],
                "status": row["proposal_status"],
                "relation_type": row["proposed_relation_type"],
                "llm_status": row["llm_status"],
            }
            for row in proposal_rows[:10]
        ],
    }


def render_report_md(report: dict[str, Any]) -> str:
    lines = [
        "# Memory Relation Candidate Proposals Report",
        "",
        f"- mode: `{report['mode']}`",
        f"- prompt_version: `{report['prompt_version']}`",
        f"- coarse_packages: **{report['coarse_packages']}**",
        f"- proposal_rows: **{report['proposal_rows']}**",
        f"- written_candidates: **{report['written_candidates']}**",
        f"- schema_rejected: **{report['schema_rejected']}**",
        f"- evidence_rejected: **{report['evidence_rejected']}**",
        "",
        "## LLM Status",
        "",
    ]
    for key, value in report["llm_status"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Proposal Status", ""])
    for key, value in report["proposal_status_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Coarse Signals", ""])
    for key, value in report["coarse_signal_counts"].items():
        lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def run_pipeline(
    *,
    db_path: Path,
    dry_run: bool,
    write: bool,
    limit: int,
    model: str,
    temperature: float,
) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as con:
        memories = load_memories(con)
        rule_relations = load_rule_relations(con)

    signals = build_recall_signals(memories, rule_relations)
    packages = merge_packages(memories, rule_relations, signals, limit)
    package_by_id = {package["package_id"]: package for package in packages}

    main_prompt, schema_text = load_prompt_assets()
    llm_status, has_live_llm = detect_llm_status()
    llm_status_counts: dict[str, int] = {}
    proposal_rows: list[dict[str, Any]] = []
    schema_rejected = 0
    evidence_rejected = 0
    client = None
    if has_live_llm:
        try:
            client = llm_mod.make_llm_client()
        except SystemExit:
            llm_status = "blocked:no_live_llm"
            has_live_llm = False
        except Exception:
            llm_status = "blocked:no_live_llm"
            has_live_llm = False

    for idx, package in enumerate(packages, 1):
        current_status = llm_status
        if not has_live_llm or client is None:
            proposal_rows.append(
                blocked_row_for_package(
                    package,
                    current_status,
                    model,
                    temperature,
                    "无 live LLM/API key，当前仅记录 bounded recall package 审计，不生成 accepted candidate。",
                )
            )
            llm_status_counts[current_status] = llm_status_counts.get(current_status, 0) + 1
            continue
        try:
            raw = llm_mod._chat_with_retry(
                client,
                model,
                messages=build_llm_messages(package, main_prompt, schema_text),
                temperature=temperature,
            )
            parsed = extract_json(raw)
            rows, schema_count, evidence_count = normalize_llm_response(
                parsed,
                package,
                current_status,
                model,
                temperature,
            )
            proposal_rows.extend(rows)
            schema_rejected += schema_count
            evidence_rejected += evidence_count
            llm_status_counts[current_status] = llm_status_counts.get(current_status, 0) + 1
        except Exception as exc:
            current_status = "blocked:no_live_llm"
            proposal_rows.append(
                blocked_row_for_package(
                    package,
                    current_status,
                    model,
                    temperature,
                    f"LLM proposal 调用失败: {type(exc).__name__}: {str(exc)[:120]}",
                )
            )
            llm_status_counts[current_status] = llm_status_counts.get(current_status, 0) + 1
        if idx <= 5:
            print(
                f"[{idx}/{len(packages)}] package={package['package_id']} "
                f"signals={','.join(package['coarse_recall_signals'])} llm_status={current_status}",
                flush=True,
            )

    candidate_rows = [
        row
        for row in (candidate_row_from_proposal(proposal, package_by_id) for proposal in proposal_rows)
        if row is not None
    ]
    if write:
        write_proposals(proposal_rows, db_path)
        write_candidates(candidate_rows, db_path)

    report = build_report(
        packages=packages,
        proposal_rows=proposal_rows,
        written_candidates=len(candidate_rows),
        schema_rejected=schema_rejected,
        evidence_rejected=evidence_rejected,
        llm_status_counts=llm_status_counts or {llm_status: len(packages)},
        limit=limit,
        write=write,
    )
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(render_report_md(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 10 memory relation candidates.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    args = parser.parse_args(argv)
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2
    if not args.dry_run and not args.write:
        print("[error] 必须指定 --dry-run 或 --write", file=sys.stderr)
        return 2

    report = run_pipeline(
        db_path=args.db,
        dry_run=args.dry_run,
        write=args.write,
        limit=args.limit,
        model=args.model,
        temperature=args.temperature,
    )
    print("# Memory Relation Candidate Proposals")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
