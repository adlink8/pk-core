"""记忆对象层构建脚本(阶段四 Wave 3:tooling 工具偏好 v3)。

v3 修正(基于用户评测反馈 + 数据缺失约束):
  - 核心约束:用户数据可能导不全(某月0次≠真的没用),判定必须容忍缺失
  - ChatGPT 误判"衰减"(实际持续):因为4/6月数据缺失 → 改用"近3月峰值"判持续
  - Claude 误判"持续"(实际衰减):因为只看"有没有"没看趋势 → 加入"趋势方向"判定
  - GPT-5.5 漏抽(只1月但最近在用):偶尔使用放宽到"近3月有使用即可"

判定逻辑(3维度):
  1. 强度: 月均使用次数(历史基线)
  2. 近期活跃: 近3月里有任一月达到"仍在用"水平
  3. 趋势方向: 仅在近3月有连续数据时才判衰减

运行: python integration\\scripts\\build_memory_store.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_knowledge.core.common import sha256_text, norm, write_json, ensure_dirs
from personal_knowledge.core.memory_governance import build_governance_metadata, load_last_seen


# === 配置 ===
ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
from personal_knowledge.core.project_paths import STAGE1_PROFILE_DIR as ANALYSIS_DIR  # stage1 reports

RULES_VERSION = "tooling-v4"  # v4: 衰减判定优先于持续(修复 Claude)

SERVICE_BLACKLIST = {"Unknown", "Auto", "AI Mode", "WSL:Archive"}

# === tooling 抽取规则阈值(v3) ===
# 持续主力:历史月均高 + 近3月至少1月还高密度(容忍其他月缺失)
CONTINUOUS_MIN_MONTHS = 3
CONTINUOUS_MIN_MONTHLY_AVG = 20
CONTINUOUS_RECENT_PEAK = 20   # 近3月里至少1个月≥20次

# 衰减主力:历史是主力 + 近3月连续月份显示明显下降趋势
DECLINING_MIN_HISTORY_MONTHS = 3
DECLINING_MIN_HISTORY_EVENTS = 50
DECLINING_MIN_RECENT_DATA_MONTHS = 2   # 近3月至少2月有数据(才能判趋势)
DECLINING_LAST_MONTH_RATIO = 0.5       # 最近月 < 近3月其他月平均的50%

# 偶尔使用:低频但最近还在用
OCCASIONAL_MIN_EVENTS = 5
OCCASIONAL_MAX_MONTHLY_AVG = 20
# 近3月必须有使用(排除已停的)

# 爆发工具
SURGING_MIN_PEAK = 200
SURGING_MAX_MONTHS = 3

# 已弃用:近3月完全0次
ABANDONED_MIN_EVENTS = 10
ABANDONED_RECENT_MAX = 0

# 专项工具
SPECIALIZED_HIGH_MIN = 100
SPECIALIZED_LOW_MIN = 10

MAX_EVIDENCE_LINKS = 50


def _get_recent_months(con: sqlite3.Connection, n: int = 3) -> list[str]:
    """动态取数据里最大的 n 个月份。"""
    rows = con.execute(
        "SELECT DISTINCT substr(month,1,7) m FROM unified_events "
        "WHERE month IS NOT NULL AND month != '' AND length(month) >= 7 "
        "ORDER BY m DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def ensure_schema(con: sqlite3.Connection) -> None:
    """建表(幂等)。"""
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
        CREATE TABLE IF NOT EXISTS memory_relations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            from_memory_id  TEXT NOT NULL,
            to_memory_id    TEXT NOT NULL,
            relation        TEXT NOT NULL,
            strength        REAL DEFAULT 1.0,
            FOREIGN KEY (from_memory_id) REFERENCES memory_items(memory_id) ON DELETE CASCADE,
            FOREIGN KEY (to_memory_id) REFERENCES memory_items(memory_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_memory_relations_from ON memory_relations(from_memory_id);
        """
    )
    con.commit()


