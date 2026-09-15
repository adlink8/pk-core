"""能力记忆抽取脚本(阶段四 Wave 3 扩展:capability)。

从 skill 类事件抽取"你让AI帮你做什么"的能力使用记忆。
这是 tooling(用什么工具)之外的补充维度:
  tooling = 入口(你用 Codex/ChatGPT)
  capability = 能力(你让它们做什么)

数据源: unified_events 中 event_type='skill' 的 527 条(380个不同skill)

设计要点:
  1. gsd:* 系列归并(几十个 gsd:xxx → 一个"GSD项目管理"能力)
  2. 强度分档(核心/常用/尝试),复用 tooling 判定逻辑
  3. 规则法,每条可回溯

运行: python integration\\scripts\\build_capability_memory.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_knowledge.core.common import sha256_text, write_json, ensure_dirs
from personal_knowledge.core.memory_governance import build_governance_metadata, load_last_seen, unique_evidence_ids


# === 配置 ===
ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
from personal_knowledge.core.project_paths import STAGE1_PROFILE_DIR as ANALYSIS_DIR  # stage1 reports

RULES_VERSION = "capability-v1"
MAX_EVIDENCE_LINKS = 30

# 强度阈值
CAP_CORE_MIN_TOTAL = 5       # 核心能力:总使用≥5次
CAP_COMMON_MIN_TOTAL = 3     # 常用能力:总使用≥3次
CAP_TRY_MIN_TOTAL = 1        # 尝试能力:至少用过1次(记录但低权重)
CAP_TRY_MAX_TOTAL = 2        # 尝试能力上限

# skill 归并规则:相同前缀的 skill 归成一个能力
# 基于数据发现:cli-anything-* 有30+变种,gsd:* 有85个,都该归并
PREFIX_GROUPS = {
    "gsd": ("GSD项目管理", "你用GSD工作流管理项目(规划/执行/审计/交付)"),
    "cli-anything": ("CLI工具搭建(cli-anything方法论)", "你用cli-anything方法论给各种工具/平台搭建CLI界面"),
    "cli-hub": ("CLI Hub", "你使用/维护CLI Hub元能力框架"),
    "doc": ("文档处理(.docx)", "你用AI处理.docx文档"),
    "pdf": ("PDF处理", "你用AI处理PDF文档"),
}


def _match_prefix(name: str) -> str | None:
    """返回 skill 名匹配的归并前缀key,无匹配返回None。

    匹配规则:以 "前缀:" 或 "前缀-" 或 "前缀" 开头。
    优先匹配长前缀(cli-anything 优先于 cli-hub)。
    """
    low = name.lower().strip()
    # 按前缀长度降序匹配,避免短前缀误吃长前缀
    for pfx in sorted(PREFIX_GROUPS.keys(), key=len, reverse=True):
        if low == pfx or low.startswith(pfx + ":") or low.startswith(pfx + "-"):
            return pfx
    return None


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


def reset_capability_memory(con: sqlite3.Connection) -> None:
    con.execute(
        "DELETE FROM memory_links WHERE memory_id IN "
        "(SELECT memory_id FROM memory_items WHERE memory_type='capability')"
    )
    con.execute("DELETE FROM memory_items WHERE memory_type='capability'")
    con.commit()


def _insert_memory(con, subtype, subject, description, evidence_ids, metadata, confidence, now):
    evidence_ids = unique_evidence_ids(evidence_ids, limit=MAX_EVIDENCE_LINKS)
    metadata = build_governance_metadata(
        source="capability:skill",
        evidence_ids=evidence_ids,
        confidence=confidence,
        merge_key=f"capability|{subtype}|{subject.lower()}",
        last_seen=load_last_seen(con, evidence_ids),
        extra=metadata,
    )
    memory_id = sha256_text(f"capability|{subtype}|{subject.lower()}")
    con.execute(
        "INSERT OR REPLACE INTO memory_items "
        "(memory_id, memory_type, memory_subtype, subject, description, "
        " confidence, evidence_count, metadata, created_at) "
        "VALUES (?, 'capability', ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, subtype, subject, description,
         confidence, len(evidence_ids), json.dumps(metadata, ensure_ascii=False), now)
    )
    for eid in evidence_ids[:MAX_EVIDENCE_LINKS]:
        con.execute(
            "INSERT INTO memory_links (memory_id, target_type, target_id, relation) "
            "VALUES (?, 'event', ?, 'evidenced_by')",
            (memory_id, eid)
        )
    return memory_id


def collect_skills(con: sqlite3.Connection) -> dict:
    """统计每个 skill 的使用情况和证据。

    返回: {归并后的能力key: {name, desc, total, evidence_ids, members}}
    """
    rows = con.execute(
        "SELECT event_id, title, service FROM unified_events WHERE event_type='skill'"
    ).fetchall()

    # 先按原始 skill 名统计
    raw_skills = defaultdict(lambda: {"total": 0, "evidence": [], "service": Counter()})
    for eid, title, svc in rows:
        name = (title or "").strip()
        if not name or name == ">-":  # 噪音
            continue
        raw_skills[name]["total"] += 1
        raw_skills[name]["evidence"].append(eid)
        if svc:
            raw_skills[name]["service"][svc] += 1

    # 归并:相同前缀的 skill 合并
    grouped = {}
    for name, data in raw_skills.items():
        prefix_key = _match_prefix(name)

        if prefix_key:
            gname, gdesc = PREFIX_GROUPS[prefix_key]
            key = prefix_key
            if key not in grouped:
                grouped[key] = {
                    "subject": gname, "description_base": gdesc,
                    "total": 0, "evidence": [], "members": [],
                    "service": Counter(),
                }
            grouped[key]["total"] += data["total"]
            grouped[key]["evidence"].extend(data["evidence"])
            grouped[key]["members"].append(name)
            grouped[key]["service"].update(data["service"])
        else:
            key = name.lower()
            if key not in grouped:
                grouped[key] = {
                    "subject": name,
                    "description_base": f"你用AI执行'{name}'相关任务",
                    "total": 0, "evidence": [], "members": [],
                    "service": Counter(),
                }
            grouped[key]["total"] += data["total"]
            grouped[key]["evidence"].extend(data["evidence"])
            grouped[key]["members"].append(name)
            grouped[key]["service"].update(data["service"])

    return grouped


def classify_capability(data: dict) -> tuple[str, str] | None:
    """判定能力强度档位。

    基于用户反馈:skill 的"存在"本身就是信号(定义过=具备该能力)。
    全部保留,只按层次分档:
      core(≥5):       反复使用的核心能力
      common(3-4):    定期使用
      occasional(2):  偶尔使用
      minor(1):       具备但很少用(最低层,仍保留)
    """
    total = data["total"]
    members = data["members"]
    desc_base = data["description_base"]

    if total >= CAP_CORE_MIN_TOTAL:
        member_note = f"(含{len(members)}个子能力)" if len(members) > 1 else ""
        return (
            "core_capability",
            f"{desc_base},是你反复使用的核心能力(共{total}次){member_note}"
        )
    if total >= CAP_COMMON_MIN_TOTAL:
        return (
            "common_capability",
            f"{desc_base}(共{total}次,定期使用)"
        )
    if total >= 2:
        member_note = f"(含{len(members)}个子能力)" if len(members) > 1 else ""
        return (
            "occasional_capability",
            f"{desc_base}(共{total}次,偶尔使用){member_note}"
        )
    # 1次的也保留为最低层(用户反馈:定义过=具备该能力)
    return (
        "minor_capability",
        f"{desc_base}(用过1次,具备但很少用)"
    )


def build_capability_memory(con: sqlite3.Connection, now: str) -> dict:
    grouped = collect_skills(con)

    stats = {
        "started_at": now,
        "rules_version": RULES_VERSION,
        "total_raw_skills": len(grouped),
        "by_subtype": defaultdict(list),
        "skipped": [],
    }

    n_inserted = 0
    for key, data in grouped.items():
        result = classify_capability(data)
        if result is None:
            stats["skipped"].append({"subject": data["subject"], "total": data["total"]})
            continue

        subtype, description = result
        evidence_ids = list(set(data["evidence"]))[:MAX_EVIDENCE_LINKS]
        metadata = {
            "rules_version": RULES_VERSION,
            "total_uses": data["total"],
            "members": data["members"][:10],
            "member_count": len(data["members"]),
            "top_services": dict(data["service"].most_common(3)),
        }
        # 不同档位不同置信度: core=0.9, common=0.8, occasional=0.6, minor=0.4
        conf_map = {"core_capability": 0.9, "common_capability": 0.8,
                    "occasional_capability": 0.6, "minor_capability": 0.4}
        _insert_memory(con, subtype, data["subject"], description, evidence_ids, metadata,
                       conf_map.get(subtype, 0.5), now)

        stats["by_subtype"][subtype].append({
            "subject": data["subject"],
            "description": description,
            "evidence_count": data["total"],
            "members": len(data["members"]),
            "top_service": data["service"].most_common(1)[0][0] if data["service"] else "",
        })
        n_inserted += 1

    con.commit()
    stats["inserted"] = n_inserted
    return stats


def write_report(stats: dict, analysis_dir: Path) -> None:
    ensure_dirs([analysis_dir])
    lines = [
        "# 能力记忆抽取报告(capability)",
        "",
        f"- 抽取时间: {stats['started_at']}",
        f"- 规则版本: {stats['rules_version']}",
        f"- 不同能力总数: {stats['total_raw_skills']}",
        f"- 抽取记忆数: {stats['inserted']}",
        "",
        "> 维度: 你让AI帮你做什么(补充 tooling 的'用什么工具')",
        "> gsd:* 系列已归并成'GSD项目管理'",
        "",
        "## 抽取出的能力(按强度档位)",
        "",
    ]

    subtype_labels = {
        "core_capability": "核心能力(反复使用,≥5次)",
        "common_capability": "常用能力(定期使用,3-4次)",
        "occasional_capability": "偶尔使用的能力(2次)",
        "minor_capability": "具备但少用(1次,最低层)",
    }

    for subtype, label in subtype_labels.items():
        items = stats["by_subtype"].get(subtype, [])
        lines.append(f"### {label}({len(items)} 条)")
        lines.append("")
        if not items:
            lines.append("_(无)_\n")
            continue
        # minor 档太多,折叠显示(只列前15 + 总数)
        if subtype == "minor_capability" and len(items) > 20:
            lines.append(f"(共{len(items)}个,只展示前15个,完整列表见 JSON)")
            lines.append("")
            items = items[:15]
        lines.append("| 能力 | 描述 | 使用次数 | 子能力数 | 主要工具 |")
        lines.append("|------|------|---------|---------|---------|")
        for it in items:
            lines.append(f"| {it['subject']} | {it['description']} | {it['evidence_count']} | {it['members']} | {it['top_service']} |")
        lines.append("")

    if stats["skipped"]:
        lines.append(f"## 跳过的({len(stats['skipped'])} 个,纯噪音)")
        lines.append("")
        for sk in stats["skipped"][:10]:
            lines.append(f"- {sk['subject']}: {sk['total']}次")
        lines.append("")

    lines.append("## 验证说明")
    lines.append("- 核心能力:这些是你反复用的吗?")
    lines.append("- gsd归并:几十个gsd:xxx合成一个合理吗?")
    lines.append("- 有没有重要的能力被漏了?")
    lines.append("\n不准确的地方在每行后面标注反馈。\n")
    lines.append(f"---\n*规则版本 {RULES_VERSION}*")

    (analysis_dir / "capability_report.md").write_text("\n".join(lines), encoding="utf-8")
    stats_clean = {**stats, "by_subtype": dict(stats["by_subtype"])}
    write_json(analysis_dir / "capability_report.json", stats_clean)


def main():
    print("=" * 60)
    print("能力记忆抽取 build_capability_memory.py")
    print(f"  memory_type: capability ({RULES_VERSION})")
    print("=" * 60)

    if not UNIFIED_DB.exists():
        print(f"\n[ERROR] 统合库不存在: {UNIFIED_DB}")
        sys.exit(1)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(UNIFIED_DB)
    try:
        print("\n[1/4] 确保表结构...")
        ensure_schema(con)
        print("    [OK] 已就绪")

        print("\n[2/4] 清空旧 capability 记忆(幂等)...")
        reset_capability_memory(con)
        print("    [OK] 已清空")

        print("\n[3/4] 抽取 capability(归并+强度判定)...")
        stats = build_capability_memory(con, now)
        print(f"    [OK] {stats['total_raw_skills']}个能力 -> 抽取 {stats['inserted']} 条")
        for subtype, items in stats["by_subtype"].items():
            print(f"      {subtype}: {len(items)} 条")

        print("\n[4/4] 生成报告...")
        write_report(stats, ANALYSIS_DIR)
        print(f"    [OK] {ANALYSIS_DIR / 'capability_report.md'}")
    finally:
        con.close()

    print("\n" + "=" * 60)
    print("完成。请打开 capability_report.md 验证。")
    print("=" * 60)


if __name__ == "__main__":
    main()
