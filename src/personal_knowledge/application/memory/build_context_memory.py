"""上下文记忆抽取scripts(阶段四 Wave 3:project/habit/fact)。

里程碑3:补齐 tooling/preference 之外的三类记忆对象。
  - fact     稳定事实(从 memory_summary.md + 环境信号词)
  - project  项目归属(从 file 路径 + memory 提到的项目路径)
  - habit    工作流习惯(从 search_to_execute_chain 时序链)

v1 修正(基于用户反馈):
  - fact 11条全部"符合" → 保留
  - habit 2条全部"符合" → 保留
  - project 噪音清洗:过滤 UUID/skillID/通用目录(用户标"看不出是什么项目")

运行: python integration\\scripts\\build_context_memory.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_knowledge.core.common import sha256_text, write_json, ensure_dirs
from personal_knowledge.core.memory_governance import build_governance_metadata, load_last_seen, unique_evidence_ids


# === 配置 ===
ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
from personal_knowledge.core.project_paths import STAGE1_PROFILE_DIR as ANALYSIS_DIR  # stage1 reports

RULES_VERSION = "context-v2"  # v2: project 噪音清洗

MAX_EVIDENCE_LINKS = 30


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            memory_id      TEXT PRIMARY KEY,
            memory_type    TEXT NOT NULL,
            memory_subtype TEXT NOT NULL,
            subject        TEXT NOT NULL,
            description    TEXT NOT NULL,
            confidence     REAL DEFAULT 0.5,
            evidence_count INTEGER DEFAULT 0,
            metadata       TEXT,
            created_at     TEXT NOT NULL,
            UNIQUE(memory_type, memory_subtype, subject)
        );
        CREATE TABLE IF NOT EXISTS memory_links (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id    TEXT NOT NULL,
            target_type  TEXT NOT NULL,
            target_id    TEXT NOT NULL,
            relation     TEXT NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memory_items(memory_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_memory_links_memory ON memory_links(memory_id);
        """
    )
    con.commit()


def reset_context_memory(con: sqlite3.Connection) -> None:
    for mtype in ["fact", "project", "habit"]:
        con.execute(
            "DELETE FROM memory_links WHERE memory_id IN "
            f"(SELECT memory_id FROM memory_items WHERE memory_type='{mtype}')"
        )
        con.execute(f"DELETE FROM memory_items WHERE memory_type='{mtype}'")
    con.commit()


def _insert_memory(con, memory_type, subtype, subject, description,
                   evidence_ids, metadata, confidence, now):
    evidence_ids = unique_evidence_ids(evidence_ids, limit=MAX_EVIDENCE_LINKS)
    metadata = build_governance_metadata(
        source=f"{memory_type}:{metadata.get('source', 'derived')}",
        evidence_ids=evidence_ids,
        confidence=confidence,
        merge_key=f"{memory_type}|{subtype}|{subject.lower()}",
        last_seen=load_last_seen(con, evidence_ids),
        extra=metadata,
    )
    memory_id = sha256_text(f"{memory_type}|{subtype}|{subject.lower()}")
    con.execute(
        "INSERT OR REPLACE INTO memory_items "
        "(memory_id, memory_type, memory_subtype, subject, description, "
        " confidence, evidence_count, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, memory_type, subtype, subject, description,
         confidence, len(evidence_ids), json.dumps(metadata, ensure_ascii=False), now)
    )
    for eid in evidence_ids[:MAX_EVIDENCE_LINKS]:
        con.execute(
            "INSERT INTO memory_links (memory_id, target_type, target_id, relation) "
            "VALUES (?, 'event', ?, 'evidenced_by')",
            (memory_id, eid)
        )
    return memory_id


def _find_events_with(con, keyword, limit=50):
    rows = con.execute(
        "SELECT event_id FROM unified_events "
        "WHERE content LIKE ? OR title LIKE ? "
        "ORDER BY month DESC, rowid DESC LIMIT ?",
        (f"%{keyword}%", f"%{keyword}%", limit)
    ).fetchall()
    return [r[0] for r in rows]


# ============================================================
# FACT 抽取(稳定事实)— v1 验证全部符合,保留
# ============================================================