def reset_memory_tables(con: sqlite3.Connection) -> None:
    """幂等:重跑时清空 tooling 记忆。"""
    con.execute(
        "DELETE FROM memory_links WHERE memory_id IN "
        "(SELECT memory_id FROM memory_items WHERE memory_type='tooling')"
    )
    con.execute(
        "DELETE FROM memory_relations WHERE from_memory_id IN "
        "(SELECT memory_id FROM memory_items WHERE memory_type='tooling') "
        "OR to_memory_id IN "
        "(SELECT memory_id FROM memory_items WHERE memory_type='tooling')"
    )
    con.execute("DELETE FROM memory_items WHERE memory_type='tooling'")
    con.commit()


def load_service_stats(con: sqlite3.Connection) -> list[dict]:
    """统计每个 service 的使用画像。"""
    recent_months = _get_recent_months(con, 3)
    recent_set = set(recent_months)

    month_dist = defaultdict(lambda: defaultdict(int))
    for r in con.execute(
        "SELECT service, substr(month,1,7) as m, COUNT(*) c "
        "FROM unified_events "
        "WHERE service IS NOT NULL AND service != '' "
        "AND month IS NOT NULL AND month != '' "
        "GROUP BY service, m"
    ):
        month_dist[r[0]][r[1]] = r[2]

    total_no_month = defaultdict(int)
    for r in con.execute(
        "SELECT service, COUNT(*) c FROM unified_events "
        "WHERE service IS NOT NULL AND service != '' "
        "AND (month IS NULL OR month = '') GROUP BY service"
    ):
        total_no_month[r[0]] = r[1]

    stats = []
    for svc, months in month_dist.items():
        if svc in SERVICE_BLACKLIST:
            continue
        total = sum(months.values()) + total_no_month.get(svc, 0)
        peak_month, peak_count = max(months.items(), key=lambda x: x[1]) if months else ("", 0)
        recent_count = sum(c for m, c in months.items() if m in recent_set)
        # 近3月每月的使用量(用于趋势判定),按月份排序
        recent_monthly = sorted(
            [(m, c) for m, c in months.items() if m in recent_set],
            key=lambda x: x[0]
        )
        # 近3月峰值
        recent_peak = max((c for _, c in recent_monthly), default=0)
        # 近3月有数据的月份数
        recent_data_months = len(recent_monthly)
        # 最后一个月的使用量(最新)
        last_month_count = recent_monthly[-1][1] if recent_monthly else 0
        # 近3月中除最后一个月外的平均(用于判趋势)
        if len(recent_monthly) >= 2:
            prev_avg = sum(c for _, c in recent_monthly[:-1]) / len(recent_monthly[:-1])
        else:
            prev_avg = last_month_count

        monthly_avg = total / len(months) if months else 0
        stats.append({
            "subject": svc,
            "source": "service",
            "total_events": total,
            "active_months": len(months),
            "monthly_avg": round(monthly_avg, 1),
            "recent_count": recent_count,
            "recent_data_months": recent_data_months,
            "recent_peak": recent_peak,
            "last_month_count": last_month_count,
            "prev_avg": round(prev_avg, 1),
            "peak_month": peak_month,
            "peak_count": peak_count,
            "month_dist": dict(months),
        })
    return stats


def load_tool_stats(con: sqlite3.Connection) -> list[dict]:
    """只出现在 tool 实体但不在 service 的工具。"""
    services = set(r[0] for r in con.execute(
        "SELECT DISTINCT service FROM unified_events "
        "WHERE service IS NOT NULL AND service != ''"
    )) - SERVICE_BLACKLIST

    tools = []
    for r in con.execute(
        "SELECT name, event_count FROM entities "
        "WHERE entity_type='tool' ORDER BY event_count DESC"
    ):
        if r[0] in services:
            continue
        tools.append({
            "subject": r[0],
            "source": "tool",
            "total_events": r[1],
        })
    return tools


