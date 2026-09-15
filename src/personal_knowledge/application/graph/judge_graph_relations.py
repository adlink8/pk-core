"""Wave 9.2: LLM 判定 graph_relation_candidates 的真实关系。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from personal_knowledge.core import llm as llm_mod
from personal_knowledge.application.graph.build_graph_relation_candidates import SQLITE_DB, SUMMARIES_JSON, canonical_pair

ROOT = Path(__file__).resolve().parents[4]
PROMPT_DIR = ROOT / "assets" / "prompts" / "graph_relation_judge"
OUT_REPORT = ROOT / "integration" / "analysis" / "ai_context" / "graph_relation_judgments_report.json"
PROMPT_VERSION = "v1"
DEFAULT_LIMIT = 5
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_TEMPERATURE = 0.1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_relation_judgments (
    candidate_id TEXT PRIMARY KEY,
    relation_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    temperature REAL NOT NULL,
    created_at TEXT NOT NULL,
    gate_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_grj_gate_status ON graph_relation_judgments(gate_status);
CREATE INDEX IF NOT EXISTS idx_grj_relation_type ON graph_relation_judgments(relation_type);
"""

ALLOWED_RELATIONS = {
    "same_problem", "subproblem_of", "follow_up", "tool_used_for",
    "preference_signal", "contradiction", "temporal_next", "no_relation",
}


def load_prompt_block(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"## {re.escape(heading)}.*?```\n(.*?)```", text, re.DOTALL)
    if not m:
        raise ValueError(f"{path.name} 未找到 {heading} 代码块")
    return m.group(1).strip()


def load_schema_inline() -> str:
    text = (PROMPT_DIR / "v1_schema.md").read_text(encoding="utf-8")
    m = re.search(r"## JSON Schema.*?```json\n(.*?)```", text, re.DOTALL)
    if not m:
        raise ValueError("v1_schema.md 未找到 JSON Schema 代码块")
    return m.group(1).strip()


def load_prompts() -> tuple[str, str]:
    main = PROMPT_DIR / "v1_main.md"
    system_prompt = load_prompt_block(main, "System Prompt")
    user_template = load_prompt_block(main, "User Prompt 模板")
    schema_inline = load_schema_inline()
    user_template = user_template.replace(
        "请严格按 schema 输出 JSON。",
        f"请严格按下面的 schema 输出 JSON:\n\n```json\n{schema_inline}\n```",
    )
    return system_prompt, user_template


def extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def load_turn_map() -> dict[str, dict]:
    data = json.loads(SUMMARIES_JSON.read_text(encoding="utf-8"))
    out = {}
    for session in data:
        sid = session["session_id"]
        topic = session.get("main_topic", "")
        for turn_no, turn in enumerate(session.get("turn_summaries", []), 1):
            node_id = f"{sid}#{turn.get('turn_id') or f't{turn_no}'}"
            out[node_id] = {
                "session_id": sid,
                "turn_id": turn.get("turn_id") or "",
                "turn_no": turn_no,
                "main_topic": topic,
                "narrative": (turn.get("narrative") or "").strip(),
                "source_refs": list(dict.fromkeys(turn.get("source_refs") or [])),
            }
    return out


def load_candidates(limit: int, resume: bool) -> list[dict]:
    con = sqlite3.connect(SQLITE_DB)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA_SQL)
        sql = (
            "SELECT c.* FROM graph_relation_candidates c "
            "LEFT JOIN graph_relation_judgments j ON j.candidate_id = c.candidate_id "
        )
        where = []
        if resume:
            where.append("j.candidate_id IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.candidate_type, c.candidate_id"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in con.execute(sql).fetchall()]
        return rows
    finally:
        con.close()


def build_user_prompt(template: str, cand: dict, source_turn: dict, target_turn: dict) -> str:
    return (template
        .replace("{{candidate_id}}", cand["candidate_id"])
        .replace("{{candidate_type}}", cand["candidate_type"])
        .replace("{{candidate_reason}}", cand.get("candidate_reason") or "")
        .replace("{{similarity}}", str(cand.get("similarity")))
        .replace("{{source_session_id}}", source_turn["session_id"])
        .replace("{{source_turn_id}}", source_turn["turn_id"] or f"t{source_turn['turn_no']}")
        .replace("{{source_main_topic}}", source_turn.get("main_topic") or "")
        .replace("{{source_refs}}", ", ".join(source_turn.get("source_refs") or []))
        .replace("{{source_narrative}}", source_turn.get("narrative") or "")
        .replace("{{target_session_id}}", target_turn["session_id"])
        .replace("{{target_turn_id}}", target_turn["turn_id"] or f"t{target_turn['turn_no']}")
        .replace("{{target_main_topic}}", target_turn.get("main_topic") or "")
        .replace("{{target_refs}}", ", ".join(target_turn.get("source_refs") or []))
        .replace("{{target_narrative}}", target_turn.get("narrative") or ""))