ENV_SIGNALS = [
    ("Windows", "你用 Windows 作为主要工作环境", 30),
    ("Docker", "你重度使用 Docker 容器化", 50),
    ("Linux", "你涉及 Linux 系统操作", 30),
    ("Python", "你的技术栈以 Python 为主", 30),
    ("Obsidian", "你用 Obsidian 做知识管理", 10),
    ("GitHub", "你重度使用 GitHub 做版本管理", 30),
    ("VSCode", "你用 VSCode 作为编辑器", 8),
    ("CUDA", "你有 GPU 计算环境(CUDA)", 5),
]


def build_fact_memory(con: sqlite3.Connection, now: str) -> list:
    facts = []
    rows = con.execute(
        "SELECT ue.event_id, r.content_rich FROM unified_events ue "
        "LEFT JOIN unified_events_rich r ON r.event_id=ue.event_id "
        "WHERE ue.title IN ('memory_summary.md','MEMORY.md','career-memory.md')"
    ).fetchall()
    profile_texts = [(r[0], r[1] or "") for r in rows]
    profile_evidence = [r[0] for r in rows]
    full_profile = " ".join(t for _, t in profile_texts).lower()

    profile_facts = []
    if "execution-first" in full_profile or "act once" in full_profile:
        profile_facts.append(("work_style", "执行优先协作",
            "你的工作方式是执行优先的AI协作——scope清楚就想立刻动手,不要反复确认"))
    if "durable" in full_profile or "one-off" in full_profile:
        profile_facts.append(("system_pref", "偏好持久系统",
            "你偏好建立持久可复用的系统,而非一次性修复"))
    if "audit-first" in full_profile or "milestone" in full_profile:
        profile_facts.append(("approach", "审计优先",
            "你做功能前倾向先审计/建立里程碑,再动手"))
    if "求职" in full_profile or "career" in full_profile or "招聘" in full_profile:
        profile_facts.append(("status", "求职阶段",
            "你处于求职/职业规划阶段(有 career-memory 工作流)"))

    for subtype, subject, desc in profile_facts:
        facts.append({
            "subtype": subtype, "subject": subject, "description": desc,
            "evidence_ids": profile_evidence[:10], "confidence": 0.9,
            "source": "memory_summary.md"
        })

    for keyword, desc, threshold in ENV_SIGNALS:
        count = con.execute(
            "SELECT COUNT(*) FROM unified_events WHERE content LIKE ? OR title LIKE ?",
            (f"%{keyword}%", f"%{keyword}%")
        ).fetchone()[0]
        if count >= threshold:
            ev_ids = _find_events_with(con, keyword, limit=30)
            facts.append({
                "subtype": "env_signal", "subject": keyword,
                "description": f"{desc}(在{count}条事件中出现)",
                "evidence_ids": ev_ids, "confidence": min(0.9, 0.5 + count / 200),
                "source": f"事件含'{keyword}'({count}次)"
            })
    return facts


# ============================================================
# PROJECT 抽取(项目归属)— v2:噪音清洗
# ============================================================

PROJECT_NOISE_DIRS = {
    "skills", "tasks", "work", "data", "workspaces", "agents",
    "references", "apple", "windows", "linux",
}
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
SKILLID_PATTERN = re.compile(r'^skill_\d+$', re.I)
HASH_PATTERN = re.compile(r'^[0-9a-f]{12,}$', re.I)


def _is_valid_project_name(name: str) -> bool:
    """判断名字是否像真实项目名(过滤 UUID/skillID/通用目录)。

    基于用户反馈标注:看不出是什么项目的(UUID/skillID)不该留。
    """
    if not name:
        return False
    n = name.strip().rstrip(".").rstrip("\\").rstrip("/").rstrip("（").rstrip("(")
    if len(n) < 2 or len(n) > 40:
        return False
    if UUID_PATTERN.match(n):
        return False
    if SKILLID_PATTERN.match(n):
        return False
    if HASH_PATTERN.match(n):
        return False
    if n.lower() in PROJECT_NOISE_DIRS:
        return False
    if n.isdigit():
        return False
    # 含 UUID 片段
    if re.search(r'[0-9a-f]{8}-[0-9a-f]{4}', n, re.I):
        return False
    return True