def classify_tooling_memory(stat: dict, recent_months: list[str]) -> tuple[str, str] | None:
    """v3: 容忍数据缺失 + 趋势方向判定。"""
    s = stat["subject"]
    total = stat["total_events"]

    if stat["source"] == "service":
        active = stat["active_months"]
        monthly_avg = stat.get("monthly_avg", 0)
        peak = stat["peak_count"]
        peak_month = stat["peak_month"]
        recent_count = stat["recent_count"]
        recent_peak = stat.get("recent_peak", 0)
        recent_data_months = stat.get("recent_data_months", 0)
        last_month_count = stat.get("last_month_count", 0)
        prev_avg = stat.get("prev_avg", 0)

        # 规则1(优先): declining_primary(衰减主力)
        # 必须先于持续主力判定——"曾经主力但现在衰减"比"持续主力"信息更准。
        # 条件: 历史是主力 + 近3月有连续数据 + 最后一个月断崖式下降。
        # 关键: last_month_count 要明显低(用绝对阈值+比例双重判定),
        #       避免把"高频工具某月略降"误判为衰减。
        if (active >= DECLINING_MIN_HISTORY_MONTHS
                and total >= DECLINING_MIN_HISTORY_EVENTS
                and recent_data_months >= DECLINING_MIN_RECENT_DATA_MONTHS
                and prev_avg > 0
                and last_month_count < prev_avg * DECLINING_LAST_MONTH_RATIO
                and last_month_count < CONTINUOUS_RECENT_PEAK):  # 最后月必须低于持续阈值
            return (
                "declining_primary",
                f"你曾经的主力工具 {s}(历史{active}个月/{total}次),近3月明显衰减"
                f"({last_month_count}←月均{prev_avg})"
            )

        # 规则2: continuous_primary(持续主力)
        # 历史月均高 + 近3月至少1月还高密度(容忍缺失)
        if (active >= CONTINUOUS_MIN_MONTHS
                and monthly_avg >= CONTINUOUS_MIN_MONTHLY_AVG
                and recent_peak >= CONTINUOUS_RECENT_PEAK):
            return (
                "continuous_primary",
                f"你的持续主力工具 {s}(跨{active}个月,月均{monthly_avg}次,近期仍在高频使用)"
            )

        # 规则3: occasional_use(偶尔使用)
        # 低频 + 最近还在用
        if (total >= OCCASIONAL_MIN_EVENTS
                and monthly_avg <= OCCASIONAL_MAX_MONTHLY_AVG
                and recent_count > 0):
            return (
                "occasional_use",
                f"你偶尔使用 {s}(共{total}次,月均{monthly_avg}次,近期仍在用)"
            )

        # 规则4: surging_tool(爆发工具)
        if peak >= SURGING_MIN_PEAK and active <= SURGING_MAX_MONTHS:
            return (
                "surging_tool",
                f"你近期重点投入 {s}({peak_month}单月{peak}次)"
            )

        # 规则5: abandoned_tool(已弃用)
        if (total >= ABANDONED_MIN_EVENTS
                and recent_count <= ABANDONED_RECENT_MAX
                and active >= 1):
            return (
                "abandoned_tool",
                f"你曾用过 {s}(共{total}次),但近3月已停用"
            )

        return None

    # tool 实体(专项基础设施)
    if stat["source"] == "tool":
        if total >= SPECIALIZED_HIGH_MIN:
            return (
                "specialized_high",
                f"你工作中高频依赖 {s}(被提及{total}次)"
            )
        if total >= SPECIALIZED_LOW_MIN:
            return (
                "specialized_low",
                f"你工作中偶尔提及 {s}(被提及{total}次)"
            )

    return None


def get_evidence_events(con: sqlite3.Connection, stat: dict, limit: int = MAX_EVIDENCE_LINKS) -> list[str]:
    """取证据事件 event_id(优先近期)。"""
    s = stat["subject"]
    if stat["source"] == "service":
        rows = con.execute(
            "SELECT event_id FROM unified_events "
            "WHERE service=? ORDER BY month DESC, rowid DESC LIMIT ?",
            (s, limit)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT ee.event_id FROM event_entities ee "
            "JOIN entities e ON ee.entity_id = e.entity_id "
            "WHERE e.entity_type='tool' AND e.name=? "
            "AND ee.relation='mentions_tool' "
            "ORDER BY ee.rowid DESC LIMIT ?",
            (s, limit)
        ).fetchall()
    return [r[0] for r in rows]


