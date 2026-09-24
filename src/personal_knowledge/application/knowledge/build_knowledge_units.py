"""Phase 14 Wave 2.1：Evidence → Knowledge Unit 抽取。

从 canonical conversation store 的 eligible user evidence 中提取结构化知识单元。
LLM 输出经过 Pydantic schema 验证、evidence ref 回查、speaker gate，产物先入 staging。

流程：
  1. 从 canonical store 读 eligible user evidence（预处理剥离 system-reminder）
  2. 按 subject/session 聚合成 evidence bundle
  3. 调 Vertex AI Gemini 抽取（temperature 0）
  4. Pydantic schema 验证（extra=forbid）
  5. evidence ref 回查（确认 evidence_quote 来自输入 bundle）
  6. 写入 knowledge_units 表（status='staging'）
  7. 报告 gate 指标

用法::

    python build_knowledge_units.py --dry-run --limit 20
    python build_knowledge_units.py --write --limit 50
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, AGENT_CONVERSATIONS_DB  # noqa: E402
from personal_knowledge.core.runtime_config import (  # noqa: E402
    gcloud_access_token,
    vertex_config,
    vertex_generate_content_url,
    vertex_generation_config,
)
from personal_knowledge.application.knowledge.knowledge_unit_pipeline import RunManifest, StagingPublisher  # noqa: E402
from personal_knowledge.core.providers import (  # noqa: E402
    PiKernelProvider, ProviderError, ProviderRequest, ProviderTimeout,
)

# Vertex AI 配置
_VERTEX = vertex_config()
GCP_PROJECT = _VERTEX.project
VERTEX_MODEL = _VERTEX.model
PROMPT_VERSION = "v1"

PROMPT_PATH = Path(__file__).resolve().parents[4] / "assets" / "prompts" / "knowledge_unit_extractor" / "v1_main.md"
V2_PROMPT_VERSION = "v2"
V2_PROMPT_PATH = Path(__file__).resolve().parents[4] / "assets" / "prompts" / "knowledge_unit_extractor" / "v2_main.md"


# === Pydantic schema（与 v1_schema.md 一致，extra=forbid）===

class KnowledgeUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_type: str = Field(...)
    subject: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=4, max_length=500)
    answer: str = Field(min_length=4, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=1)
    lifecycle: str = Field(default="current")
    duplicate_of: str | None = Field(default=None, max_length=48)

    @field_validator("unit_type")
    @classmethod
    def valid_unit_type(cls, v: str) -> str:
        allowed = {"preference", "habit", "personal_fact", "project_decision", "capability", "tool_usage"}
        if v not in allowed:
            raise ValueError(f"unit_type must be one of {allowed}")
        return v


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: list[KnowledgeUnit] = Field(default_factory=list)
    abstain: bool = Field(default=False)
    abstain_reason: str = Field(default="")


# === Phase 41（D-01）：assistant 轨独立 schema ===

ASSISTANT_PROMPT_VERSION = "v1_assistant"
ASSISTANT_PROMPT_PATH = Path(__file__).resolve().parents[4] / "assets" / "prompts" / "knowledge_unit_extractor" / "v1_assistant.md"
V2_ASSISTANT_PROMPT_VERSION = "v2_assistant"
V2_ASSISTANT_PROMPT_PATH = Path(__file__).resolve().parents[4] / "assets" / "prompts" / "knowledge_unit_extractor" / "v2_assistant.md"


class AssistantKnowledgeUnit(BaseModel):
    """assistant 轨知识单元（D-01 独立 unit_type 集合，与 user 轨 6 类型不混）。"""

    model_config = ConfigDict(extra="forbid")
    unit_type: str = Field(...)
    subject: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=4, max_length=500)
    answer: str = Field(min_length=4, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=1)
    lifecycle: str = Field(default="current")
    duplicate_of: str | None = Field(default=None, max_length=48)

    @field_validator("unit_type")
    @classmethod
    def valid_unit_type(cls, v: str) -> str:
        allowed = {"solution", "decision_rationale", "technical_conclusion"}
        if v not in allowed:
            raise ValueError(f"unit_type must be one of {allowed}")
        return v


class AssistantExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: list[AssistantKnowledgeUnit] = Field(default_factory=list)
    abstain: bool = Field(default=False)
    abstain_reason: str = Field(default="")


# === System-reminder 预处理（唯一权威定义在 eligibility.py，此处 re-export 兼容旧 import 路径）===

from personal_knowledge.application.knowledge.eligibility import (  # noqa: E402,F401
    SYSTEM_INJECTION_PATTERNS,
    strip_system_injections,
    is_meaningful,
)


# === Vertex AI 调用 ===

def _get_gcloud_token() -> str:
    return gcloud_access_token(_VERTEX)


def _extract_text(candidate: dict) -> str:
    parts = candidate.get("content", {}).get("parts", [])
    if parts:
        return "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
    c = candidate.get("content")
    return c if isinstance(c, str) else ""


def call_llm(system_prompt: str, user_content: str) -> dict:
    """调用 Pi extraction route; Vertex remains an explicit rollback seam."""
    if os.environ.get("PI_KERNEL_LEGACY_MODE", "").strip() != "1":
        prompt = f"{system_prompt}\n\n---\n用户对话证据（role=user）：\n{user_content}\n\n---\n请提取知识单元，输出JSON："
        request_checksum = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        provider = PiKernelProvider(purpose="extraction_summary")
        try:
            result = provider.generate(ProviderRequest(
                prompt=prompt, request_checksum=request_checksum, temperature=0,
                max_output_tokens=2048, timeout_seconds=120,
            ))
            provider.stage_candidate(
                candidate_id=f"pi_ku_legacy_{request_checksum[:24]}",
                proposal={"kind": "knowledge_extraction", "status": "pending_validation", "input_checksum": request_checksum, "response_checksum": result.response_checksum},
                evidence_refs=[{"ref": f"artifact:{hashlib.sha256(user_content.encode('utf-8')).hexdigest()[:32]}", "checksum": hashlib.sha256(user_content.encode("utf-8")).hexdigest()}],
                candidate_checksum=result.response_checksum,
                run_checksum=hashlib.sha256(f"{request_checksum}:{result.response_checksum}".encode()).hexdigest(),
            )
            return {"text": json.dumps(dict(result.response_payload), ensure_ascii=False), "usage": {"promptTokenCount": result.telemetry.input_tokens, "candidatesTokenCount": result.telemetry.output_tokens}}
        except ProviderTimeout:
            return {"error": "provider outcome unknown", "error_class": "terminal"}
        except ProviderError:
            return {"error": "pi kernel task failed", "error_class": "terminal"}

    # Explicit rollback-only Vertex compatibility path.
    token = _get_gcloud_token()
    url = vertex_generate_content_url(_VERTEX)
    user_text = f"{system_prompt}\n\n---\n用户对话证据（role=user）：\n{user_content}\n\n---\n请提取知识单元，输出JSON："
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": vertex_generation_config(VERTEX_MODEL, 2048),
    }).encode()

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }, method="POST")
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read().decode())
            return {"text": _extract_text(data["candidates"][0]),
                    "usage": data.get("usageMetadata", {})}
        except urllib.error.HTTPError as e:
            if e.code in (503, 500, 429) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return {"error": f"{type(e).__name__}: {str(e)[:150]}"}
    return {"error": "max retries exceeded"}


def _clean_json(text: str) -> str:
    """清洗 Gemini JSON 输出（去 code fence、括号匹配截取 JSON 对象）。"""
    cleaned = text.strip()
    # 去掉 markdown code fence
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # 用括号深度匹配截取第一个完整 JSON 对象
    first = cleaned.find("{")
    if first < 0:
        return cleaned
    depth = 0
    in_string = False
    escape = False
    for i in range(first, len(cleaned)):
        ch = cleaned[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[first:i + 1]
    return cleaned[first:]  # 未闭合，返回剩余


# === Evidence 加载 ===

def load_evidence(canonical_db: Path, limit: int | None = None) -> list[dict]:
    """LEGACY (L1 path, role=user only)：从 canonical store 读 eligible user evidence。

    预处理：剥离 system-reminder，过滤短消息。
    新代码应使用 eligibility.compute_eligible_messages（D-05 唯一口径）。
    """
    con = sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT m.canonical_message_id, m.canonical_session_id, m.content, "
        "m.source, s.agent, s.started_at, s.evidence_eligible "
        "FROM canonical_messages m JOIN canonical_sessions s "
        "ON m.canonical_session_id=s.canonical_session_id "
        "WHERE m.role='user' AND s.evidence_eligible=1 "
        "AND m.content IS NOT NULL AND length(m.content) > 20 "
        "ORDER BY s.started_at DESC"
    ).fetchall()
    con.close()

    evidence = []
    seen_hashes = set()
    for r in rows:
        content = r["content"]
        # 预处理剥离系统注入
        cleaned = strip_system_injections(content)
        if not is_meaningful(cleaned):
            continue
        # content_hash 去重
        chash = hashlib.sha256(" ".join(cleaned.split()).encode("utf-8")).hexdigest()[:32]
        if chash in seen_hashes:
            continue
        seen_hashes.add(chash)
        evidence.append({
            "message_id": r["canonical_message_id"],
            "session_id": r["canonical_session_id"],
            "raw_content": content,
            "cleaned_content": cleaned,
            "source": r["source"],
            "agent": r["agent"],
            "started_at": r["started_at"],
            "content_hash": chash,
        })
        if limit and len(evidence) >= limit:
            break
    return evidence


# === 主流程 ===

def run(dry_run: bool, write: bool, limit: int | None = None,
        canonical_db: Path = AGENT_CONVERSATIONS_DB,
        unified_db: Path = UNIFIED_DB) -> int:
    if dry_run and write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    evidence = load_evidence(canonical_db, limit=limit)

    print(f"{'='*60}")
    print("Phase 14 Wave 2：Knowledge Unit 抽取")
    print(f"{'='*60}")
    print(f"evidence bundles: {len(evidence)}")
    print(f"model: {VERTEX_MODEL}")
    print(f"prompt: {PROMPT_VERSION}")
    print()

    if dry_run:
        # dry-run：只加载 evidence，不调 LLM
        for i, ev in enumerate(evidence[:5], 1):
            print(f"[{i}] agent={ev['agent']:10} len={len(ev['cleaned_content']):4}")
            print(f"    {ev['cleaned_content'][:100]}")
        print(f"\n[dry-run] {len(evidence)} bundles loaded, no LLM calls. Use --write for extraction.")
        return 0

    # === --write：调 LLM 抽取 ===
    manifest = RunManifest.create(
        run_type="extraction",
        source_build_id="canonical-v1",
        input_data={"evidence_count": len(evidence), "first_hash": evidence[0]["content_hash"] if evidence else ""},
        prompt_version=PROMPT_VERSION,
        model=VERTEX_MODEL,
        config={"limit": limit, "strip_injections": True},
    )
    publisher = StagingPublisher(manifest, db_path=unified_db)
    publisher.begin_staging()

    stats = {
        "total": len(evidence),
        "extracted": 0,
        "abstained": 0,
        "errors": 0,
        "schema_valid": 0,
        "schema_invalid": 0,
        "units_total": 0,
        "units_with_evidence": 0,
        "units_without_evidence": 0,
        "by_type": {},
    }

    con = connect_rw(unified_db)
    for i, ev in enumerate(evidence, 1):
        print(f"[{i}/{len(evidence)}] agent={ev['agent']:10} len={len(ev['cleaned_content']):4}", end="")

        resp = call_llm(system_prompt, ev["cleaned_content"])
        if "error" in resp:
            print(f" -> ERROR: {resp['error'][:60]}")
            stats["errors"] += 1
            time.sleep(1)
            continue

        raw_text = resp["text"]
        try:
            cleaned_json = _clean_json(raw_text)
            parsed = json.loads(cleaned_json)
            result = ExtractionResult(**parsed)
            stats["schema_valid"] += 1
        except (json.JSONDecodeError, ValidationError) as ve:
            print(f" -> SCHEMA FAIL")
            stats["schema_invalid"] += 1
            time.sleep(1)
            continue

        if result.abstain:
            stats["abstained"] += 1
            print(f" -> ABSTAIN: {result.abstain_reason[:50]}")
        else:
            stats["extracted"] += 1
            print(f" -> {len(result.units)} units")

        for unit in result.units:
            stats["units_total"] += 1
            stats["by_type"][unit.unit_type] = stats["by_type"].get(unit.unit_type, 0) + 1

            # evidence 回查：evidence_quote 应来自输入 content 或与之高度重叠。
            # LLM 可能轻微改写（加标点、调语序），所以用关键词片段匹配：
            # evidence_quote 中任一 ≥10 字连续片段在原文中出现即算通过。
            def _evidence_supported(quote: str, source: str) -> bool:
                """检查 quote 是否有 ≥10 字连续片段在 source 中。"""
                if not quote:
                    return False
                # 精确匹配
                if quote.strip() in source:
                    return True
                # 滑动窗口：取 quote 中所有 10 字片段检查
                q = quote.strip()
                for j in range(len(q) - 9):
                    if q[j:j + 10] in source:
                        return True
                return False

            if _evidence_supported(unit.evidence_quote, ev["cleaned_content"]):
                stats["units_with_evidence"] += 1
            else:
                stats["units_without_evidence"] += 1

            # 写入 staging
            unit_id = hashlib.sha256(
                f"v1|{manifest.run_id}|{ev['message_id']}|{stats['units_total']}".encode()
            ).hexdigest()[:32]
            unit_id = f"v1|{unit_id}"
            con.execute(
                "INSERT OR REPLACE INTO knowledge_units "
                "(unit_id, run_id, unit_type, subject, question, answer, confidence, "
                "evidence_quote, lifecycle, source_session_id, source_message_ref, "
                "source_agent, evidence_scope, status, version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    unit_id, manifest.run_id, unit.unit_type, unit.subject,
                    unit.question, unit.answer, unit.confidence,
                    unit.evidence_quote, unit.lifecycle,
                    ev["session_id"], ev["message_id"], ev["agent"],
                    "user", "staging", 1,
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            # evidence link
            con.execute(
                "INSERT OR IGNORE INTO knowledge_unit_evidence (unit_id, evidence_ref) VALUES (?,?)",
                (unit_id, ev["message_id"]),
            )

        con.commit()
        time.sleep(1)  # Vertex AI rate limit safety

    con.close()

    # Gate 检查
    gate_pass = (
        stats["schema_invalid"] / max(stats["total"], 1) < 0.05  # schema 有效率 ≥95%
        and stats["units_without_evidence"] == 0  # 无 evidence 的 unit = 0
    )

    if gate_pass and write:
        publisher.promote(
            dataset_hash=hashlib.sha256(str(stats).encode()).hexdigest()[:32],
            stats=stats,
        )
        print(f"\n[ok] gate PASS, {stats['units_total']} units promoted to current")
    else:
        publisher.abort(reason=f"gate failed: schema_invalid={stats['schema_invalid']}, no_evidence={stats['units_without_evidence']}")
        print(f"\n[warn] gate {'PASS' if gate_pass else 'FAIL'}, units in staging (not promoted)")

    print(f"\n{'='*60}")
    print(f"抽取完成: {stats['extracted']} extracted, {stats['abstained']} abstained, "
          f"{stats['errors']} errors")
    print(f"units: {stats['units_total']} (with evidence: {stats['units_with_evidence']}, "
          f"without: {stats['units_without_evidence']})")
    print(f"schema: {stats['schema_valid']} valid / {stats['schema_invalid']} invalid")
    print(f"types: {stats['by_type']}")
    print(f"gate: {'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14 Wave 2: knowledge unit extraction")
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--write", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 条 evidence")
    args = p.parse_args(argv)
    if not args.write and not args.dry_run:
        args.dry_run = True  # 默认 dry-run
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2
    return run(args.dry_run, args.write, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