def normalize_judgment(candidate_id: str, parsed: dict | None) -> dict:
    if not parsed:
        return {
            "candidate_id": candidate_id,
            "relation_type": "no_relation",
            "confidence": 0.0,
            "evidence_refs": [],
            "reason": "judge output parse failed",
            "risk_flags": ["parse_failed"],
        }
    rel = str(parsed.get("relation_type") or "no_relation").strip()
    if rel not in ALLOWED_RELATIONS:
        rel = "no_relation"
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    refs = parsed.get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = []
    flags = parsed.get("risk_flags") or []
    if not isinstance(flags, list):
        flags = [str(flags)] if flags else []
    reason = str(parsed.get("reason") or "").strip() or "(empty reason)"
    return {
        "candidate_id": candidate_id,
        "relation_type": rel,
        "confidence": conf,
        "evidence_refs": [str(x) for x in refs if str(x).strip()],
        "reason": reason,
        "risk_flags": [str(x) for x in flags if str(x).strip()],
    }


def write_judgments(rows: list[dict], model: str, temperature: float) -> None:
    con = sqlite3.connect(SQLITE_DB)
    try:
        con.executescript(SCHEMA_SQL)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        payload = [(
            r["candidate_id"], r["relation_type"], r["confidence"],
            json.dumps(r["evidence_refs"], ensure_ascii=False), r["reason"],
            json.dumps(r["risk_flags"], ensure_ascii=False), model,
            PROMPT_VERSION, temperature, now, "pending",
        ) for r in rows]
        con.executemany(
            "INSERT OR REPLACE INTO graph_relation_judgments "
            "(candidate_id, relation_type, confidence, evidence_refs_json, reason, risk_flags_json, "
            "model, prompt_version, temperature, created_at, gate_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        con.commit()
    finally:
        con.close()


def run_dry(candidates: list[dict], turn_map: dict[str, dict], system_prompt: str, user_template: str) -> int:
    print(f"[dry] candidates={len(candidates)}")
    for cand in candidates[:2]:
        src = turn_map[cand["source_node_id"]]
        tgt = turn_map[cand["target_node_id"]]
        prompt = build_user_prompt(user_template, cand, src, tgt)
        print("=" * 70)
        print(f"candidate_id={cand['candidate_id']} type={cand['candidate_type']} sim={cand.get('similarity')}")
        print("--- system prompt (first 200) ---")
        print(system_prompt[:200])
        print("--- user prompt (first 1200) ---")
        print(prompt[:1200])
    return 0


def run_write(candidates: list[dict], turn_map: dict[str, dict], system_prompt: str,
              user_template: str, model: str, temperature: float) -> int:
    client = llm_mod.make_llm_client()
    out = []
    for idx, cand in enumerate(candidates, 1):
        src = turn_map.get(cand["source_node_id"])
        tgt = turn_map.get(cand["target_node_id"])
        if not src or not tgt:
            row = normalize_judgment(cand["candidate_id"], None)
            row["reason"] = "source or target turn missing in summaries"
            row["risk_flags"] = ["turn_missing"]
            out.append(row)
            print(f"[{idx}/{len(candidates)}] {cand['candidate_id']} -> turn_missing")
            continue
        prompt = build_user_prompt(user_template, cand, src, tgt)
        try:
            raw = llm_mod._chat_with_retry(
                client, model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
            )
            parsed = extract_json(raw)
            row = normalize_judgment(cand["candidate_id"], parsed)
        except Exception as exc:
            row = normalize_judgment(cand["candidate_id"], None)
            row["reason"] = f"judge failed: {type(exc).__name__}: {str(exc)[:80]}"
            row["risk_flags"] = ["judge_failed"]
        out.append(row)
        print(f"[{idx}/{len(candidates)}] {cand['candidate_id']} -> {row['relation_type']} conf={row['confidence']:.2f}", flush=True)
    write_judgments(out, model=model, temperature=temperature)
    report = {
        "count": len(out),
        "by_relation_type": {},
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "temperature": temperature,
    }
    for row in out:
        report["by_relation_type"][row["relation_type"]] = report["by_relation_type"].get(row["relation_type"], 0) + 1
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[write] SQLite graph_relation_judgments = {len(out)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Wave 9.2 LLM relation judge")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--model", default=os.environ.get("MEM0_LLM_MODEL", DEFAULT_MODEL))
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    args = p.parse_args(argv)
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2
    if not args.dry_run and not args.write:
        print("[error] 必须指定 --dry-run 或 --write", file=sys.stderr)
        return 2
    system_prompt, user_template = load_prompts()
    turn_map = load_turn_map()
    candidates = load_candidates(args.limit, args.resume)
    if not candidates:
        print("[warn] 无待处理候选")
        return 0
    if args.dry_run:
        return run_dry(candidates, turn_map, system_prompt, user_template)
    return run_write(candidates, turn_map, system_prompt, user_template, args.model, args.temperature)


if __name__ == "__main__":
    raise SystemExit(main())