def insert_memory(
    con: sqlite3.Connection,
    memory_subtype: str,
    subject: str,
    description: str,
    stat: dict,
    evidence_count: int,
    rules_version: str,
    now: str,
) -> str:
    """插入一条 memory_items + memory_links 证据。"""
    memory_id = sha256_text(f"tooling|{memory_subtype}|{subject.lower()}")
    evidence_ids = get_evidence_events(con, stat)
    metadata = build_governance_metadata(
        source=f"tooling:{stat['source']}",
        evidence_ids=evidence_ids,
        confidence=0.7,
        merge_key=f"tooling|{memory_subtype}|{subject.lower()}",
        last_seen=load_last_seen(con, evidence_ids),
        extra={
        "rules_version": rules_version,
        "total_events": stat["total_events"],
        "active_months": stat.get("active_months"),
        "monthly_avg": stat.get("monthly_avg"),
        "recent_count": stat.get("recent_count"),
        "recent_peak": stat.get("recent_peak"),
        "recent_data_months": stat.get("recent_data_months"),
        "last_month_count": stat.get("last_month_count"),
        "prev_avg": stat.get("prev_avg"),
        "peak_month": stat.get("peak_month"),
        "peak_count": stat.get("peak_count"),
        },
    )
    con.execute(
        "INSERT OR REPLACE INTO memory_items "
        "(memory_id, memory_type, memory_subtype, subject, description, "
        " confidence, evidence_count, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, "tooling", memory_subtype, subject, description,
         0.7, evidence_count, json.dumps(metadata, ensure_ascii=False), now)
    )
    for eid in evidence_ids:
        con.execute(
            "INSERT INTO memory_links (memory_id, target_type, target_id, relation) "
            "VALUES (?, 'event', ?, 'evidenced_by')",
            (memory_id, eid)
        )
    return memory_id


def build_tooling_memory(con: sqlite3.Connection, now: str) -> dict:
    """主流程:抽取所有 tooling 记忆对象。"""
    recent_months = _get_recent_months(con, 3)
    service_stats = load_service_stats(con)
    tool_stats = load_tool_stats(con)
    all_stats = service_stats + tool_stats

    stats = {
        "started_at": now,
        "rules_version": RULES_VERSION,
        "recent_months": recent_months,
        "total_candidates": len(all_stats),
        "by_subtype": defaultdict(list),
        "skipped": [],
    }

    n_inserted = 0
    for stat in all_stats:
        result = classify_tooling_memory(stat, recent_months)
        if result is None:
            stats["skipped"].append({
                "subject": stat["subject"],
                "source": stat["source"],
                "total": stat["total_events"],
                "reason": "不满足任何 tooling 规则阈值",
            })
            continue
        subtype, description = result
        memory_id = insert_memory(
            con, subtype, stat["subject"], description, stat,
            stat["total_events"], RULES_VERSION, now
        )
        stats["by_subtype"][subtype].append({
            "memory_id": memory_id,
            "subject": stat["subject"],
            "description": description,
            "evidence_count": stat["total_events"],
        })
        n_inserted += 1

    con.commit()
    stats["inserted"] = n_inserted
    return stats


