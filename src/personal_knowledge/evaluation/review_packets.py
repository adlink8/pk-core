"""Private review packets, strict imports, and calibration status.

Tracked code and schemas contain no private payloads. Packets, labels and imports
live under ``var/runtime/private_evals`` and only metadata/checksums are printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB, ROOT, UNIFIED_DB
from personal_knowledge.retrieval.evidence import EvidenceResolver


PRIVATE_DIR = ROOT / "var" / "runtime" / "private_evals"
GOLD_PACKET = PRIVATE_DIR / "human_gold_review_v1.private.json"
GOLD_LABEL_TEMPLATE = PRIVATE_DIR / "human_gold_review_v1.labels.private.json"
GOLD_IMPORT = PRIVATE_DIR / "human_gold_v1.private.jsonl"
GOLD_MANIFEST = PRIVATE_DIR / "human_gold_v1.manifest.json"
GROUNDED_SOURCE = PRIVATE_DIR / "grounded_l2_review_v1.jsonl"
GROUNDED_PACKET = PRIVATE_DIR / "grounded_l2_review_packet_v1.private.json"
GROUNDED_LABEL_TEMPLATE = PRIVATE_DIR / "grounded_l2_review_v1.labels.private.json"
GROUNDED_IMPORT = PRIVATE_DIR / "grounded_l2_labels_v1.private.jsonl"
GROUNDED_MANIFEST = PRIVATE_DIR / "grounded_l2_labels_v1.manifest.json"
JUDGE_PACKET = PRIVATE_DIR / "judge_calibration_packet_v1.private.json"
JUDGE_LABEL_TEMPLATE = PRIVATE_DIR / "judge_calibration_v1.labels.private.json"
JUDGE_IMPORT = PRIVATE_DIR / "judge_calibration_v1.jsonl"
JUDGE_REPORT = PRIVATE_DIR / "judge_calibration_v1.report.json"

MODES = ("raw", "l1", "l2_only", "l1_l2", "hybrid")
_AGENT_RE = re.compile(r"agent|codex|gpt|claude|gemini|synthetic|auto", re.I)
_REVIEWER_TYPES = {"human", "llm"}
_NON_USER_PAYLOAD_RE = re.compile(
    r"^\s*(?:<system-reminder\b|<local-command-(?:caveat|stdout)\b|<teammate-message\b|"
    r"<cb_summary\b|#\s*AGENTS\.md instructions\b|#\s*Browser comments\b|\[image unavailable\]|"
    r"\[SYSTEM NOTIFICATION - NOT USER INPUT\]|"
    r"\[Assistant Rules\b|Warning: apply_patch was requested via shell\.|"
    r"Please continue with the conversation based on the summarized context above\.|"
    r"The TodoWrite tool hasn't been used recently\.|"
    r"This session was forked from a previous session message\.|"
    r"Your task is to write a detailed and structured summary\b)",
    re.I,
)


class ReviewError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checksum(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ReviewError(f"missing private artifact: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError("review artifact must be a JSON object")
    return value


def build_packet(packet_type: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    body_checksum = checksum(rows)
    return {
        "schema_version": "review_packet_v1",
        "packet_type": packet_type,
        "packet_id": f"{packet_type}_{body_checksum[:16]}",
        "source_checksum": body_checksum,
        "rows": rows,
    }


def _review_identity(
    labels: Mapping[str, Any], packet: Mapping[str, Any]
) -> tuple[str, str, dict[str, str]]:
    if labels.get("packet_id") != packet.get("packet_id"):
        raise ReviewError("packet_id mismatch")
    if labels.get("source_checksum") != packet.get("source_checksum"):
        raise ReviewError("source_checksum mismatch")
    reviewer = str(labels.get("reviewer_id") or "").strip()
    reviewed_at = str(labels.get("reviewed_at") or "").strip()
    reviewer_type = str(labels.get("reviewer_type") or "human").strip().lower()
    if reviewer_type not in _REVIEWER_TYPES:
        raise ReviewError("reviewer_type must be human or llm")
    if len(reviewer) < 3:
        raise ReviewError("reviewer_id must be at least 3 characters")
    if reviewer_type == "human" and _AGENT_RE.search(reviewer):
        raise ReviewError("human reviewer_id cannot identify an agent or model")
    try:
        datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewError("reviewed_at must be ISO-8601") from exc
    provenance = {
        "reviewer_type": reviewer_type,
        "model_id": str(labels.get("model_id") or "").strip(),
        "review_run_id": str(labels.get("review_run_id") or "").strip(),
        "prompt_version": str(labels.get("prompt_version") or "").strip(),
    }
    if reviewer_type == "llm" and not all(provenance[key] for key in ("model_id", "review_run_id", "prompt_version")):
        raise ReviewError("llm review requires model_id, review_run_id and prompt_version")
    return reviewer, reviewed_at, provenance


def _validate_llm_item(item: Mapping[str, Any], provenance: Mapping[str, str]) -> None:
    if provenance.get("reviewer_type") != "llm":
        return
    confidence = item.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise ReviewError("llm review item confidence must be between 0 and 1")


def _validate_rating(item: Mapping[str, Any]) -> None:
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 1 <= float(score) <= 5:
        raise ReviewError("judge score must be between 1 and 5")
    if not isinstance(item.get("pass"), bool) or not isinstance(item.get("privacy_violation"), bool):
        raise ReviewError("judge pass and privacy_violation must be boolean")


def _provenance_fields(reviewer: str, reviewed_at: str, provenance: Mapping[str, str]) -> dict[str, Any]:
    return {
        "reviewer_id_hash": hashlib.sha256(reviewer.encode()).hexdigest(),
        "reviewed_at": reviewed_at,
        "reviewer_type": provenance["reviewer_type"],
        "model_id": provenance.get("model_id") or None,
        "review_run_id": provenance.get("review_run_id") or None,
        "prompt_version": provenance.get("prompt_version") or None,
    }


def _resolved_eligible(refs: Iterable[str], resolver: EvidenceResolver) -> bool:
    refs = [str(ref) for ref in refs if str(ref)]
    if not refs:
        return False
    return all(
        resolver.resolve(ref, artifact_type="canonical_message").get("status") == "ok"
        for ref in refs
    )


def _is_reviewable_user_text(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) >= 3 and not _NON_USER_PAYLOAD_RE.search(text)


def _review_tokens(value: Any) -> set[str]:
    text = str(value or "").lower()
    latin = set(re.findall(r"[a-z0-9_]{2,}", text))
    han = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    return latin | {han[index:index + 2] for index in range(max(0, len(han) - 1))}


def _normalized_review_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _pair_score(left: Any, right: Any) -> float:
    a, b = _review_tokens(left), _review_tokens(right)
    if not a or not b:
        return 0.0
    overlap = a & b
    return len(overlap) / len(a | b) + min(len(overlap), 8) * 0.01


def import_gold(
    packet_path: Path,
    labels_path: Path,
    *,
    out_path: Path = GOLD_IMPORT,
    manifest_path: Path = GOLD_MANIFEST,
    resolver: EvidenceResolver | None = None,
) -> dict[str, Any]:
    packet, labels = _read_json(packet_path), _read_json(labels_path)
    if packet.get("packet_type") != "gold":
        raise ReviewError("not a gold packet")
    if checksum(packet.get("rows") or []) != packet.get("source_checksum"):
        raise ReviewError("packet payload checksum mismatch")
    reviewer, reviewed_at, provenance = _review_identity(labels, packet)
    by_id = {str(row.get("case_id")): row for row in packet.get("rows") or []}
    imported: list[dict[str, Any]] = []
    resolver = resolver or EvidenceResolver()
    seen: set[str] = set()
    for label in labels.get("labels") or []:
        case_id = str(label.get("case_id") or "")
        if case_id in seen or case_id not in by_id:
            raise ReviewError("unknown or duplicate case_id")
        seen.add(case_id)
        decision = str(label.get("decision") or "")
        _validate_llm_item(label, provenance)
        if decision not in {"accept", "reject"}:
            raise ReviewError("gold decision must be accept/reject")
        if decision == "reject":
            continue
        row = dict(by_id[case_id])
        reviewed_query = str(label.get("query") or row.get("query") or "").strip()
        if len(reviewed_query) < 3:
            raise ReviewError("accepted gold requires a reviewed query")
        row["query"] = reviewed_query
        row.pop("evidence_excerpts", None)
        refs = list(row.get("gold_evidence_refs") or [])
        if not _resolved_eligible(refs, resolver):
            raise ReviewError("accepted gold has unresolved or ineligible evidence")
        if str(row.get("split") or "").startswith("synthetic"):
            raise ReviewError("synthetic row cannot be imported as real gold")
        row.update(
            {
                "gold_provenance": f"{provenance['reviewer_type']}_reviewed_v1",
                "reviewer_id": reviewer,
                "reviewed_at": reviewed_at,
                **provenance,
                "review_packet_id": packet["packet_id"],
                "review_source_checksum": packet["source_checksum"],
            }
        )
        imported.append(row)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in imported),
        encoding="utf-8",
    )
    manifest = {
        "kind": "reviewed_gold",
        "count": len(imported),
        "cross_turn_count": sum(bool(row.get("requires_cross_turn")) for row in imported),
        **_provenance_fields(reviewer, reviewed_at, provenance),
        "packet_id": packet["packet_id"],
        "source_checksum": packet["source_checksum"],
        "import_checksum": checksum(imported),
    }
    _write_json(manifest_path, manifest)
    return manifest


def import_grounded(
    packet_path: Path,
    labels_path: Path,
    *,
    out_path: Path = GROUNDED_IMPORT,
    manifest_path: Path = GROUNDED_MANIFEST,
    resolver: EvidenceResolver | None = None,
) -> dict[str, Any]:
    packet, labels = _read_json(packet_path), _read_json(labels_path)
    if packet.get("packet_type") != "grounded_l2":
        raise ReviewError("not a grounded L2 packet")
    if checksum(packet.get("rows") or []) != packet.get("source_checksum"):
        raise ReviewError("packet payload checksum mismatch")
    reviewer, reviewed_at, provenance = _review_identity(labels, packet)
    by_id = {str(row.get("unit_id")): row for row in packet.get("rows") or []}
    resolver = resolver or EvidenceResolver()
    imported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label in labels.get("labels") or []:
        unit_id = str(label.get("unit_id") or "")
        if unit_id in seen or unit_id not in by_id:
            raise ReviewError("unknown or duplicate unit_id")
        seen.add(unit_id)
        grounded = label.get("grounded")
        _validate_llm_item(label, provenance)
        if grounded not in {True, False, "uncertain"}:
            raise ReviewError("grounded must be true/false/uncertain")
        source_ref = str(by_id[unit_id].get("source_message_ref") or "")
        if not _resolved_eligible([source_ref], resolver):
            raise ReviewError("grounded label source is unresolved or ineligible")
        imported.append(
            {
                "unit_id": unit_id,
                "grounded": grounded,
                "reviewer_notes": str(label.get("reviewer_notes") or ""),
                "reviewer_id": reviewer,
                "reviewed_at": reviewed_at,
                **provenance,
                "source_checksum": packet["source_checksum"],
            }
        )
    out_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in imported),
        encoding="utf-8",
    )
    manifest = {
        "kind": "grounded_l2",
        "count": len(imported),
        **_provenance_fields(reviewer, reviewed_at, provenance),
        "packet_id": packet["packet_id"],
        "source_checksum": packet["source_checksum"],
        "import_checksum": checksum(imported),
    }
    _write_json(manifest_path, manifest)
    return manifest


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2 + 1
        for index in order[i:j]:
            ranks[index] = rank
        i = j
    return ranks


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    lx, rx = _ranks(left), _ranks(right)
    lm, rm = sum(lx) / len(lx), sum(rx) / len(rx)
    num = sum((a - lm) * (b - rm) for a, b in zip(lx, rx))
    den = (sum((a - lm) ** 2 for a in lx) * sum((b - rm) ** 2 for b in rx)) ** 0.5
    return num / den if den else None


def _kappa(left: list[bool], right: list[bool]) -> float | None:
    if not left or len(left) != len(right):
        return None
    n = len(left)
    observed = sum(a == b for a, b in zip(left, right)) / n
    lp, rp = sum(left) / n, sum(right) / n
    expected = lp * rp + (1 - lp) * (1 - rp)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def calibrate_judge(
    packet_path: Path,
    human_path: Path,
    judge_cache_path: Path,
    *,
    report_path: Path = JUDGE_REPORT,
    allow_network_judge: bool = False,
    paid_authorization: str = "",
) -> dict[str, Any]:
    if allow_network_judge and paid_authorization != "I_AUTHORIZE_PAID_JUDGE":
        raise ReviewError("network judge requires separate explicit paid authorization")
    if allow_network_judge:
        raise ReviewError("network judge provider is intentionally not configured; use reviewed cache")
    packet, primary, judge = _read_json(packet_path), _read_json(human_path), _read_json(judge_cache_path)
    reviewer, reviewed_at, provenance = _review_identity(primary, packet)
    judge_provenance: dict[str, str] = {}
    if provenance["reviewer_type"] == "llm":
        _, _, judge_provenance = _review_identity(judge, packet)
        if judge_provenance["reviewer_type"] != "llm":
            raise ReviewError("llm calibration requires an llm judge cache")
        if judge_provenance["review_run_id"] == provenance["review_run_id"]:
            raise ReviewError("llm calibration requires two distinct review_run_id values")
    expected = {
        (str(row.get("case_id")), str(row.get("mode")))
        for row in packet.get("rows") or []
    }
    human_map = {(str(x.get("case_id")), str(x.get("mode"))): x for x in primary.get("ratings") or []}
    judge_map = {(str(x.get("case_id")), str(x.get("mode"))): x for x in judge.get("ratings") or []}
    if len(expected) < 150 or set(human_map) != expected or set(judge_map) != expected:
        raise ReviewError("judge calibration requires complete 30x5 primary and cached judge ratings")
    hs, js, hp, jp = [], [], [], []
    privacy_disagreement = 0
    for key in sorted(expected):
        h, j = human_map[key], judge_map[key]
        _validate_llm_item(h, provenance)
        _validate_llm_item(j, judge_provenance)
        _validate_rating(h)
        _validate_rating(j)
        hs.append(float(h["score"]))
        js.append(float(j["score"]))
        hp.append(bool(h["pass"]))
        jp.append(bool(j["pass"]))
        privacy_disagreement += bool(h.get("privacy_violation")) != bool(j.get("privacy_violation"))
    rho, kappa = _correlation(hs, js), _kappa(hp, jp)
    passed = bool((rho is not None and rho >= 0.70) or (kappa is not None and kappa >= 0.70)) and privacy_disagreement == 0
    report = {
        "kind": "judge_calibration",
        "count": len(expected),
        "case_count": len({case for case, _ in expected}),
        "mode_count": len({mode for _, mode in expected}),
        "spearman_rho": rho,
        "cohen_kappa": kappa,
        "privacy_disagreement": privacy_disagreement,
        "judge_gate_enabled": passed,
        **_provenance_fields(reviewer, reviewed_at, provenance),
        "primary_review_checksum": checksum(primary.get("ratings") or []),
        "human_checksum": checksum(primary.get("ratings") or []),
        "judge_cache_checksum": checksum(judge.get("ratings") or []),
        "judge_reviewer_type": judge_provenance.get("reviewer_type") or "cached",
        "judge_model_id": judge_provenance.get("model_id") or None,
        "judge_review_run_id": judge_provenance.get("review_run_id") or None,
        "judge_prompt_version": judge_provenance.get("prompt_version") or None,
        "network_used": False,
    }
    _write_json(report_path, report)
    return report


def prepare_grounded() -> dict[str, Any]:
    resolver = EvidenceResolver()
    source_rows = [
        json.loads(line)
        for line in GROUNDED_SOURCE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if GROUNDED_SOURCE.exists() else []
    rows = [
        row for row in source_rows
        if resolver.resolve(str(row.get("source_message_ref") or ""), artifact_type="canonical_message").get("status") == "ok"
    ]
    seen = {str(row.get("unit_id") or "") for row in rows}
    if len(rows) < 50:
        con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            candidates = con.execute(
                "SELECT unit_id,subject,question,answer,evidence_quote,source_message_ref "
                "FROM knowledge_units WHERE unit_id LIKE 'l2|%' "
                "AND COALESCE(source_message_ref,'')<>'' AND COALESCE(evidence_quote,'')<>'' "
                "AND status='current' ORDER BY unit_id"
            ).fetchall()
        finally:
            con.close()
        for candidate in candidates:
            row = dict(candidate)
            if row["unit_id"] in seen:
                continue
            if resolver.resolve(row["source_message_ref"], artifact_type="canonical_message").get("status") != "ok":
                continue
            row.update({"grounded": None, "reviewer_notes": ""})
            rows.append(row)
            seen.add(row["unit_id"])
            if len(rows) >= 50:
                break
    if len(rows) < 50:
        raise ReviewError(f"fewer than 50 eligible grounded L2 candidates: {len(rows)}")
    rows = rows[:50]
    packet = build_packet("grounded_l2", rows)
    _write_json(GROUNDED_PACKET, packet)
    _write_json(
        GROUNDED_LABEL_TEMPLATE,
        {
            "packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"],
            "reviewer_type": "human", "reviewer_id": "", "reviewed_at": "",
            "labels": [{"unit_id": row["unit_id"], "grounded": None, "reviewer_notes": ""} for row in rows],
        },
    )
    return {"packet_id": packet["packet_id"], "count": len(rows), "source_checksum": packet["source_checksum"]}


def prepare_gold() -> dict[str, Any]:
    """Build private real-evidence candidates; it does not assign labels."""
    unit_con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
    ref_to_units: dict[str, set[str]] = {}
    try:
        for ref, unit_id in unit_con.execute(
            "SELECT DISTINCT u.source_message_ref,m.canonical_unit_id "
            "FROM knowledge_units u JOIN canonical_unit_members m ON m.member_unit_id=u.unit_id "
            "WHERE COALESCE(u.source_message_ref,'')<>'' AND u.status='current'"
        ):
            ref_to_units.setdefault(str(ref), set()).add(str(unit_id))
    finally:
        unit_con.close()
    con = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT m.canonical_session_id,m.canonical_message_id,m.content,m.ordinal "
        "FROM canonical_messages m JOIN canonical_sessions s "
        "ON s.canonical_session_id=m.canonical_session_id "
        "WHERE COALESCE(s.evidence_eligible,0)=1 "
        "AND LOWER(COALESCE(s.evidence_scope,'user'))='user' "
        "AND LOWER(COALESCE(m.evidence_scope,'user'))='user' "
        "AND COALESCE(m.is_system,0)=0 AND m.role='user' "
        "ORDER BY m.canonical_session_id,m.ordinal"
    ).fetchall()
    con.close()
    candidates: list[dict[str, Any]] = []
    by_session: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_session.setdefault(str(row["canonical_session_id"]), []).append(row)
    for session_id, messages in sorted(by_session.items()):
        messages = [
            row for row in messages
            if _is_reviewable_user_text(row["content"])
            and str(row["canonical_message_id"]) in ref_to_units
        ]
        if len(messages) < 2:
            continue
        pairs = [
            (_pair_score(left["content"], right["content"]), left, right)
            for index, left in enumerate(messages[:-1])
            for right in messages[index + 1:]
            if _normalized_review_text(left["content"]) != _normalized_review_text(right["content"])
            and SequenceMatcher(
                None,
                _normalized_review_text(left["content"])[:2000],
                _normalized_review_text(right["content"])[:2000],
            ).ratio() < 0.75
        ]
        if not pairs:
            continue
        score, first, second = max(pairs, key=lambda item: (item[0], int(item[2]["ordinal"])))
        refs = [str(first["canonical_message_id"]), str(second["canonical_message_id"])]
        unit_ids = sorted(ref_to_units[refs[0]] | ref_to_units[refs[1]])
        digest = hashlib.sha256((session_id + "|" + "|".join(refs)).encode()).hexdigest()[:16]
        candidates.append(
            {
                "case_id": f"human-cross-{digest}",
                "id": f"human-cross-{digest}",
                "query": "",
                "split": "human_review_candidate",
                "scenario": "cross_turn",
                "requires_cross_turn": True,
                "gold_unit_ids": unit_ids,
                "gold_evidence_refs": refs,
                "candidate_pair_score": round(score, 6),
                "evidence_excerpts": [str(first["content"] or "")[:500], str(second["content"] or "")[:500]],
            }
        )
    candidates.sort(key=lambda row: (-float(row["candidate_pair_score"]), row["case_id"]))
    candidates = candidates[:60]
    if len(candidates) < 30:
        raise ReviewError("fewer than 30 eligible multi-evidence candidates")
    packet = build_packet("gold", candidates)
    _write_json(GOLD_PACKET, packet)
    _write_json(
        GOLD_LABEL_TEMPLATE,
        {
            "packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"],
            "reviewer_type": "human", "reviewer_id": "", "reviewed_at": "",
            "labels": [{"case_id": row["case_id"], "decision": "pending", "query": ""} for row in candidates],
        },
    )
    return {"packet_id": packet["packet_id"], "count": len(candidates), "cross_turn_candidates": len(candidates), "source_checksum": packet["source_checksum"]}


def prepare_judge() -> dict[str, Any]:
    """Generate a blind 30x5 packet from deterministic, cache-keyed answers."""
    from personal_knowledge.evaluation.answer_eval import generate_answer
    from personal_knowledge.evaluation.eval_contracts import load_cases_jsonl
    from personal_knowledge.evaluation.run_knowledge_eval import (
        DEFAULT_CONFIG,
        load_config,
        resolve_cases_path,
        stage_retrieval,
    )

    cfg = load_config(DEFAULT_CONFIG)
    cases = load_cases_jsonl(resolve_cases_path(cfg))[:30]
    if len(cases) < 30:
        raise ReviewError("judge packet requires at least 30 cases")
    work = PRIVATE_DIR / "judge_calibration_work_v1"
    work.mkdir(parents=True, exist_ok=True)
    retrieval = stage_retrieval(cases, cfg, work, offline=False)
    rows: list[dict[str, Any]] = []
    for mode in MODES:
        ranked_cases = (retrieval.get("mode_ranked") or {}).get(mode) or []
        if len(ranked_cases) != len(cases):
            raise ReviewError(f"missing deterministic contexts for mode {mode}")
        for case, ranked in zip(cases, ranked_cases):
            answer = generate_answer(case.query, ranked, expected_abstain=case.expected_abstain)
            rows.append(
                {
                    "case_id": case.id,
                    "mode": mode,
                    "query": case.query,
                    "answer": answer.answer,
                    "cited_ids": answer.cited_ids,
                    "cache_key": answer.cache_key,
                    "expected_abstain": case.expected_abstain,
                }
            )
    packet = build_packet("judge_30x5", rows)
    _write_json(JUDGE_PACKET, packet)
    _write_json(
        JUDGE_LABEL_TEMPLATE,
        {
            "packet_id": packet["packet_id"], "source_checksum": packet["source_checksum"],
            "reviewer_type": "human", "reviewer_id": "", "reviewed_at": "",
            "ratings": [{"case_id": row["case_id"], "mode": row["mode"], "score": None, "pass": None, "privacy_violation": None} for row in rows],
        },
    )
    return {"packet_id": packet["packet_id"], "case_count": 30, "rating_count": len(rows), "source_checksum": packet["source_checksum"], "network_used": False}


def status() -> dict[str, Any]:
    def manifest(path: Path) -> dict[str, Any]:
        return _read_json(path) if path.exists() else {}
    gold, grounded, judge = manifest(GOLD_MANIFEST), manifest(GROUNDED_MANIFEST), manifest(JUDGE_REPORT)
    checks = {
        "additional_real_gold": int(gold.get("count") or 0) >= 8,
        "real_cross_turn_gold": int(gold.get("cross_turn_count") or 0) >= 30,
        "grounded_l2_labels": int(grounded.get("count") or 0) >= 50,
        "judge_calibration_30x5": bool(judge.get("judge_gate_enabled")) and int(judge.get("count") or 0) >= 150,
        "review_provenance_allowed": all(
            manifest.get("reviewer_type", "human") in _REVIEWER_TYPES
            for manifest in (gold, grounded, judge) if manifest
        ),
    }
    return {"ok": all(checks.values()), "checks": checks, "manifests": {"gold": gold, "grounded": grounded, "judge": judge}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare-grounded")
    sub.add_parser("prepare-gold")
    sub.add_parser("prepare-judge")
    imp_g = sub.add_parser("import-gold")
    imp_g.add_argument("--packet", type=Path, required=True)
    imp_g.add_argument("--labels", type=Path, required=True)
    imp_l2 = sub.add_parser("import-grounded")
    imp_l2.add_argument("--packet", type=Path, required=True)
    imp_l2.add_argument("--labels", type=Path, required=True)
    judge_cmd = sub.add_parser("calibrate-judge")
    judge_cmd.add_argument("--packet", type=Path, default=JUDGE_PACKET)
    judge_cmd.add_argument("--human", type=Path, required=True)
    judge_cmd.add_argument("--judge-cache", type=Path, required=True)
    judge_cmd.add_argument("--allow-network-judge", action="store_true")
    judge_cmd.add_argument("--paid-authorization", default="")
    stat = sub.add_parser("status")
    stat.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-grounded":
            result = prepare_grounded()
        elif args.command == "prepare-gold":
            result = prepare_gold()
        elif args.command == "prepare-judge":
            result = prepare_judge()
        elif args.command == "import-gold":
            result = import_gold(args.packet, args.labels)
        elif args.command == "import-grounded":
            result = import_grounded(args.packet, args.labels)
        elif args.command == "calibrate-judge":
            result = calibrate_judge(
                args.packet, args.human, args.judge_cache,
                allow_network_judge=args.allow_network_judge,
                paid_authorization=args.paid_authorization,
            )
        else:
            result = status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not (args.command == "status" and args.strict and not result["ok"]) else 1
    except ReviewError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
