"""从记忆对象和记忆关系生成 AI 可消费的用户画像 v2。

输出:
  integration/analysis/ai_context/person_profile_v2.md

运行:
  python integration\\scripts\\build_profile_from_memory.py
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
OUT_DIR = ROOT / "integration" / "analysis" / "ai_context"
OUT_MD = OUT_DIR / "person_profile_v2.md"

TYPE_TITLES = {
    "tooling": "工具偏好(tooling)",
    "preference": "内容关注(preference)",
    "capability": "核心能力(capability)",
    "fact": "关键事实(fact)",
    "project": "项目(project)",
    "habit": "工作流习惯(habit)",
}

TYPE_LIMITS = {
    "tooling": 12,
    "preference": 10,
    "capability": 18,
    "fact": 12,
    "project": 10,
    "habit": 10,
}


def load_memories(con: sqlite3.Connection) -> dict[str, list[dict]]:
    con.row_factory = sqlite3.Row
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in con.execute(
        "SELECT memory_id, memory_type, memory_subtype, subject, description, "
        "confidence, evidence_count, metadata, created_at "
        "FROM memory_items "
        "ORDER BY memory_type, evidence_count DESC, confidence DESC, subject"
    ):
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.get("metadata") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        grouped[row["memory_type"]].append(data)
    return grouped


def load_relations(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in con.execute(
            "SELECT mr.relation, mr.strength, "
            "src.memory_type AS from_type, src.subject AS from_subject, "
            "dst.memory_type AS to_type, dst.subject AS to_subject "
            "FROM memory_relations mr "
            "JOIN memory_items src ON src.memory_id = mr.from_memory_id "
            "JOIN memory_items dst ON dst.memory_id = mr.to_memory_id "
            "ORDER BY mr.strength DESC, mr.relation, src.subject "
            "LIMIT ?",
            (limit,),
        )
    ]


def load_evidence_summary(con: sqlite3.Connection, memory_id: str, limit: int = 3) -> list[str]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT ue.source, ue.event_time, ue.title "
        "FROM memory_links ml "
        "JOIN unified_events ue ON ue.event_id = ml.target_id "
        "WHERE ml.memory_id=? AND ml.target_type='event' "
        "ORDER BY ue.event_time DESC, ml.id DESC LIMIT ?",
        (memory_id, limit),
    ).fetchall()
    return [
        f"{row['source']} {str(row['event_time'] or '')[:10]} {str(row['title'] or '(无标题)')[:36]}"
        for row in rows
    ]


def render_section(con: sqlite3.Connection, memory_type: str, items: list[dict]) -> list[str]:
    title = TYPE_TITLES.get(memory_type, memory_type)
    limit = TYPE_LIMITS.get(memory_type, 10)
    lines = [f"## {title}"]
    if not items:
        lines.append("- 暂无。")
        lines.append("")
        return lines

    for item in items[:limit]:
        subtype = item["memory_subtype"]
        evidence = item["evidence_count"]
        confidence = item["confidence"]
        metadata = item.get("metadata") or {}
        last_seen = metadata.get("last_seen") or item.get("created_at") or ""
        evidence_summary = load_evidence_summary(con, item["memory_id"], limit=2)
        lines.append(
            f"- {item['subject']} ({subtype}, 证据 {evidence}, 置信 {confidence}): "
            f"{item['description']}"
        )
        if last_seen:
            lines.append(f"  最近证据时间: {str(last_seen)[:19]}")
        if evidence_summary:
            lines.append(f"  证据摘要: {'; '.join(evidence_summary)}")
    hidden = len(items) - limit
    if hidden > 0:
        lines.append(f"- 另有 {hidden} 条低优先级 {memory_type} 记忆未展开。")
    lines.append("")
    return lines


def build_profile() -> Path:
    if not UNIFIED_DB.exists():
        raise FileNotFoundError(f"统合库不存在: {UNIFIED_DB}")

    con = sqlite3.connect(UNIFIED_DB)
    grouped = load_memories(con)
    relations = load_relations(con, limit=20)
    total = sum(len(v) for v in grouped.values())

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        "# 用户记忆画像(自动生成)",
        "",
        f"- 生成时间: {generated_at}",
        f"- 数据快照: memory_items={total}, memory_relations={len(relations)}(展示Top {min(len(relations), 20)})",
        "- 用途: 可注入 AI system prompt, 让 AI 在回答前理解用户的稳定偏好、项目、能力和工具环境。",
        "",
    ]

    for memory_type in ["tooling", "preference", "capability", "fact", "project", "habit"]:
        lines.extend(render_section(con, memory_type, grouped.get(memory_type, [])))

    lines.append("## 记忆间关系(图谱摘要)")
    if relations:
        for rel in relations[:20]:
            lines.append(
                f"- {rel['from_subject']}[{rel['from_type']}] "
                f"--{rel['relation']}({rel['strength']})--> "
                f"{rel['to_subject']}[{rel['to_type']}]"
            )
    else:
        lines.append("- 暂无关系。")
    lines.append("")
    lines.append("## 使用建议")
    lines.append("- 需要个性化回答时,优先读取 tooling / capability / habit。")
    lines.append("- 需要判断用户长期兴趣时,优先读取 preference / project。")
    lines.append("- 需要解释环境约束时,优先读取 fact。")
    lines.append("- 需要追溯证据时,回到 SQLite 的 memory_links 表查看原始事件。")
    con.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    return OUT_MD


def main() -> None:
    out = build_profile()
    print(f"已生成: {out}")


if __name__ == "__main__":
    main()