def write_report(stats: dict, analysis_dir: Path) -> None:
    """生成 memory_report.md。"""
    ensure_dirs([analysis_dir])
    lines = [
        "# 记忆对象抽取报告(tooling v3)",
        "",
        f"- 抽取时间: {stats['started_at']}",
        f"- 规则版本: {stats['rules_version']}",
        f"- 近3月判定: {', '.join(stats['recent_months'])}",
        f"- 候选工具总数: {stats['total_candidates']}",
        f"- 抽取记忆数: {stats['inserted']}",
        f"- 跳过(不满足阈值): {len(stats['skipped'])}",
        "",
        "> v3 修正: 容忍数据缺失(某月0次≠停用) + 趋势方向判定(看降不降)。",
        "> 关键约束: 用户数据可能导不全,判定不能依赖'每月都有'。",
        "",
        "## 抽取出的记忆(按子类型)",
        "",
    ]

    subtype_labels = {
        "continuous_primary": "持续主力(长期高频,近期仍密集)",
        "declining_primary": "衰减主力(曾经主力,近3月连续下降)",
        "occasional_use": "偶尔使用(低频但近期仍在用)",
        "surging_tool": "爆发工具(短期高密度投入)",
        "abandoned_tool": "已弃用(近3月完全0次)",
        "specialized_high": "专项-高频依赖(基础设施,被提及)",
        "specialized_low": "专项-偶尔提及(基础设施,被提及)",
    }

    for subtype, label in subtype_labels.items():
        items = stats["by_subtype"].get(subtype, [])
        lines.append(f"### {label}({len(items)} 条)")
        lines.append("")
        if not items:
            lines.append("_(无)_")
            lines.append("")
            continue
        lines.append("| 工具 | 描述 | 证据数 |")
        lines.append("|------|------|--------|")
        for it in items:
            lines.append(f"| {it['subject']} | {it['description']} | {it['evidence_count']} |")
        lines.append("")

    if stats["skipped"]:
        lines.append(f"## 跳过的工具({len(stats['skipped'])} 个,未达阈值)")
        lines.append("")
        lines.append("| 工具 | 来源 | 总次数 | 原因 |")
        lines.append("|------|------|--------|------|")
        for sk in stats["skipped"]:
            lines.append(f"| {sk['subject']} | {sk['source']} | {sk['total']} | {sk['reason']} |")
        lines.append("")

    lines.append("## v3 重点验证")
    lines.append("")
    lines.append("- **持续主力**: ChatGPT 现在应落这里(修复v2误判,容忍4/6月缺失)")
    lines.append("- **衰减主力**: Claude 应落这里(4→5→6月: 329→22→2 明显衰减)")
    lines.append("- **偶尔使用**: GPT-5.5/Gemini 应落这里")
    lines.append("- **已弃用**: 只有近3月完全0次的才是真弃用")
    lines.append("")
    lines.append("不准确的地方继续在每行后面标注反馈。")
    lines.append("")
    lines.append("---")
    lines.append(f"*规则版本 {RULES_VERSION} · 容忍缺失 + 趋势判定*")

    (analysis_dir / "memory_report.md").write_text("\n".join(lines), encoding="utf-8")
    stats_clean = {**stats, "by_subtype": dict(stats["by_subtype"])}
    write_json(analysis_dir / "memory_report.json", stats_clean)


def main() -> None:
    print("=" * 60)
    print("记忆对象层构建 build_memory_store.py")
    print(f"  memory_type: tooling ({RULES_VERSION})")
    print(f"  数据库: {UNIFIED_DB.name}")
    print("=" * 60)

    if not UNIFIED_DB.exists():
        print(f"\n[ERROR] 统合库不存在: {UNIFIED_DB}")
        sys.exit(1)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(UNIFIED_DB)
    try:
        print("\n[1/4] 确保表结构存在...")
        ensure_schema(con)
        print("    [OK] memory_items / memory_links / memory_relations 已就绪")

        print("\n[2/4] 清空旧 tooling 记忆(幂等重跑)...")
        reset_memory_tables(con)
        print("    [OK] 已清空")

        print("\n[3/4] 抽取 tooling 记忆(v3 容忍缺失+趋势判定)...")
        stats = build_tooling_memory(con, now)
        print(f"    [OK] 候选 {stats['total_candidates']} -> 抽取 {stats['inserted']} 条记忆")
        for subtype, items in stats["by_subtype"].items():
            print(f"      {subtype}: {len(items)} 条")

        print("\n[4/4] 生成报告...")
        write_report(stats, ANALYSIS_DIR)
        print(f"    [OK] {ANALYSIS_DIR / 'memory_report.md'}")
    finally:
        con.close()

    print("\n" + "=" * 60)
    print("完成。请打开 memory_report.md 验证 v3 抽取质量。")
    print("=" * 60)


if __name__ == "__main__":
    main()
