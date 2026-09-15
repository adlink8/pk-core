"""Wave 2: decompose and merge memory mechanisms.

This script compares the two memory experiments at the method level:

- first-gen rule memory scripts: deterministic rules directly write long-term
  memory tables;
- second-gen conversation graph scripts: turn compression, vector candidate
  generation, prompt-driven relation judgment, and evidence gates.

It intentionally does not rank old memory items against new graph edges, does
not write memory_items/memory_links/memory_relations, and does not create a
promotion table. Wave 2 only emits the mechanism matrix and target design.

Usage:
  python integration\\scripts\\analyze_memory_mechanisms.py --dry-run
  python integration\\scripts\\analyze_memory_mechanisms.py --write
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = ROOT / "tools" / "compat" / "v1_1"
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
PROMPT_DIR = ROOT / "assets" / "prompts" / "memory_mechanism_judge"

INVENTORY_JSON = AI_DIR / "memory_experiment_inventory.json"
INVENTORY_MD = AI_DIR / "memory_experiment_inventory.md"
LEGACY_COMPARISON_JSON = AI_DIR / "memory_experiment_comparison.json"
LEGACY_COMPARISON_MD = AI_DIR / "memory_experiment_comparison.md"

OUT_JSON = AI_DIR / "memory_mechanism_matrix.json"
OUT_MD = AI_DIR / "memory_mechanism_matrix.md"
OUT_DESIGN_MD = AI_DIR / "memory_pipeline_target_design.md"

PROMPT_VERSION = "memory_mechanism_judge/v1"
SCHEMA_VERSION = "v1"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_TEMPERATURE = 0.1

MECHANISM_STEPS = [
    "input_selection",
    "unitization",
    "compression",
    "candidate_generation",
    "semantic_judgment",
    "evidence_gate",
    "storage_boundary",
    "promotion_policy",
    "decomplexity",
]

REQUIRED_ROW_FIELDS = [
    "mechanism_step",
    "old_method",
    "new_method",
    "keep_from_old",
    "keep_from_new",
    "merged_method",
    "delete_or_deprecate",
    "required_tables",
    "required_prompts",
    "required_human_review",
    "evidence_refs",
    "source_files",
    "reason",
    "risk_flags",
    "prompt_version",
    "model",
    "temperature",
    "llm_status",
]

OLD_SCRIPTS = [
    "build_memory_store.py",
    "build_capability_memory.py",
    "build_context_memory.py",
    "build_preference_memory.py",
    "build_memory_graph.py",
]

NEW_SCRIPTS = [
    "build_conversation_summary.py",
    "build_conversation_vector_store.py",
    "build_graph_relation_candidates.py",
    "judge_graph_relations.py",
    "evaluate_graph_relation_judgments.py",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def line_refs(path: Path, patterns: list[str], limit: int = 8) -> list[dict[str, Any]]:
    """Return deterministic source references for matching lines."""
    if not path.exists():
        return []
    refs: list[dict[str, Any]] = []
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if any(p.search(line) for p in compiled):
            refs.append(
                {
                    "file": rel(path),
                    "line": lineno,
                    "snippet": line.strip()[:180],
                }
            )
            if len(refs) >= limit:
                break
    return refs


def compact_ref(ref: dict[str, Any]) -> str:
    return f"{ref['file']}:{ref['line']}"


def source_files_for(*names: str) -> list[str]:
    return [rel(SCRIPTS_DIR / name) for name in names]


def report_file_refs() -> list[str]:
    candidates = [
        INVENTORY_MD,
        INVENTORY_JSON,
        AI_DIR / "conversation_quality_report.json",
        AI_DIR / "vector_collection_health.json",
        AI_DIR / "vector_retrieval_eval_report.json",
        AI_DIR / "graph_relation_candidates_report.json",
        AI_DIR / "graph_relation_judgments_report.json",
        AI_DIR / "graph_relation_eval_report.json",
    ]
    return [rel(path) for path in candidates if path.exists()]


def detect_llm_status(use_llm: bool = False) -> str:
    """LLM calls are optional for Wave 2.

    Default execution is deterministic. If the caller does not explicitly opt
    into LLM use, the status says so. If LLM use is requested but no key exists,
    the status is the explicit no-key fallback required by the plan.
    """
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MEM0_API_KEY")
    if not api_key:
        return "fallback:no_api_key"
    if not use_llm:
        return "fallback:llm_not_requested"
    return "fallback:live_not_implemented"


def _resolve_script(name: str) -> Path:
    """Locate implementation after scripts package split (prefer package dir over shim)."""
    implementations = sorted((ROOT / "src" / "personal_knowledge").rglob(name))
    if implementations:
        return implementations[0]
    # Prefer real implementation under domain packages; fall back to root shim.
    for pkg in (
        "core",
        "knowledge",
        "memory",
        "conversation",
        "graph",
        "vector",
        "services",
        "pipeline",
        "source_adapters",
    ):
        candidate = SCRIPTS_DIR / pkg / name
        if candidate.exists():
            return candidate
    return SCRIPTS_DIR / name


def build_evidence_index() -> dict[str, list[dict[str, Any]]]:
    script = _resolve_script
    return {
        "input_selection": (
            line_refs(script("build_memory_store.py"), ["unified_events", "service_stats", "tool_stats"], 4)
            + line_refs(script("build_preference_memory.py"), ["PREF_SOURCES", "source='Google'", "service IN"], 4)
            + line_refs(script("build_conversation_summary.py"), ["AGENT_DB", "agent_messages", "session_rows"], 4)
            + line_refs(script("build_conversation_vector_store.py"), ["conversation_summaries", "conversation_turns"], 4)
        ),
        "unitization": (
            line_refs(script("build_memory_store.py"), ["memory_id", "event_id", "memory_links"], 5)
            + line_refs(script("build_conversation_summary.py"), ["TurnSummary", "assemble_turns", "turn_id"], 5)
            + line_refs(script("build_conversation_vector_store.py"), ["unit_id", "session_id", "turn_no"], 4)
        ),
        "compression": (
            line_refs(script("build_conversation_summary.py"), ["SUMMARY_SYSTEM_PROMPT", "压缩率目标", "narrative"], 8)
            + line_refs(script("build_conversation_vector_store.py"), ["MIN_NARRATIVE_LEN", "narrative"], 4)
        ),
        "candidate_generation": (
            line_refs(script("build_memory_store.py"), ["CONTINUOUS_", "DECLINING_", "classify_tooling_memory"], 5)
            + line_refs(script("build_capability_memory.py"), ["PREFIX_GROUPS", "classify_capability"], 4)
            + line_refs(script("build_graph_relation_candidates.py"), ["semantic_candidate", "temporal_candidate", "MIN_SEMANTIC_SCORE", "top_k"], 8)
        ),
        "semantic_judgment": (
            line_refs(script("build_memory_graph.py"), ["TOPIC_MAPPING", "ENABLES_MAPPING", "EMBODIES_MAPPING", "same_subject"], 8)
            + line_refs(script("judge_graph_relations.py"), ["PROMPT_DIR", "ALLOWED_RELATIONS", "normalize_judgment", "prompt_version"], 8)
        ),
        "evidence_gate": (
            line_refs(script("build_capability_memory.py"), ["memory_links", "evidenced_by", "build_governance_metadata"], 5)
            + line_refs(script("evaluate_graph_relation_judgments.py"), ["refs_match", "ACCEPT_MIN_CONF", "REVIEW_MIN_CONF", "gate_status"], 8)
        ),
        "storage_boundary": (
            line_refs(script("build_memory_store.py"), ["CREATE TABLE IF NOT EXISTS memory_items", "memory_relations"], 5)
            + line_refs(script("build_graph_relation_candidates.py"), ["graph_relation_candidates", "SAMPLE_REPORT"], 5)
            + line_refs(script("judge_graph_relations.py"), ["graph_relation_judgments", "OUT_REPORT"], 5)
            + line_refs(script("evaluate_graph_relation_judgments.py"), ["graph_relation_review_queue", "OUT_JSON"], 5)
        ),
        "promotion_policy": (
            line_refs(script("build_memory_store.py"), ["INSERT OR REPLACE INTO memory_items", "reset_memory_tables"], 5)
            + line_refs(script("judge_graph_relations.py"), ["gate_status", "pending"], 4)
            + line_refs(script("evaluate_graph_relation_judgments.py"), ["accepted", "review", "rejected"], 6)
        ),
        "decomplexity": (
            line_refs(script("audit_memory_experiments.py"), ["remove_candidate", "deprecated", "mem0"], 8)
            + line_refs(script("build_memory_graph.py"), ["RULES_VERSION", "TOPIC_MAPPING"], 4)
        ),
    }


def matrix_definitions() -> dict[str, dict[str, Any]]:
    return {
        "input_selection": {
            "old_method": "从 unified_events、entities、event_entities、entity_links_v2 和 Google 模块事件中直接筛选记忆来源；输入偏事件和实体统计。",
            "new_method": "从规范化 agent_messages / tool_calls 生成 conversation_summaries，再把 turn 叙述写入独立 conversation_turns 向量 collection。",
            "keep_from_old": "保留 unified_events / memory_links 作为历史事件证据源，保留服务、工具、skill、项目路径等结构化信号。",
            "keep_from_new": "保留对话 turn 级输入和 source_refs，因为它能解释用户意图、工具调用和结果因果。",
            "merged_method": "输入层同时接受 normalized events 和 compressed conversation turns；events 只作证据，turn narratives 作高密度上下文。",
            "delete_or_deprecate": ["不要把旧 memory_items 作为唯一输入权威", "mem0 candidate 路径只保留审计记录"],
            "required_tables": ["unified_events", "unified_events_rich", "entities", "event_entities", "conversation_sessions", "conversation_turns_summary"],
            "required_prompts": ["memory_mechanism_judge/v1", "conversation_summary prompt v2"],
            "required_human_review": False,
            "reason": "旧事件层覆盖长期行为统计，新对话层覆盖任务意图和因果；二者合并为 source corpus，而不是互相覆盖。",
            "risk_flags": ["legacy_memory_not_authoritative", "conversation_summaries_must_have_source_refs"],
        },
        "unitization": {
            "old_method": "以 memory_type/subtype/subject 聚合成 memory_items，并用 memory_links 回连 event_id；图关系以 memory_id 为节点。",
            "new_method": "以 session_id + turn_id/turn_no 构成 turn 节点，向量单元是 turn narrative，候选 pair 是 turn-to-turn。",
            "keep_from_old": "保留 event_id 和 memory_links 这种低成本、可审计的证据链接方式。",
            "keep_from_new": "保留 turn 作为最小语义单元，避免 message 级碎片化和 session 级过粗。",
            "merged_method": "候选判断以 turn/event evidence bundle 为单位；长期记忆 claim 只有在后续 promotion gate 通过后才成为 memory item。",
            "delete_or_deprecate": ["废弃以 memory_id 之间规则相似度直接定关系的主判断路径"],
            "required_tables": ["memory_links", "conversation_turns_summary", "graph_relation_candidates"],
            "required_prompts": ["memory_mechanism_judge/v1"],
            "required_human_review": False,
            "reason": "event_id 提供原始回溯，turn_id 提供上下文边界；目标 pipeline 需要二者共存。",
            "risk_flags": ["id_bridge_missing_until_wave3", "memory_id_cannot_replace_source_refs"],
        },
        "compression": {
            "old_method": "基本不做 LLM 叙述压缩，直接从统计、关键词、路径、规则阈值写描述。",
            "new_method": "使用 conversation summary prompt 逐 turn 生成叙述，保留主干、分支、工具调用和因果链。",
            "keep_from_old": "保留规则统计值和原始事件作为压缩后的可校验背景，不把 LLM 摘要当唯一事实。",
            "keep_from_new": "保留高密度 turn narrative，作为候选生成、LLM 判断和人工 review 的主要上下文。",
            "merged_method": "压缩层先生成 turn narrative，再附带原始 source_refs / event stats；任何长期 claim 必须能回源。",
            "delete_or_deprecate": ["不要用硬编码 description 直接替代上下文压缩"],
            "required_tables": ["conversation_turns_summary"],
            "required_prompts": ["conversation_summary prompt v2", "memory_mechanism_judge/v1"],
            "required_human_review": False,
            "reason": "旧规则描述可解释但上下文薄；新摘要能保留任务过程，但必须被 source_refs 约束。",
            "risk_flags": ["summary_hallucination_possible", "compression_loss_possible"],
        },
        "candidate_generation": {
            "old_method": "用阈值、信号词、前缀归并、人工映射和同名匹配直接生成 memory_items 与 memory_relations。",
            "new_method": "用 conversation_turns 向量 top-k 生成 cross-session semantic candidates，并加入同 session adjacent temporal candidates。",
            "keep_from_old": "保留阈值和信号词作为 deterministic prefilter / guardrail，不保留其最终裁决权。",
            "keep_from_new": "保留向量召回和 temporal adjacency 作为候选扩展机制。",
            "merged_method": "候选生成采用 hybrid: events/statistics 产出 evidence candidates，turn vectors 产出 semantic candidates，脚本只负责召回和去重。",
            "delete_or_deprecate": ["废弃 TOPIC_MAPPING/ENABLES_MAPPING/EMBODIES_MAPPING 作为最终关系判断器"],
            "required_tables": ["memory_links", "graph_relation_candidates", "conversation_turns"],
            "required_prompts": ["memory_mechanism_judge/v1"],
            "required_human_review": False,
            "reason": "候选阶段需要高召回和可审计过滤；最终语义成立与长期价值不能由 shallow rules 决定。",
            "risk_flags": ["vector_similarity_not_truth", "rule_thresholds_bias_candidates"],
        },
        "semantic_judgment": {
            "old_method": "语义判断由 Python if/regex/映射表完成，包括工具强度、偏好主题、项目主题和记忆间关系。",
            "new_method": "judge_graph_relations.py 使用版本化 prompt + JSON schema 让 LLM 判 relation_type、confidence、evidence_refs、risk_flags。",
            "keep_from_old": "保留 deterministic 校验：schema、允许枚举、confidence clamp、证据 ref 过滤。",
            "keep_from_new": "保留 prompt-driven semantic judgment，尤其处理重复、冲突、长期价值和关系类型解释。",
            "merged_method": "LLM 负责 semantic judgment；scripts 负责 payload 组装、schema 校验、枚举校验、证据 refs 校验和审计输出。",
            "delete_or_deprecate": ["禁止把旧规则换名后继续当最终 judgment", "旧 memory_experiment_comparison 逐条 PK 口径不再作为主结论"],
            "required_tables": ["graph_relation_candidates", "graph_relation_judgments"],
            "required_prompts": ["memory_mechanism_judge/v1", "graph_relation_judge/v1"],
            "required_human_review": True,
            "reason": "Phase 08 的判断对象是机制步骤合并；具体记忆晋级必须留到后续 review/promotion gate。",
            "risk_flags": ["llm_misjudgment_possible", "prompt_version_must_be_recorded"],
        },
        "evidence_gate": {
            "old_method": "memory_links 记录 event evidence，metadata 记录 governance，但旧规则写入时 evidence gate 偏弱。",
            "new_method": "evaluate_graph_relation_judgments.py 校验 evidence_refs 是否匹配 source_refs，并按 confidence/risk_flags 分类 accepted/review/rejected。",
            "keep_from_old": "保留 memory_links 的 evidenced_by 结构和治理 metadata。",
            "keep_from_new": "保留 refs_match、confidence thresholds、risk_flags review queue 机制。",
            "merged_method": "任何候选必须带 event_id/session_id/turn_id/source_refs；证据不匹配只能 review 或 reject，不能自动晋级。",
            "delete_or_deprecate": ["废弃无 evidence_refs 的长期写入路径"],
            "required_tables": ["memory_links", "graph_relation_judgments", "graph_relation_review_queue"],
            "required_prompts": ["memory_mechanism_judge/v1", "graph_relation_judge/v1"],
            "required_human_review": True,
            "reason": "证据链是两套机制融合的共同约束；没有可回溯证据的判断不应进入长期记忆。",
            "risk_flags": ["evidence_mismatch", "source_ref_format_variance"],
        },
        "storage_boundary": {
            "old_method": "规则脚本直接写 memory_items、memory_links、memory_relations，并由 profile/search 读取。",
            "new_method": "候选、判定、review queue、accepted graph 分层存储；accepted graph 仍是实验分析层，不是长期记忆事实。",
            "keep_from_old": "保留现有 memory_items/memory_links/memory_relations 作为当前检索兼容层，但不在 Wave 2 写入。",
            "keep_from_new": "保留 graph_relation_candidates/judgments/eval report 的审计层职责。",
            "merged_method": "目标边界分三层：evidence layer、judgment audit layer、reviewed long-term memory layer；Wave 2 只写报告文件。",
            "delete_or_deprecate": ["不要新增第三套长期 memory store", "不要把 accepted graph edges 直接写入 memory_items"],
            "required_tables": ["memory_items", "memory_links", "memory_relations", "graph_relation_candidates", "graph_relation_judgments", "e_relation"],
            "required_prompts": ["memory_mechanism_judge/v1"],
            "required_human_review": True,
            "reason": "存储职责必须防止实验判断污染长期记忆，同时保持现有检索入口不破坏。",
            "risk_flags": ["long_term_store_pollution", "parallel_store_complexity"],
        },
        "promotion_policy": {
            "old_method": "满足规则阈值后直接 insert/replace memory_items，并按 memory_type 局部清空重建。",
            "new_method": "Phase 07 只产出 judgment/gate status；accepted edge 不等于长期记忆。",
            "keep_from_old": "保留长期 memory schema 的兼容性和现有 retrieval 价值。",
            "keep_from_new": "保留 promotion 前必须经过 prompt judgment、evidence gate 和 review 的思想。",
            "merged_method": "Wave 2 定义晋级政策：只有稳定、非一次性、非重复/冲突、证据 refs 可解析、人工或高置信 gate 通过的 claim 才能进入长期记忆；实际候选表属于后续 Wave。",
            "delete_or_deprecate": ["废弃规则命中即长期写入", "废弃 accepted graph edge 自动晋级"],
            "required_tables": ["memory_items", "memory_links", "graph_relation_judgments"],
            "required_prompts": ["memory_mechanism_judge/v1", "future memory_promotion_judge"],
            "required_human_review": True,
            "reason": "promotion 是长期记忆污染的主要风险点，必须从抽取脚本中拆出来。",
            "risk_flags": ["promotion_not_executed_in_wave2", "human_review_required_for_high_risk"],
        },
        "decomplexity": {
            "old_method": "多条规则抽取脚本、规则图谱、profile/report、旧 comparison 结果并存，容易把实验产物误当主线。",
            "new_method": "inventory 已标记 remove/deprecated candidates；新机制把判断收敛到 deterministic orchestration + prompt judgment + gate。",
            "keep_from_old": "保留仍被 run_pipeline、unified_search、profile 读取的兼容入口。",
            "keep_from_new": "保留 conversation summaries、vector store、relation candidates/judgments/eval 这一条审计链。",
            "merged_method": "先标记废弃与替代路径，再在后续删除计划中关入口；Wave 2 只给机制级删减方向。",
            "delete_or_deprecate": ["memory_experiment_comparison.md/json 属旧口径，仅作反例", "mem0 candidate 实验路径降级为 archive", "graph_relation_review_queue 若无读者则仅作 audit spillover"],
            "required_tables": ["memory_items", "graph_relation_review_queue", "e_relation"],
            "required_prompts": ["memory_mechanism_judge/v1"],
            "required_human_review": True,
            "reason": "去复杂化要先识别仍在用的入口，不能直接删除；旧 comparison 产物不能继续驱动 Wave 2 主结论。",
            "risk_flags": ["deletion_requires_later_phase", "do_not_break_existing_retrieval"],
        },
    }


def build_mechanism_matrix(
    *,
    llm_status: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> list[dict[str, Any]]:
    status = llm_status or detect_llm_status(use_llm=False)
    evidence_index = build_evidence_index()
    defs = matrix_definitions()
    fallback_flags = []
    if status.startswith("fallback:no_api_key"):
        fallback_flags = ["fallback_no_api_key", "deterministic_fallback"]
    elif status.startswith("fallback:"):
        fallback_flags = ["deterministic_fallback"]

    rows: list[dict[str, Any]] = []
    for step in MECHANISM_STEPS:
        item = dict(defs[step])
        evidence = evidence_index.get(step, [])
        source_files = sorted({ref["file"] for ref in evidence})
        risk_flags = list(dict.fromkeys(item.get("risk_flags", []) + fallback_flags))
        row = {
            "mechanism_step": step,
            "old_method": item["old_method"],
            "new_method": item["new_method"],
            "keep_from_old": item["keep_from_old"],
            "keep_from_new": item["keep_from_new"],
            "merged_method": item["merged_method"],
            "delete_or_deprecate": item["delete_or_deprecate"],
            "required_tables": item["required_tables"],
            "required_prompts": item["required_prompts"],
            "required_human_review": bool(item["required_human_review"]),
            "evidence_refs": [compact_ref(ref) for ref in evidence],
            "source_files": source_files,
            "reason": item["reason"],
            "risk_flags": risk_flags,
            "prompt_version": PROMPT_VERSION,
            "model": model,
            "temperature": temperature,
            "llm_status": status,
        }
        validate_row(row)
        rows.append(row)
    return rows


def validate_row(row: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_ROW_FIELDS if field not in row]
    if missing:
        raise ValueError(f"matrix row missing fields: {missing}")
    if row["mechanism_step"] not in MECHANISM_STEPS:
        raise ValueError(f"unknown mechanism_step: {row['mechanism_step']}")
    if not row["evidence_refs"]:
        raise ValueError(f"{row['mechanism_step']} has no evidence refs")


def build_target_pipeline(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "target_memory_pipeline_v1",
        "scope": "Wave 2 mechanism design only; no long-term memory writes.",
        "ordered_stages": [
            {
                "stage": "source_corpus",
                "method": "Load normalized events plus compressed conversation turns, preserving event_id/session_id/turn_id/source_refs.",
                "mechanism_steps": ["input_selection", "unitization"],
            },
            {
                "stage": "compression_context",
                "method": "Use turn narrative summaries as context, with original refs and old event statistics attached as evidence.",
                "mechanism_steps": ["compression"],
            },
            {
                "stage": "candidate_boundary",
                "method": "Generate candidates through deterministic filters, event evidence bundles, vector top-k, and temporal adjacency; scripts do not make final semantic decisions.",
                "mechanism_steps": ["candidate_generation"],
            },
            {
                "stage": "semantic_judgment",
                "method": "Use versioned prompts and JSON schema for relation/long-term-value/redundancy/conflict judgment; scripts validate output.",
                "mechanism_steps": ["semantic_judgment"],
            },
            {
                "stage": "evidence_and_review_gate",
                "method": "Reject or review records whose evidence refs do not resolve, whose confidence is weak, or whose risk flags are present.",
                "mechanism_steps": ["evidence_gate", "promotion_policy"],
            },
            {
                "stage": "storage_boundary",
                "method": "Keep experiment artifacts separate from reviewed long-term memory; Wave 2 writes only matrix/design reports.",
                "mechanism_steps": ["storage_boundary", "decomplexity"],
            },
        ],
        "non_goals_enforced": [
            "No memory_items writes.",
            "No memory_links writes.",
            "No memory_relations writes.",
            "No promotion table creation in Wave 2.",
            "Legacy memory_experiment_comparison.md/json is explicitly marked old-scope evidence, not the main conclusion.",
        ],
        "step_count": len(rows),
    }


def build_report(
    *,
    llm_status: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> dict[str, Any]:
    inventory = load_json(INVENTORY_JSON)
    rows = build_mechanism_matrix(llm_status=llm_status, model=model, temperature=temperature)
    report_refs = report_file_refs()
    legacy_exists = LEGACY_COMPARISON_JSON.exists() or LEGACY_COMPARISON_MD.exists()
    return {
        "generated_at": utc_now(),
        "phase": "08",
        "wave": "2",
        "scope": "mechanism_decomposition_matrix",
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "temperature": temperature,
        "llm_status": rows[0]["llm_status"] if rows else detect_llm_status(False),
        "mechanism_steps": rows,
        "target_pipeline": build_target_pipeline(rows),
        "source_corpus": {
            "old_scripts": source_files_for(*OLD_SCRIPTS),
            "new_scripts": source_files_for(*NEW_SCRIPTS),
            "reports": report_refs,
            "inventory_status": inventory.get("status", "unknown"),
            "key_counts": inventory.get("key_counts", {}),
        },
        "legacy_comparison_disposition": {
            "files_exist": legacy_exists,
            "files": [rel(p) for p in (LEGACY_COMPARISON_MD, LEGACY_COMPARISON_JSON) if p.exists()],
            "status": "old_scope_counterexample_not_main_output",
            "reason": "Those files compare old memory records against graph edges. Wave 2 now compares and merges mechanism methods.",
        },
    }


def render_matrix_md(report: dict[str, Any]) -> str:
    lines = [
        "# Memory Mechanism Decomposition Matrix",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- phase: {report['phase']}",
        f"- wave: {report['wave']}",
        f"- prompt_version: {report['prompt_version']}",
        f"- schema_version: {report['schema_version']}",
        f"- llm_status: {report['llm_status']}",
        f"- model: {report['model']}",
        f"- temperature: {report['temperature']}",
        "",
        "## Scope Correction",
        "",
        "This report compares and merges the methods used by the first-gen rule memory mechanism and the second-gen LLM conversation graph mechanism.",
        "",
        "The old `memory_experiment_comparison.md/json` artifacts are legacy-scope counterexamples: they compare old memory records with graph edges and are not used as the main Wave 2 conclusion.",
        "",
        "Wave 2 does not write `memory_items`, `memory_links`, or `memory_relations`, and does not create a promotion table.",
        "",
        "## Matrix",
        "",
        "| Step | Merged Method | Keep From Old | Keep From New | Deprecate/Delete | Human Review | LLM Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["mechanism_steps"]:
        dep = "; ".join(row["delete_or_deprecate"])
        lines.append(
            f"| `{row['mechanism_step']}` | {row['merged_method']} | {row['keep_from_old']} | "
            f"{row['keep_from_new']} | {dep} | {row['required_human_review']} | `{row['llm_status']}` |"
        )

    lines += ["", "## Step Evidence", ""]
    for row in report["mechanism_steps"]:
        lines.append(f"### {row['mechanism_step']}")
        lines.append(f"- old_method: {row['old_method']}")
        lines.append(f"- new_method: {row['new_method']}")
        lines.append(f"- required_tables: {', '.join(f'`{x}`' for x in row['required_tables'])}")
        lines.append(f"- required_prompts: {', '.join(f'`{x}`' for x in row['required_prompts'])}")
        lines.append(f"- reason: {row['reason']}")
        lines.append(f"- risk_flags: {', '.join(f'`{x}`' for x in row['risk_flags'])}")
        lines.append(f"- evidence_refs: {', '.join(f'`{x}`' for x in row['evidence_refs'][:10])}")
        lines.append("")

    lines += ["## Target Pipeline Summary", ""]
    for stage in report["target_pipeline"]["ordered_stages"]:
        lines.append(f"- `{stage['stage']}`: {stage['method']}")
    lines.append("")
    lines.append("## Source Corpus")
    lines.append("")
    lines.append("### Old Mechanism Scripts")
    for path in report["source_corpus"]["old_scripts"]:
        lines.append(f"- `{path}`")
    lines.append("")
    lines.append("### New Mechanism Scripts")
    for path in report["source_corpus"]["new_scripts"]:
        lines.append(f"- `{path}`")
    lines.append("")
    lines.append("### Reports")
    for path in report["source_corpus"]["reports"]:
        lines.append(f"- `{path}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def render_target_design_md(report: dict[str, Any]) -> str:
    pipeline = report["target_pipeline"]
    lines = [
        "# Memory Pipeline Target Design",
        "",
        "## One-line Design",
        "",
        "A single auditable memory pipeline: normalized evidence plus compressed turns -> deterministic candidate assembly -> prompt-driven semantic judgment -> evidence gate -> human-reviewed long-term memory.",
        "",
        "## Boundary",
        "",
        "- Wave 2 outputs mechanism design only.",
        "- Existing long-term memory tables remain compatibility surfaces, not automatic write targets.",
        "- Phase 07 accepted graph edges remain analysis-layer judgments until later promotion review.",
        "- The old item-vs-edge comparison artifacts are not the governing output for this wave.",
        "",
        "## Ordered Pipeline",
        "",
    ]
    for idx, stage in enumerate(pipeline["ordered_stages"], 1):
        steps = ", ".join(f"`{s}`" for s in stage["mechanism_steps"])
        lines.append(f"{idx}. `{stage['stage']}`")
        lines.append(f"   - method: {stage['method']}")
        lines.append(f"   - mechanism_steps: {steps}")
        lines.append("")

    lines += [
        "## Retained Methods",
        "",
        "- From old mechanism: event evidence, memory_links-style traceability, existing retrieval-compatible long-term memory schema, deterministic schema checks.",
        "- From new mechanism: turn-level narrative compression, conversation_turns vector recall, prompt/schema judgment, evidence gate and review queue semantics.",
        "",
        "## Replaced Methods",
        "",
        "- Rule-only semantic judgment is replaced by prompt-driven judgment plus deterministic validation.",
        "- Direct write-on-rule-hit promotion is replaced by a later review-controlled promotion path.",
        "- Item-vs-edge result comparison is replaced by mechanism-step fusion.",
        "",
        "## Required Human Review",
        "",
        "- Any deletion, overwrite, merge, or long-term memory promotion.",
        "- Any judgment with evidence mismatch, conflict, duplicate ambiguity, low confidence, or risk flags.",
        "- Any decomplexity action that affects active scripts, retrieval paths, or existing profile generation.",
        "",
        "## Non-goals Enforced",
        "",
    ]
    lines.extend(f"- {item}" for item in pipeline["non_goals_enforced"])
    lines.append("")
    lines.append("## LLM/Fallback Status")
    lines.append("")
    lines.append(f"- llm_status: `{report['llm_status']}`")
    lines.append(f"- prompt_version: `{report['prompt_version']}`")
    lines.append(f"- model: `{report['model']}`")
    lines.append(f"- temperature: `{report['temperature']}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_outputs(report: dict[str, Any]) -> None:
    AI_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_matrix_md(report), encoding="utf-8")
    OUT_DESIGN_MD.write_text(render_target_design_md(report), encoding="utf-8")


def dry_run(report: dict[str, Any]) -> None:
    print("# Memory Mechanism Matrix Dry Run")
    print(f"llm_status: {report['llm_status']}")
    print(f"steps: {len(report['mechanism_steps'])}")
    print(f"legacy_comparison: {report['legacy_comparison_disposition']['status']}")
    print("target_pipeline:")
    for stage in report["target_pipeline"]["ordered_stages"]:
        print(f"  - {stage['stage']}: {', '.join(stage['mechanism_steps'])}")
    print("matrix_steps:")
    for row in report["mechanism_steps"]:
        print(f"  - {row['mechanism_step']}: refs={len(row['evidence_refs'])} human_review={row['required_human_review']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 08 Wave 2 memory mechanism matrix.")
    parser.add_argument("--dry-run", action="store_true", help="print summary only; do not write output files")
    parser.add_argument("--write", action="store_true", help="write matrix JSON/Markdown and target design")
    parser.add_argument("--model", default=os.environ.get("MEM0_LLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--use-llm", action="store_true", help="reserved; Wave 2 defaults to deterministic fallback")
    args = parser.parse_args(argv)

    if args.dry_run and args.write:
        print("[error] --dry-run and --write are mutually exclusive")
        return 2
    if not args.dry_run and not args.write:
        print("[error] specify --dry-run or --write")
        return 2

    report = build_report(
        llm_status=detect_llm_status(use_llm=args.use_llm),
        model=args.model,
        temperature=args.temperature,
    )
    if args.dry_run:
        dry_run(report)
        return 0

    write_outputs(report)
    print(f"[write] {rel(OUT_JSON)}")
    print(f"[write] {rel(OUT_MD)}")
    print(f"[write] {rel(OUT_DESIGN_MD)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