def build_project_memory(con: sqlite3.Connection, now: str) -> list:
    projects = {}

    # --- 来源1: memory_summary.md 里的项目路径(最可信)---
    rows = con.execute(
        "SELECT ue.event_id, r.content_rich FROM unified_events ue "
        "LEFT JOIN unified_events_rich r ON r.event_id=ue.event_id "
        "WHERE ue.title IN ('memory_summary.md','MEMORY.md','career-memory.md')"
    ).fetchall()
    for eid, cr in rows:
        if not cr:
            continue
        for m in re.finditer(r'([A-Za-z]):\\[^\s`,;\)\]]+', cr):
            path = m.group(0)
            parts = path.replace("\\", "/").split("/")
            proj_name = None
            for i, p in enumerate(parts):
                if p in ("Myproject", "Desktop", "Documents", "课程") and i + 1 < len(parts):
                    proj_name = parts[i + 1]
                    break
            if proj_name and _is_valid_project_name(proj_name):
                key = proj_name.lower()
                if key not in projects:
                    projects[key] = {"subject": proj_name, "path": path,
                                     "evidence": [], "source": "memory路径"}
                projects[key]["evidence"].append(eid)

    # --- 来源2: file 实体 evidence 里的项目路径(用过滤规则清洗)---
    frows = con.execute(
        "SELECT name, evidence, event_count FROM entities "
        "WHERE entity_type='file' AND evidence IS NOT NULL AND event_count >= 5"
    ).fetchall()
    for fname, ev, ec in frows:
        if not ev:
            continue
        m = re.search(r'([\w_]+)\s+([\w_]+)\\([^\\\s]+)', ev)
        if m:
            seg2 = m.group(3)
            if seg2 and _is_valid_project_name(seg2):
                key = seg2.lower()
                if key not in projects:
                    projects[key] = {"subject": seg2, "path": ev[:80],
                                     "evidence": [], "source": "file路径"}
                rel_ev = con.execute(
                    "SELECT ee.event_id FROM event_entities ee "
                    "JOIN entities e ON ee.entity_id=e.entity_id "
                    "WHERE e.entity_type='file' AND e.name=? LIMIT 5", (fname,)
                ).fetchall()
                projects[key]["evidence"].extend([r[0] for r in rel_ev])

    result = []
    for key, p in projects.items():
        p["evidence"] = list(set(p["evidence"]))[:MAX_EVIDENCE_LINKS]
        if len(p["evidence"]) >= 1:
            result.append(p)
    return result


# ============================================================
# HABIT 抽取(工作流习惯)— v1 验证全部符合,保留
# ============================================================

