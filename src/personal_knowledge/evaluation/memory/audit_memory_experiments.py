"""Wave 1: inventory current memory experiments.

目标:
1. 盘点第一代规则记忆实验层与第二代 LLM 对话图谱实验层。
2. 扫描 SQLite / DuckDB / Chroma 的真实对象计数与 schema 摘要。
3. 扫描scripts引用，提取 memory / graph / vector 的 reader / writer 线索。
4. 输出 inventory JSON / Markdown，给后续对比和删减提供事实基线。

用法:
  python -m personal_knowledge.evaluation.memory.audit_memory_experiments
  python -m personal_knowledge.evaluation.memory.audit_memory_experiments --write
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.chroma_client import ChromaClient, ChromaError
from personal_knowledge.core.project_paths import (
    ROOT,
    PACKAGE_DIR,
    AI_CONTEXT_DIR,
    UNIFIED_DB,
    CONV_GRAPH_DB,
)

try:
    import duckdb  # type: ignore
except Exception as exc:  # pragma: no cover - exercised through fallback path
    duckdb = None
    DUCKDB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    DUCKDB_IMPORT_ERROR = ""


SCRIPTS_DIR = PACKAGE_DIR
AI_DIR = AI_CONTEXT_DIR
SQLITE_DB = UNIFIED_DB
DUCKDB_DB = CONV_GRAPH_DB
OUT_JSON = AI_DIR / "memory_experiment_inventory.json"
OUT_MD = AI_DIR / "memory_experiment_inventory.md"

FIRST_GEN = "first_gen_rule_memory_experiment"
SECOND_GEN = "second_gen_llm_conversation_graph_experiment"

SQLITE_OBJECTS = {
    "memory_items": {
        "layer": FIRST_GEN,
        "write_mode": "replace_by_memory_type",
        "owner_scripts": [
            "build_memory_store.py",
            "build_capability_memory.py",
            "build_context_memory.py",
            "build_preference_memory.py",
        ],
        "description": "第一代规则筛出的长期记忆候选主表。",
    },
    "memory_links": {
        "layer": FIRST_GEN,
        "write_mode": "replace_by_memory_type",
        "owner_scripts": [
            "build_memory_store.py",
            "build_capability_memory.py",
            "build_context_memory.py",
            "build_preference_memory.py",
        ],
        "description": "memory -> event 的证据桥表。",
    },
    "memory_relations": {
        "layer": FIRST_GEN,
        "write_mode": "full_rebuild",
        "owner_scripts": ["build_memory_graph.py"],
        "description": "第一代规则记忆图关系表。",
    },
    "conversation_sessions": {
        "layer": SECOND_GEN,
        "write_mode": "full_refresh_from_conversation_summaries",
        "owner_scripts": ["build_triple_store.py"],
        "description": "turn 级叙述的 session 摘要表。",
    },
    "conversation_turns_summary": {
        "layer": SECOND_GEN,
        "write_mode": "full_refresh_from_conversation_summaries",
        "owner_scripts": ["build_triple_store.py"],
        "description": "turn 级叙述摘要表。",
    },
    "graph_relation_candidates": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild",
        "owner_scripts": ["build_graph_relation_candidates.py"],
        "description": "Wave 9 候选关系表。",
    },
    "graph_relation_judgments": {
        "layer": SECOND_GEN,
        "write_mode": "insert_or_replace_by_candidate",
        "owner_scripts": ["judge_graph_relations.py"],
        "description": "LLM 判边结果表。",
    },
    "graph_relation_review_queue": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild_from_judgments",
        "owner_scripts": ["evaluate_graph_relation_judgments.py"],
        "description": "evidence gate 之后需要 review 的队列表。",
    },
}

DUCKDB_OBJECTS = {
    "g_turn": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild_from_accepted_judgments",
        "owner_scripts": ["build_conversation_graph.py"],
        "description": "accepted graph 的 turn 节点表。",
    },
    "g_session": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild_from_accepted_judgments",
        "owner_scripts": ["build_conversation_graph.py"],
        "description": "accepted graph 的 session 节点表。",
    },
    "g_topic": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild_from_accepted_judgments",
        "owner_scripts": ["build_conversation_graph.py"],
        "description": "accepted graph 的 topic 节点表。",
    },
    "g_tool": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild_from_accepted_judgments",
        "owner_scripts": ["build_conversation_graph.py"],
        "description": "accepted graph 的 tool 节点表。",
    },
    "e_relation": {
        "layer": SECOND_GEN,
        "write_mode": "full_rebuild_from_accepted_judgments",
        "owner_scripts": ["build_conversation_graph.py"],
        "description": "accepted graph 的关系边表。",
    },
}

CHROMA_OBJECTS = {
    "personal_events": {
        "layer": FIRST_GEN,
        "write_mode": "collection_rebuild_resume",
        "owner_scripts": ["build_vector_store.py"],
        "description": "第一代 unified events 向量库。",
    },
    "conversation_turns": {
        "layer": SECOND_GEN,
        "write_mode": "collection_rebuild",
        "owner_scripts": ["build_conversation_vector_store.py"],
        "description": "turn 叙述向量库。",
    },
}

SCRIPT_ENTRIES = [
    {
        "script": "run_pipeline.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "entrypoint",
        "notes": "主链默认执行步骤 1-12，conversation_turns 回流需显式 opt-in。",
    },
    {
        "script": "build_memory_store.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "写 memory_items/memory_links(memory_type=tooling)。",
    },
    {
        "script": "build_capability_memory.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "写 capability 类 memory_items/memory_links。",
    },
    {
        "script": "build_context_memory.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "写 fact/project/habit 类 memory_items/memory_links。",
    },
    {
        "script": "build_preference_memory.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "写 preference 类 memory_items/memory_links。",
    },
    {
        "script": "build_memory_graph.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "重建 memory_relations。",
    },
    {
        "script": "build_vector_store.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "写 Chroma personal_events。",
    },
    {
        "script": "build_context_doc.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "reader",
        "notes": "从 personal_events 派生长期上下文文档。",
    },
    {
        "script": "build_profile_from_memory.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "reader",
        "notes": "从 memory_items/memory_links/memory_relations 派生 profile 文档。",
    },
    {
        "script": "search_vectors.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "reader",
        "notes": "共享向量检索入口；也读取 conversation_turns。",
    },
    {
        "script": "unified_search.py",
        "layer": FIRST_GEN,
        "classification": "active",
        "role": "entrypoint",
        "notes": "当前检索入口，同时读 memory_items 和两类向量 collection。",
    },
    {
        "script": "build_conversation_vector_store.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "写 Chroma conversation_turns。",
    },
    {
        "script": "evaluate_vector_collections.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "auditor",
        "notes": "核对 personal_events / conversation_turns live 状态。",
    },
    {
        "script": "evaluate_vector_retrieval.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "auditor",
        "notes": "评估向量检索效果。",
    },
    {
        "script": "build_graph_relation_candidates.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "从 conversation_turns 生成 graph_relation_candidates。",
    },
    {
        "script": "judge_graph_relations.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "LLM 判边写 graph_relation_judgments。",
    },
    {
        "script": "evaluate_graph_relation_judgments.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "做 evidence gate 并维护 review queue。",
    },
    {
        "script": "build_conversation_graph.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "仅以 accepted judgments 重建 conversation_graph.duckdb。",
    },
    {
        "script": "query_conversation_graph.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "reader",
        "notes": "对 accepted graph 做 smoke query。",
    },
    {
        "script": "visualize_conversation_graph.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "reader",
        "notes": "读取 accepted graph 导出 HTML 可视化。",
    },
    {
        "script": "build_triple_store.py",
        "layer": SECOND_GEN,
        "classification": "active",
        "role": "writer",
        "notes": "SQLite + Chroma 路径仍在用；DuckDB pseudo-graph path 已 deprecated。",
    },
    {
        "script": "build_mem0_candidate_memory.py",
        "layer": SECOND_GEN,
        "classification": "deprecated",
        "role": "writer",
        "notes": "Phase 07 已把 mem0 降级为可选实验，不在主路径。",
    },
    {
        "script": "build_conversation_segments.py",
        "layer": SECOND_GEN,
        "classification": "deprecated",
        "role": "upstream",
        "notes": "仅服务于 mem0 候选实验，不在当前主线。",
    },
]

FILE_ENTRIES = [
    {
        "path": "integration/analysis/ai_context/conversation_summaries.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "build_conversation_summary.py",
        "notes": "第二代实验的上游语料基线。",
    },
    {
        "path": "integration/analysis/ai_context/conversation_quality_report.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "evaluate_conversation_quality.py",
        "notes": "conversation summary 质量门禁报告。",
    },
    {
        "path": "integration/analysis/ai_context/vector_collection_health.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "evaluate_vector_collections.py",
        "notes": "向量 collection live 健康报告。",
    },
    {
        "path": "integration/analysis/ai_context/vector_retrieval_eval_report.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "evaluate_vector_retrieval.py",
        "notes": "向量检索评估报告。",
    },
    {
        "path": "integration/analysis/ai_context/graph_relation_candidates_report.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "build_graph_relation_candidates.py",
        "notes": "候选生成统计报告。",
    },
    {
        "path": "integration/analysis/ai_context/graph_relation_judgments_report.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "judge_graph_relations.py",
        "notes": "LLM 判边结果统计。",
    },
    {
        "path": "integration/analysis/ai_context/graph_relation_eval_report.json",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "evaluate_graph_relation_judgments.py",
        "notes": "accepted / review / rejected gate 报告。",
    },
    {
        "path": "integration/analysis/ai_context/conversation_graph.html",
        "layer": SECOND_GEN,
        "classification": "active",
        "source_script": "visualize_conversation_graph.py",
        "notes": "accepted graph 的可视化工件。",
    },
    {
        "path": "integration/analysis/ai_context/mem0_candidate_memories.json",
        "layer": SECOND_GEN,
        "classification": "deprecated",
        "source_script": "build_mem0_candidate_memory.py",
        "notes": "mem0 可选实验产物，已不在主线。",
    },
    {
        "path": "integration/analysis/ai_context/mem0_candidate_evaluation.md",
        "layer": SECOND_GEN,
        "classification": "deprecated",
        "source_script": "build_mem0_candidate_memory.py",
        "notes": "mem0 实验评估文档，保留作审计记录。",
    },
    {
        "path": "integration/analysis/ai_context/deep_memory_mining.json",
        "layer": FIRST_GEN,
        "classification": "active",
        "source_script": "mine_deep_memory_graph.py",
        "notes": "第一代记忆图谱洞察报告。",
    },
    {
        "path": "integration/analysis/ai_context/deep_memory_insights.json",
        "layer": FIRST_GEN,
        "classification": "active",
        "source_script": "evaluate_memory_depth.py",
        "notes": "第一代记忆深度分析产物。",
    },
    {
        "path": "integration/analysis/ai_context/person_profile.md",
        "layer": FIRST_GEN,
        "classification": "active",
        "source_script": "build_context_doc.py",
        "notes": "第一代长期上下文文档。",
    },
    {
        "path": "integration/analysis/ai_context/person_profile_v2.md",
        "layer": FIRST_GEN,
        "classification": "active",
        "source_script": "build_profile_from_memory.py",
        "notes": "第一代记忆图谱版长期上下文文档。",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def schema_summary(columns: list[tuple]) -> dict:
    items = []
    for col in columns:
        items.append(
            {
                "name": str(col[1]),
                "type": str(col[2] or ""),
                "notnull": bool(col[3]),
                "default": col[4],
                "pk": bool(col[5]),
            }
        )
    short = ", ".join(f"{item['name']}:{item['type'] or '?'}" for item in items[:8])
    if len(items) > 8:
        short += f" ... (+{len(items) - 8} cols)"
    return {"column_count": len(items), "columns": items, "summary": short}


def normalize_list(values: list[str]) -> list[str]:
    return sorted(dict.fromkeys(v for v in values if v))


def classify_object_status(name: str, readers: list[str], writers: list[str], explicit_status: str | None = None) -> str:
    if explicit_status:
        return explicit_status
    if name == "graph_relation_review_queue" and not readers:
        return "remove_candidate"
    return "active"


def compute_overall_status(reports: list[dict]) -> str:
    states = [report.get("status") for report in reports if report.get("status")]
    if "blocked" in states:
        return "blocked"
    if "degraded" in states:
        return "degraded"
    return "healthy"


def scan_sqlite_tables() -> dict:
    report = {
        "path": str(SQLITE_DB),
        "exists": SQLITE_DB.exists(),
        "status": "healthy",
        "objects": [],
        "issues": [],
        "actions": [],
    }
    if not SQLITE_DB.exists():
        report["status"] = "blocked"
        report["issues"].append("sqlite_missing")
        report["actions"].append(f"缺少 SQLite 数据库：{SQLITE_DB}")
        return report

    con = sqlite3.connect(SQLITE_DB)
    try:
        for table_name, meta in SQLITE_OBJECTS.items():
            row = {
                "name": table_name,
                "kind": "sqlite_table",
                "layer": meta["layer"],
                "description": meta["description"],
                "write_mode": meta["write_mode"],
                "owner_scripts": meta["owner_scripts"],
                "exists": False,
                "count": None,
                "schema": {"column_count": 0, "columns": [], "summary": ""},
                "extras": {},
            }
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if not exists:
                report["status"] = "degraded"
                report["issues"].append(f"missing_table:{table_name}")
                report["actions"].append(f"缺少 SQLite 表 `{table_name}`，需补相应 build scripts输出。")
                report["objects"].append(row)
                continue

            row["exists"] = True
            row["count"] = int(con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
            row["schema"] = schema_summary(con.execute(f"PRAGMA table_info({table_name})").fetchall())
            if table_name == "memory_items":
                by_type = con.execute(
                    "SELECT memory_type, COUNT(*) c FROM memory_items GROUP BY 1 ORDER BY c DESC, memory_type"
                ).fetchall()
                row["extras"]["memory_type_breakdown"] = {name: count for name, count in by_type}
            if table_name == "graph_relation_judgments":
                by_gate = con.execute(
                    "SELECT gate_status, COUNT(*) c FROM graph_relation_judgments GROUP BY 1 ORDER BY c DESC, gate_status"
                ).fetchall()
                row["extras"]["gate_status_breakdown"] = {name or "": count for name, count in by_gate}
            report["objects"].append(row)
    finally:
        con.close()
    return report


def scan_duckdb_tables() -> dict:
    report = {
        "path": str(DUCKDB_DB),
        "exists": DUCKDB_DB.exists(),
        "status": "healthy",
        "objects": [],
        "issues": [],
        "actions": [],
    }
    if duckdb is None:
        report["status"] = "blocked"
        report["issues"].append(f"duckdb_import_error:{DUCKDB_IMPORT_ERROR}")
        report["actions"].append(
            "DuckDB Python 模块不可用；先恢复 duckdb 运行环境，再重跑 `python -m personal_knowledge.evaluation.memory.audit_memory_experiments --write`。"
        )
        return report
    if not DUCKDB_DB.exists():
        report["status"] = "blocked"
        report["issues"].append("duckdb_missing")
        report["actions"].append(f"缺少 DuckDB 数据库：{DUCKDB_DB}")
        return report

    con = duckdb.connect(str(DUCKDB_DB), read_only=True)
    try:
        available = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        for table_name, meta in DUCKDB_OBJECTS.items():
            row = {
                "name": table_name,
                "kind": "duckdb_table",
                "layer": meta["layer"],
                "description": meta["description"],
                "write_mode": meta["write_mode"],
                "owner_scripts": meta["owner_scripts"],
                "exists": table_name in available,
                "count": None,
                "schema": {"column_count": 0, "columns": [], "summary": ""},
                "extras": {},
            }
            if table_name not in available:
                report["status"] = "blocked"
                report["issues"].append(f"missing_duckdb_table:{table_name}")
                report["actions"].append(
                    f"DuckDB 缺少 `{table_name}`，需检查 `build_conversation_graph.py --write` 是否完成。"
                )
                report["objects"].append(row)
                continue

            row["count"] = int(con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
            desc = con.execute(f"DESCRIBE {table_name}").fetchall()
            columns = [(idx, d[0], d[1], False, None, False) for idx, d in enumerate(desc)]
            row["schema"] = schema_summary(columns)
            if table_name == "e_relation":
                by_type = con.execute(
                    "SELECT relation_type, COUNT(*) c FROM e_relation GROUP BY 1 ORDER BY c DESC, relation_type"
                ).fetchall()
                row["extras"]["relation_type_breakdown"] = {name: count for name, count in by_type}
            report["objects"].append(row)
    finally:
        con.close()
    return report


def sample_chroma_collection(client: ChromaClient, name: str) -> dict:
    meta = CHROMA_OBJECTS[name]
    row = {
        "name": name,
        "kind": "chroma_collection",
        "layer": meta["layer"],
        "description": meta["description"],
        "write_mode": meta["write_mode"],
        "owner_scripts": meta["owner_scripts"],
        "exists": False,
        "count": None,
        "dimension": None,
        "schema_keys": [],
        "sample_metadata": {},
        "extras": {},
    }

    coll_info = client._find_collection_by_name(name)
    if not coll_info:
        return row

    row["exists"] = True
    row["dimension"] = coll_info.get("dimension")
    coll = client.get_or_create_collection(name)
    row["count"] = int(coll.count())
    raw = coll.get(limit=1, include=["metadatas"])
    metas = raw.get("metadatas") or []
    sample = metas[0] if metas else {}
    row["sample_metadata"] = sample
    row["schema_keys"] = sorted(sample.keys()) if isinstance(sample, dict) else []
    return row


def scan_chroma_collections() -> dict:
    report = {
        "host": "127.0.0.1",
        "port": 8001,
        "status": "healthy",
        "blocked": False,
        "heartbeat_ns": None,
        "objects": [],
        "issues": [],
        "actions": [],
    }
    try:
        client = ChromaClient()
        report["heartbeat_ns"] = client.heartbeat()
        for name in CHROMA_OBJECTS:
            row = sample_chroma_collection(client, name)
            if not row["exists"]:
                report["status"] = "degraded"
                report["issues"].append(f"missing_collection:{name}")
                rebuild = (
                    "python -m personal_knowledge.retrieval.build_vector_store --write"
                    if name == "personal_events"
                    else "python -m personal_knowledge.application.conversation.build_conversation_vector_store --write"
                )
                report["actions"].append(f"缺少 Chroma collection `{name}`，需重建：`{rebuild}`。")
            report["objects"].append(row)
    except ChromaError as exc:
        report["status"] = "blocked"
        report["blocked"] = True
        report["issues"].append(f"chroma_error:{exc}")
        report["actions"].append(
            "Chroma 不可用；先恢复 127.0.0.1:8001 服务，再重跑 `python -m personal_knowledge.evaluation.memory.audit_memory_experiments --write`。"
        )
    except Exception as exc:  # pragma: no cover - safety net
        report["status"] = "blocked"
        report["blocked"] = True
        report["issues"].append(f"chroma_unexpected:{type(exc).__name__}: {exc}")
        report["actions"].append(
            "Chroma inventory 发生异常；先修复本机 Chroma 访问，再重跑 inventory。"
        )
    return report


def infer_sql_roles(text: str, target_name: str) -> tuple[bool, bool]:
    escaped = re.escape(target_name)
    is_writer = any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in [
            rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{escaped}\b",
            rf"INSERT(?:\s+OR\s+(?:REPLACE|IGNORE))?\s+INTO\s+{escaped}\b",
            rf"DELETE\s+FROM\s+{escaped}\b",
            rf"UPDATE\s+{escaped}\b",
        ]
    )
    is_reader = any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in [
            rf"SELECT[\s\S]{{0,240}}?FROM\s+{escaped}\b",
            rf"JOIN\s+{escaped}\b",
            rf"COUNT\(\*\)\s+FROM\s+{escaped}\b",
            rf"PRAGMA\s+table_info\({escaped}\)",
        ]
    )
    return is_reader, is_writer


def infer_collection_roles(text: str, target_name: str) -> tuple[bool, bool]:
    if f'"{target_name}"' not in text and f"'{target_name}'" not in text:
        return False, False
    is_writer = bool(
        re.search(r"\.(add|upsert)\(", text)
        or re.search(r"delete_collection_by_name\(", text)
        or re.search(r"delete_collection\(", text)
    )
    is_reader = bool(
        re.search(r"\.(query|get|count)\(", text)
        or re.search(r"list_collections\(", text)
        or re.search(r"_find_collection_by_name\(", text)
    )
    return is_reader, is_writer


def scan_script_references() -> dict:
    targets = list(SQLITE_OBJECTS) + list(DUCKDB_OBJECTS) + list(CHROMA_OBJECTS)
    scan = {
        "scan_root": str(SCRIPTS_DIR),
        "targets": {
            name: {
                "reader_scripts": [],
                "writer_scripts": [],
                "references": [],
            }
            for name in targets
        },
    }

    for path in sorted(SCRIPTS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for name in targets:
            refs = []
            for idx, line in enumerate(lines, 1):
                if name in line:
                    refs.append({"script": path.name, "line": idx, "snippet": line.strip()[:180]})
            if not refs:
                continue

            if name in CHROMA_OBJECTS:
                is_reader, is_writer = infer_collection_roles(text, name)
            else:
                is_reader, is_writer = infer_sql_roles(text, name)

            if is_reader:
                scan["targets"][name]["reader_scripts"].append(path.name)
            if is_writer:
                scan["targets"][name]["writer_scripts"].append(path.name)
            scan["targets"][name]["references"].extend(refs[:6])

    for info in scan["targets"].values():
        info["reader_scripts"] = normalize_list(info["reader_scripts"])
        info["writer_scripts"] = normalize_list(info["writer_scripts"])
    return scan


def build_file_inventory() -> list[dict]:
    items = []
    for entry in FILE_ENTRIES:
        path = ROOT / Path(entry["path"])
        item = dict(entry)
        item["exists"] = path.exists()
        item["size_bytes"] = path.stat().st_size if path.exists() else 0
        items.append(item)
    return items


def build_script_inventory(reference_scan: dict) -> list[dict]:
    scripts = []
    by_target = reference_scan["targets"]
    for entry in SCRIPT_ENTRIES:
        item = dict(entry)
        reads: list[str] = []
        writes: list[str] = []
        evidence: list[dict] = []
        for target_name, target_info in by_target.items():
            if item["script"] in target_info["reader_scripts"]:
                reads.append(target_name)
            if item["script"] in target_info["writer_scripts"]:
                writes.append(target_name)
            for ref in target_info["references"]:
                if ref["script"] == item["script"]:
                    evidence.append({"target": target_name, **ref})
        item["reads"] = normalize_list(reads)
        item["writes"] = normalize_list(writes)
        item["evidence"] = evidence[:10]
        scripts.append(item)
    return scripts


def build_store_inventory(store_report: dict, reference_scan: dict) -> list[dict]:
    objects = []
    for row in store_report["objects"]:
        ref = reference_scan["targets"].get(row["name"], {})
        reader_scripts = [name for name in ref.get("reader_scripts", []) if name != "audit_memory_experiments.py"]
        item = dict(row)
        item["reader_scripts"] = reader_scripts
        item["writer_scripts"] = normalize_list(item.get("owner_scripts", []) + ref.get("writer_scripts", []))
        item["status"] = classify_object_status(
            row["name"],
            item["reader_scripts"],
            item["writer_scripts"],
        )
        item["reference_clues"] = ref.get("references", [])[:10]
        objects.append(item)
    return objects


def summarize_key_counts(sqlite_report: dict, duckdb_report: dict, chroma_report: dict) -> dict:
    counts = {}
    for report in (sqlite_report, duckdb_report, chroma_report):
        for item in report["objects"]:
            if item.get("count") is not None:
                counts[item["name"]] = item["count"]
    return counts


def build_remove_candidates(store_objects: list[dict], script_inventory: list[dict], file_inventory: list[dict]) -> list[dict]:
    candidates = []
    for item in store_objects:
        if item["status"] == "remove_candidate":
            reason = "no automated reader detected"
            if item["name"] == "graph_relation_review_queue":
                reason = "queue has writer but no automated reader; currently only acts as audit spillover"
            candidates.append(
                {
                    "kind": item["kind"],
                    "name": item["name"],
                    "layer": item["layer"],
                    "reason": reason,
                }
            )
    for item in script_inventory:
        if item["classification"] in {"deprecated", "remove_candidate"}:
            candidates.append(
                {
                    "kind": "script",
                    "name": item["script"],
                    "layer": item["layer"],
                    "reason": item["notes"],
                }
            )
    for item in file_inventory:
        if item["classification"] in {"deprecated", "remove_candidate"}:
            candidates.append(
                {
                    "kind": "file",
                    "name": item["path"],
                    "layer": item["layer"],
                    "reason": item["notes"],
                }
            )
    return candidates


def build_inventory() -> dict:
    reference_scan = scan_script_references()
    sqlite_report = scan_sqlite_tables()
    duckdb_report = scan_duckdb_tables()
    chroma_report = scan_chroma_collections()

    sqlite_objects = build_store_inventory(sqlite_report, reference_scan)
    duckdb_objects = build_store_inventory(duckdb_report, reference_scan)
    chroma_objects = build_store_inventory(chroma_report, reference_scan)
    script_inventory = build_script_inventory(reference_scan)
    file_inventory = build_file_inventory()
    all_store_objects = sqlite_objects + duckdb_objects + chroma_objects

    issues = sqlite_report["issues"] + duckdb_report["issues"] + chroma_report["issues"]
    actions = sqlite_report["actions"] + duckdb_report["actions"] + chroma_report["actions"]
    overall_status = compute_overall_status([sqlite_report, duckdb_report, chroma_report])

    first_gen_objects = [item for item in all_store_objects if item["layer"] == FIRST_GEN]
    second_gen_objects = [item for item in all_store_objects if item["layer"] == SECOND_GEN]

    inventory = {
        "generated_at": utc_now(),
        "phase": "08",
        "wave": "1",
        "status": overall_status,
        "stores": {
            "sqlite": {**sqlite_report, "objects": sqlite_objects},
            "duckdb": {**duckdb_report, "objects": duckdb_objects},
            "chroma": {**chroma_report, "objects": chroma_objects},
        },
        "layers": {
            FIRST_GEN: {
                "label": "First-gen rule memory experiment",
                "key_counts": {item["name"]: item["count"] for item in first_gen_objects},
                "store_objects": first_gen_objects,
                "scripts": [item for item in script_inventory if item["layer"] == FIRST_GEN],
                "files": [item for item in file_inventory if item["layer"] == FIRST_GEN],
            },
            SECOND_GEN: {
                "label": "Second-gen LLM conversation graph experiment",
                "key_counts": {item["name"]: item["count"] for item in second_gen_objects},
                "store_objects": second_gen_objects,
                "scripts": [item for item in script_inventory if item["layer"] == SECOND_GEN],
                "files": [item for item in file_inventory if item["layer"] == SECOND_GEN],
            },
        },
        "key_counts": summarize_key_counts(sqlite_report, duckdb_report, chroma_report),
        "script_reference_scan": reference_scan,
        "remove_candidates": build_remove_candidates(all_store_objects, script_inventory, file_inventory),
        "issues": issues,
        "actions": actions,
    }
    return inventory


def render_store_table(items: list[dict]) -> list[str]:
    lines = [
        "| Object | Count | Status | Owner | Readers | Write Mode | Schema |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        owner = ", ".join(item.get("owner_scripts", [])) or "-"
        readers = ", ".join(item.get("reader_scripts", [])) or "-"
        count = item.get("count")
        count_text = str(count) if count is not None else "-"
        schema = item.get("schema", {}).get("summary", "") or "-"
        lines.append(
            f"| `{item['name']}` | {count_text} | {item['status']} | `{owner}` | `{readers}` | `{item['write_mode']}` | `{schema}` |"
        )
    return lines


def render_script_table(items: list[dict]) -> list[str]:
    lines = [
        "| Script | Class | Role | Reads | Writes | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        reads = ", ".join(item["reads"]) or "-"
        writes = ", ".join(item["writes"]) or "-"
        lines.append(
            f"| `{item['script']}` | {item['classification']} | {item['role']} | `{reads}` | `{writes}` | {item['notes']} |"
        )
    return lines


def render_file_table(items: list[dict]) -> list[str]:
    lines = [
        "| File | Class | Source Script | Size | Notes |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for item in items:
        lines.append(
            f"| `{item['path']}` | {item['classification']} | `{item['source_script']}` | {item['size_bytes']} | {item['notes']} |"
        )
    return lines


def render_md(inventory: dict) -> str:
    lines = [
        "# Memory Experiment Inventory",
        "",
        f"- generated_at: {inventory['generated_at']}",
        f"- status: {inventory['status']}",
        "- scope: Wave 1 inventory only; no Wave 2-5 comparison/promotion/deletion logic applied.",
        "",
        "## Key Counts",
        "",
    ]
    for name, count in inventory["key_counts"].items():
        lines.append(f"- {name}: {count}")

    lines += ["", "## First-gen Rule Memory Experiment", ""]
    lines += render_store_table(inventory["layers"][FIRST_GEN]["store_objects"])
    lines += ["", "### Scripts", ""]
    lines += render_script_table(inventory["layers"][FIRST_GEN]["scripts"])
    lines += ["", "### Files", ""]
    lines += render_file_table(inventory["layers"][FIRST_GEN]["files"])

    lines += ["", "## Second-gen LLM Conversation Graph Experiment", ""]
    lines += render_store_table(inventory["layers"][SECOND_GEN]["store_objects"])
    lines += ["", "### Scripts", ""]
    lines += render_script_table(inventory["layers"][SECOND_GEN]["scripts"])
    lines += ["", "### Files", ""]
    lines += render_file_table(inventory["layers"][SECOND_GEN]["files"])

    lines += ["", "## Issues", ""]
    if inventory["issues"]:
        lines.extend(f"- {issue}" for issue in inventory["issues"])
    else:
        lines.append("- none")

    lines += ["", "## Actions", ""]
    if inventory["actions"]:
        lines.extend(f"- {action}" for action in inventory["actions"])
    else:
        lines.append("- none")

    lines += ["", "## Remove Candidates", ""]
    if inventory["remove_candidates"]:
        for item in inventory["remove_candidates"]:
            lines.append(f"- `{item['name']}` ({item['kind']}, {item['layer']}): {item['reason']}")
    else:
        lines.append("- none")

    lines += ["", "## Notes", ""]
    lines.append("- `first_gen_rule_memory_experiment` 对应 `memory_items/memory_links/memory_relations + personal_events`。")
    lines.append("- `second_gen_llm_conversation_graph_experiment` 对应 `conversation_turns + graph_relation_* + conversation_graph.duckdb`。")
    lines.append("- mem0 路径保留为审计记录，但按 Phase 07/08 口径已降级为可选实验。")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit current memory experiments.")
    parser.add_argument("--write", action="store_true", help="write inventory JSON/Markdown")
    args = parser.parse_args()

    inventory = build_inventory()
    text = json.dumps(inventory, ensure_ascii=False, indent=2)
    md = render_md(inventory)

    print(f"status: {inventory['status']}")
    for name, count in inventory["key_counts"].items():
        print(f"{name}: {count}")
    if inventory["issues"]:
        print("issues:")
        for issue in inventory["issues"]:
            print(f"  - {issue}")

    if args.write:
        AI_DIR.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(text + "\n", encoding="utf-8")
        OUT_MD.write_text(md, encoding="utf-8")
        print(f"[write] {OUT_JSON}")
        print(f"[write] {OUT_MD}")


if __name__ == "__main__":
    main()
