"""Wave 10.1: 向量 collection 健康检查。

目标:
1. 明确 `personal_events` 与 `conversation_turns` 当前是否存在、是否可读。
2. 检查 count / metadata 覆盖率 / 空文档比例 / source 分布。
3. 对比上游源数据,识别“collection 存在但已过期”的问题。

输出:
  integration/analysis/ai_context/vector_collection_health.json
  integration/analysis/ai_context/vector_collection_health.md

用法:
  python integration\\scripts\\evaluate_vector_collections.py
  python integration\\scripts\\evaluate_vector_collections.py --write
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from personal_knowledge.core.conversation_turn_units import load_turn_units as turns_mod
from personal_knowledge.retrieval import build_vector_store as personal_mod
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError


ROOT = Path(__file__).resolve().parents[3]
AI_DIR = ROOT / "integration" / "analysis" / "ai_context"
OUT_JSON = AI_DIR / "vector_collection_health.json"
OUT_MD = AI_DIR / "vector_collection_health.md"
SUMMARIES_JSON = AI_DIR / "conversation_summaries.json"
SQLITE_DB = ROOT / "integration" / "db" / "personal_system.sqlite"

REQUIRED_FIELDS = {
    "personal_events": ["source", "event_type", "service", "category_v2", "title"],
    "conversation_turns": ["session_id", "turn_no", "main_topic", "source", "event_type"],
}


def load_summary_stats() -> dict:
    if not SUMMARIES_JSON.exists():
        return {"available": False}
    data = json.loads(SUMMARIES_JSON.read_text(encoding="utf-8"))
    by_source = Counter(s.get("meta", {}).get("source", "unknown") for s in data)
    return {
        "available": True,
        "session_count": len(data),
        "turn_count": sum(len(s.get("turn_summaries", [])) for s in data),
        "sessions_by_source": dict(sorted(by_source.items())),
    }


def load_expected_counts() -> dict:
    rows = personal_mod.load_events()
    vectorizable_rows, skipped_personal = personal_mod.filter_vectorizable(rows)

    loaded = turns_mod.load_turn_units()
    if not loaded:
        turn_units, skipped_turns = [], 0
    else:
        turn_units, skipped_turns = loaded

    return {
        "personal_events": {
            "upstream_total": len(rows),
            "expected_count": len(vectorizable_rows),
            "skipped_short": skipped_personal,
        },
        "conversation_turns": {
            "upstream_total": len(turn_units) + skipped_turns,
            "expected_count": len(turn_units),
            "skipped_short": skipped_turns,
        },
    }


def load_sqlite_counts() -> dict:
    out = {"db_exists": SQLITE_DB.exists()}
    if not SQLITE_DB.exists():
        return out
    con = sqlite3.connect(SQLITE_DB)
    try:
        for table in (
            "unified_events",
            "unified_events_rich",
            "conversation_sessions",
            "conversation_turns_summary",
        ):
            try:
                out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                out[table] = f"ERROR: {exc}"
    finally:
        con.close()
    return out


def sample_collection(client: ChromaClient, name: str, limit: int = 200) -> dict:
    info = {
        "name": name,
        "exists": False,
        "available": False,
        "count": 0,
        "dimension": None,
        "sample_size": 0,
        "required_metadata_coverage": {},
        "empty_document_count": 0,
        "short_document_count": 0,
        "sample_source_distribution": {},
    }
    coll_info = client._find_collection_by_name(name)
    if not coll_info:
        return info

    info["exists"] = True
    info["dimension"] = coll_info.get("dimension")
    coll = client.get_or_create_collection(name)
    info["count"] = coll.count()
    raw = coll.get(limit=limit, include=["documents", "metadatas"])
    docs = raw.get("documents") or []
    metas = raw.get("metadatas") or []
    info["available"] = True
    info["sample_size"] = min(len(docs), len(metas))

    counter = Counter()
    for meta in metas[: info["sample_size"]]:
        counter[str(meta.get("source") or "unknown")] += 1
    info["sample_source_distribution"] = dict(sorted(counter.items()))

    for field in REQUIRED_FIELDS.get(name, []):
        present = sum(1 for meta in metas[: info["sample_size"]] if str(meta.get(field) or "").strip())
        denom = info["sample_size"] or 1
        info["required_metadata_coverage"][field] = round(present / denom, 4)

    for doc in docs[: info["sample_size"]]:
        text = (doc or "").strip()
        if not text:
            info["empty_document_count"] += 1
        if len(text) < 20:
            info["short_document_count"] += 1
    return info


def finalize_report(report: dict) -> dict:
    """把原始采样结果收敛为明确的健康结论和动作建议。"""
    report["status"] = "healthy"
    report["blocked"] = False
    actions: list[str] = []
    issues: list[str] = []

    chroma = report["chroma"]
    collections = chroma["collections"]

    if not chroma["available"]:
        report["status"] = "blocked"
        report["blocked"] = True
        issues.append("chroma_unavailable")
        actions.append(
            "Chroma live check failed，当前向量库状态为 blocked；先恢复 127.0.0.1:8001 的 Chroma，再重跑 `python -m personal_knowledge.retrieval.evaluate_vector_collections --write`。"
        )
        report["issues"] = issues
        report["actions"] = actions
        return report

    for name in ("personal_events", "conversation_turns"):
        info = collections.get(name, {})
        if not info.get("exists"):
            report["status"] = "unhealthy"
            issues.append(f"missing_collection:{name}")
            rebuild_cmd = (
                "python -m personal_knowledge.retrieval.build_vector_store --write"
                if name == "personal_events"
                else "python -m personal_knowledge.application.conversation.build_conversation_vector_store --write"
            )
            actions.append(f"{name} collection 不存在，需重建：`{rebuild_cmd}`。")
            continue
        if info.get("count_match") is False:
            report["status"] = "unhealthy"
            issues.append(f"count_mismatch:{name}")
            if name == "conversation_turns":
                actions.append(
                    "conversation_turns 与 conversation_summaries.json 不一致，需重跑 `python -m personal_knowledge.application.conversation.build_conversation_vector_store --write`。"
                )
            else:
                actions.append(
                    "personal_events 与当前上游事件计数不一致，需重跑 `python -m personal_knowledge.retrieval.build_vector_store --write`。"
                )

    sqlite_turns = report["sqlite"].get("conversation_turns_summary")
    expected_turns = report["expected"]["conversation_turns"]["expected_count"]
    if isinstance(sqlite_turns, int) and sqlite_turns != expected_turns:
        report["status"] = "unhealthy"
        issues.append("sqlite_turns_summary_stale")
        actions.append(
            "SQLite conversation_turns_summary 已过期，需同步 `python -m personal_knowledge.application.graph.build_triple_store --write --only sqlite`。"
        )

    if not actions:
        actions.append("两类向量 collection 与当前上游计数一致，live 检查通过，无需回灌。")

    report["issues"] = issues
    report["actions"] = actions
    return report


def evaluate() -> dict:
    report = {
        "summary": load_summary_stats(),
        "sqlite": load_sqlite_counts(),
        "expected": load_expected_counts(),
        "chroma": {
            "available": False,
            "heartbeat_ns": None,
            "collections": {},
            "errors": [],
        },
        "actions": [],
    }

    try:
        client = ChromaClient()
        report["chroma"]["heartbeat_ns"] = client.heartbeat()
        report["chroma"]["available"] = True
        for name in ("personal_events", "conversation_turns"):
            info = sample_collection(client, name)
            expected = report["expected"].get(name, {}).get("expected_count")
            if expected is not None:
                info["expected_count"] = expected
                info["count_gap"] = info["count"] - expected
                info["count_match"] = info["count"] == expected
            report["chroma"]["collections"][name] = info
    except ChromaError as exc:
        report["chroma"]["errors"].append(f"ChromaError: {exc}")
    except Exception as exc:
        report["chroma"]["errors"].append(f"{type(exc).__name__}: {exc}")

    return finalize_report(report)


def render_md(report: dict) -> str:
    lines = ["# Vector Collection Health", ""]
    lines += [
        f"- status: {report.get('status', 'unknown')}",
        f"- blocked: {report.get('blocked', False)}",
        "",
    ]

    summary = report["summary"]
    if summary.get("available"):
        lines += [
            f"- conversation_summaries.json: {summary['session_count']} sessions / {summary['turn_count']} turns",
            f"- sessions_by_source: {json.dumps(summary['sessions_by_source'], ensure_ascii=False)}",
            "",
        ]

    sqlite = report["sqlite"]
    lines += ["## SQLite", ""]
    lines += [
        f"- unified_events: {sqlite.get('unified_events', 'N/A')}",
        f"- unified_events_rich: {sqlite.get('unified_events_rich', 'N/A')}",
        f"- conversation_sessions: {sqlite.get('conversation_sessions', 'N/A')}",
        f"- conversation_turns_summary: {sqlite.get('conversation_turns_summary', 'N/A')}",
        "",
    ]

    lines += ["## Chroma", ""]
    lines += [f"- available: {report['chroma']['available']}"]
    if report["chroma"]["heartbeat_ns"] is not None:
        lines += [f"- heartbeat_ns: {report['chroma']['heartbeat_ns']}"]
    if report["chroma"]["errors"]:
        lines += [f"- errors: {json.dumps(report['chroma']['errors'], ensure_ascii=False)}", ""]

    lines += [
        "| collection | exists | count | expected | gap | sample | empty_docs | short_docs | key_meta_coverage |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in ("personal_events", "conversation_turns"):
        info = report["chroma"]["collections"].get(name, {})
        cov = ", ".join(
            f"{k}={v:.0%}" for k, v in info.get("required_metadata_coverage", {}).items()
        ) or "-"
        lines.append(
            f"| {name} | {info.get('exists', False)} | {info.get('count', 0)} | "
            f"{info.get('expected_count', 'N/A')} | {info.get('count_gap', 'N/A')} | "
            f"{info.get('sample_size', 0)} | {info.get('empty_document_count', 0)} | "
            f"{info.get('short_document_count', 0)} | {cov} |"
        )
    lines.append("")

    for name in ("personal_events", "conversation_turns"):
        info = report["chroma"]["collections"].get(name, {})
        if info:
            lines += [
                f"### {name}",
                "",
                f"- sample_source_distribution: {json.dumps(info.get('sample_source_distribution', {}), ensure_ascii=False)}",
                "",
            ]

    lines += ["## Actions", ""]
    for action in report["actions"]:
        lines.append(f"- {action}")
    if report.get("issues"):
        lines.append("")
        lines.append("## Issues")
        lines.append("")
        for issue in report["issues"]:
            lines.append(f"- {issue}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wave 10.1 向量 collection 健康检查")
    parser.add_argument("--write", action="store_true", help="把报告写入 ai_context")
    args = parser.parse_args(argv)

    report = evaluate()
    md = render_md(report)
    print(md)

    if args.write:
        AI_DIR.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        OUT_MD.write_text(md, encoding="utf-8")
        print(f"[write] {OUT_JSON.relative_to(ROOT)}")
        print(f"[write] {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