def build_habit_memory(con: sqlite3.Connection, now: str) -> list:
    habits = []
    chains = con.execute(
        "SELECT from_event_id, to_event_id, evidence FROM entity_links_v2 "
        "WHERE relation='search_to_execute_chain'"
    ).fetchall()
    if not chains:
        return habits

    spans = []
    for from_id, to_id, ev in chains:
        m = re.search(r'span=(\d+)min', ev or "")
        if m:
            spans.append(int(m.group(1)))
        m2 = re.search(r'span=(\d+)h', ev or "")
        if m2:
            spans.append(int(m2.group(1)) * 60)

    if spans:
        median = sorted(spans)[len(spans) // 2]
        ev_ids = set()
        for from_id, to_id, _ in chains[:20]:
            ev_ids.add(from_id)
            ev_ids.add(to_id)
        if median < 360:
            desc = f"你倾向快速闭环——从搜索到AI执行通常在{median//60}小时内完成(基于{len(chains)}条链路)"
        else:
            desc = f"你的搜→问→做工作流周期约{median//60}小时(基于{len(chains)}条链路)"
        habits.append({
            "subtype": "search_execute_loop", "subject": "搜→问→做闭环",
            "description": desc, "evidence_ids": list(ev_ids)[:30],
            "confidence": 0.7, "source": f"search_to_execute_chain({len(chains)}条)"
        })

    ask_chains = con.execute(
        "SELECT COUNT(*) FROM entity_links_v2 WHERE relation='search_to_ask_chain'"
    ).fetchone()[0]
    if ask_chains >= 50:
        ev_ids2 = set()
        for r in con.execute(
            "SELECT from_event_id, to_event_id FROM entity_links_v2 "
            "WHERE relation='search_to_ask_chain' LIMIT 20"
        ):
            ev_ids2.add(r[0])
            ev_ids2.add(r[1])
        habits.append({
            "subtype": "search_ask_pattern", "subject": "搜→问模式",
            "description": f"你经常先Google搜索,再带着问题问GPT(共{ask_chains}条链路)",
            "evidence_ids": list(ev_ids2)[:30], "confidence": 0.7,
            "source": f"search_to_ask_chain({ask_chains}条)"
        })
    return habits


def save_and_report(con, facts, projects, habits, now):
    stats = {"started_at": now, "rules_version": RULES_VERSION,
             "fact": 0, "project": 0, "habit": 0,
             "details": {"fact": [], "project": [], "habit": []}}

    for f in facts:
        _insert_memory(con, "fact", f["subtype"], f["subject"], f["description"],
                       f["evidence_ids"], {"source": f.get("source", "")}, f["confidence"], now)
        stats["fact"] += 1
        stats["details"]["fact"].append({"subject": f["subject"], "description": f["description"]})

    for p in projects:
        _insert_memory(con, "project", "active_project", p["subject"],
                       f"你在做的项目:{p['subject']}(路径含{p['path'][:40]})",
                       p["evidence"], {"path": p["path"], "source": p["source"]}, 0.7, now)
        stats["project"] += 1
        stats["details"]["project"].append({"subject": p["subject"], "path": p["path"][:60]})

    for h in habits:
        _insert_memory(con, "habit", h["subtype"], h["subject"], h["description"],
                       h["evidence_ids"], {"source": h.get("source", "")}, h["confidence"], now)
        stats["habit"] += 1
        stats["details"]["habit"].append({"subject": h["subject"], "description": h["description"]})

    con.commit()
    return stats


def write_report(stats, analysis_dir):
    ensure_dirs([analysis_dir])
    lines = [
        "# 上下文记忆抽取报告(project/habit/fact v2)",
        "",
        f"- 抽取时间: {stats['started_at']}",
        f"- 规则版本: {stats['rules_version']}",
        f"- fact: {stats['fact']} 条",
        f"- project: {stats['project']} 条(v2已清洗噪音)",
        f"- habit: {stats['habit']} 条",
        "",
        "> v2 修正: project 过滤 UUID/skillID/通用目录(基于用户反馈)。",
        "",
    ]
    for mtype, label in [("fact", "稳定事实(关于你的硬事实)"),
                          ("project", "项目归属(你在做的项目)"),
                          ("habit", "工作流习惯(反复的行为模式)")]:
        items = stats["details"][mtype]
        lines.append(f"## {label}({len(items)} 条)")
        lines.append("")
        if not items:
            lines.append("_(无)_\n")
            continue
        lines.append("| 主体 | 描述 |")
        lines.append("|------|------|")
        for it in items:
            desc = it.get("description", it.get("path", ""))
            lines.append(f"| {it['subject']} | {desc} |")
        lines.append("")

    lines.append("## 验证说明")
    lines.append("- v2 重点验证: project 噪音是否已清除(应只剩真项目)")
    lines.append("\n不准确的地方在每行后面标注反馈。\n")
    lines.append(f"---\n*规则版本 {RULES_VERSION}*")

    (analysis_dir / "context_report.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(analysis_dir / "context_report.json", stats)


def main():
    print("=" * 60)
    print("上下文记忆抽取 build_context_memory.py")
    print(f"  fact + project + habit ({RULES_VERSION})")
    print("=" * 60)

    if not UNIFIED_DB.exists():
        print(f"\n[ERROR] 统合库不存在: {UNIFIED_DB}")
        sys.exit(1)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(UNIFIED_DB)
    try:
        print("\n[1/5] 确保表结构...")
        ensure_schema(con)
        print("    [OK] 已就绪")

        print("\n[2/5] 清空旧 fact/project/habit 记忆(幂等)...")
        reset_context_memory(con)
        print("    [OK] 已清空")

        print("\n[3/5] 抽取 fact(稳定事实)...")
        facts = build_fact_memory(con, now)
        print(f"    [OK] {len(facts)} 条 fact")

        print("\n[4/5] 抽取 project(v2 噪音清洗)...")
        projects = build_project_memory(con, now)
        print(f"    [OK] {len(projects)} 条 project")

        print("\n[5/5] 抽取 habit(工作流习惯)...")
        habits = build_habit_memory(con, now)
        print(f"    [OK] {len(habits)} 条 habit")

        print("\n写入数据库 + 生成报告...")
        stats = save_and_report(con, facts, projects, habits, now)
        write_report(stats, ANALYSIS_DIR)
        print(f"    [OK] {ANALYSIS_DIR / 'context_report.md'}")
    finally:
        con.close()

    print("\n" + "=" * 60)
    print(f"完成: fact {stats['fact']} + project {stats['project']} + habit {stats['habit']}")
    print("请打开 context_report.md 验证 v2。")
    print("=" * 60)


if __name__ == "__main__":
    main()
