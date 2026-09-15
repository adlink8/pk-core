"""偏好记忆对象抽取脚本(阶段四 Wave 3:preference 关注偏好)。

从 Google 模块(YouTube/Gemini Apps/Search)的 title+content,
用"信号词归并 + 噪音过滤 + 强度判定"三步法,抽出你的内容关注偏好。

与 tooling 的区别:
  - tooling: 工具是离散可数的(24个候选),直接判强度
  - preference: 内容是连续的,先挖信号词→归并主题→再判强度

三步法:
  1. 过滤噪音(URL碎片/系统文本/隐私内容/哈希ID)
  2. 信号词归并到主题(7个主题词表)
  3. 强度判定(复用 tooling 的 5档:持续/衰减/偶尔/突击/已停)

7个主题:
  asmr_relax    ASMR/助眠放松
  current_affairs 时事评论
  ai_automation AI自动化工作流
  dev_tech      开发技术栈
  language_learn 英语/语言学习
  career        职业/求职
  tech_tinker   网络工具折腾

运行: python integration\\scripts\\build_preference_memory.py
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

RULES_VERSION = "preference-v2"  # v2: 持续拆高频/低频 + surging/declining描述优化

# 数据源:Google 模块的三个 service
PREF_SOURCES = ["YouTube", "Gemini Apps", "Search"]

# === 第1步:噪音过滤黑名单 ===
# URL碎片/系统文本/隐私内容/哈希ID,命中即丢弃该信号词
NOISE_PATTERNS = [
    # URL/广告追踪碎片
    "https", "http", "google", "play", "youtube", "www.",
    "gbraid", "gclid", "gad_source", "gad_campaignid", "start-153",
    # Gemini 系统模板文本(出现545次的噪音)
    "used", "chat.", "manage", "gems.",
    # 代码片段/字段名噪音
    "cid", "date", "title", "const", "output", "node_modules", "file",
    "new", "master", "token", "gem",
    # 成人内容(隐私,不进偏好)
    "missav", "porn", "jav", "xxx", "porno", "sex", "tube", "pornhub",
    "黄漫",
]

def is_noise(word: str) -> bool:
    """判断信号词是否是噪音。"""
    w = word.lower()
    # 精确匹配黑名单
    if w in NOISE_PATTERNS:
        return True
    # 哈希/ID 模式(纯字母数字混合≥12位)
    if re.match(r'^[a-z0-9]{12,}$', w):
        return True
    # .jpg/.png 等文件名
    if re.match(r'^[a-z0-9]+\.(jpg|png|jpeg|json|txt|db)$', w):
        return True
    # 含哈希片段(如 a4910d283097d12b)
    if re.search(r'[a-f0-9]{10,}', w):
        return True
    return False


# === 第2步:主题词表(信号词 → 主题归并)===
# 每个主题: (主题key, 中文名, 描述, 信号词列表)
TOPICS = [
    ("asmr_relax", "ASMR/助眠放松", "你关注放松、助眠类内容",
     ["asmr", "relax", "sleep", "ear", "whispering", "ku100",
      "口腔音", "助眠", "放松", "massage", "芦荟胶", "cleaning",
      "恋乃夜", "vtuber"]),

    ("current_affairs", "时事评论", "你关注时政/社会评论内容",
     ["老周横眉", "方脸说", "左颜玉", "张雪峰", "北美老班长",
      "两会", "政府工作报告", "政治", "时政", "社会",
      "防火墙", "王局", "讣告"]),

    ("ai_automation", "AI自动化工作流", "你关注用AI构建自动化流程",
     ["自动流", "规划师", "设计师", "n8n", "workflow", "工作流",
      "提示词工程师", "automation", "gem", "gems", "自动化"]),

    ("dev_tech", "开发技术栈", "你关注编程开发技术",
     ["code", "github", "docker", "python", "linux", "devops",
      "api", "aws", "iot", "json", "markdown", "node",
      "前端", "后端", "数据库", "git", "编程", "调试",
      "cursor", "vscode", "typescript", "react", "vue"]),

    ("language_learn", "英语/语言学习", "你关注语言学习内容",
     ["english", "learn", "japanese", "conversation",
      "语言学习", "英语", "日语", "听力"]),

    ("career", "职业/求职", "你关注职业发展和求职",
     ["career_advice", "career_planning", "项目寻找", "求职", "offer",
      "面试", "简历", "职业", "考研", "留学", "金融"]),

    ("tech_tinker", "网络工具折腾", "你关注网络工具折腾",
     ["network_tools", "f-droid", "pairdrop", "android",
      "obsidian"]),
]

# === 第3步:强度判定阈值(v2: 拆分持续为高频/低频两档)===
# 持续-高频(ASMR/时事/开发/英语 这类长期高频)
PREF_CONT_HIGH_MIN_MONTHS = 3
PREF_CONT_HIGH_MIN_MONTHLY_AVG = 8
PREF_CONT_HIGH_RECENT_PEAK = 3

# 持续-低频(职业/求职 这类长期反复但每次不多)
PREF_CONT_LOW_MIN_MONTHS = 3
PREF_CONT_LOW_MIN_TOTAL = 20
PREF_CONT_LOW_RECENT_MIN = 1   # 近3月有使用即可

PREF_DECLINING_MIN_HISTORY = 3
PREF_DECLINING_MIN_HISTORY_TOTAL = 15
PREF_DECLINING_MIN_RECENT_DATA = 2
PREF_DECLINING_LAST_RATIO = 0.5
PREF_DECLINING_LAST_ABS = 3

PREF_OCCASIONAL_MIN_TOTAL = 5
PREF_OCCASIONAL_MAX_MONTHLY_AVG = 8
PREF_OCCASIONAL_RECENT_MIN = 1

PREF_SURGING_MIN_PEAK = 15
PREF_SURGING_MAX_MONTHS = 2

PREF_MIN_TOTAL = 5  # 低于此值不记录(信号太弱)

MAX_EVIDENCE_LINKS = 30


def _get_recent_months(con: sqlite3.Connection, n: int = 3) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT substr(month,1,7) m FROM unified_events "
        "WHERE month IS NOT NULL AND month != '' AND length(month) >= 7 "
        "ORDER BY m DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def ensure_schema(con: sqlite3.Connection) -> None:
    """确保 memory_items 等表存在(与 tooling 共用)。"""
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


def reset_preference_memory(con: sqlite3.Connection) -> None:
    """幂等:重跑时清空 preference 记忆。"""
    con.execute(
        "DELETE FROM memory_links WHERE memory_id IN "
        "(SELECT memory_id FROM memory_items WHERE memory_type='preference')"
    )
    con.execute("DELETE FROM memory_items WHERE memory_type='preference'")
    con.commit()


def collect_topic_hits(con: sqlite3.Connection) -> dict:
    """扫描所有 Google 模块事件,统计每个主题的命中情况和证据。

    返回: {topic_key: {total, months: {m: count}, evidence_ids: [...], by_source: {...}}}
    """
    # 初始化主题命中容器
    topic_data = {t[0]: {
        "name": t[1], "desc": t[2], "signals": t[3],
        "total": 0, "months": defaultdict(int),
        "evidence_ids": [], "by_source": defaultdict(int),
    } for t in TOPICS}

    # 建立信号词 → 主题 的反向索引(加速查找)
    signal_to_topic = {}
    for t in TOPICS:
        for sig in t[3]:
            signal_to_topic[sig.lower()] = t[0]

    # 扫描每个事件
    rows = con.execute(
        "SELECT event_id, service, title, substr(month,1,7) as m "
        "FROM unified_events "
        "WHERE source='Google' AND service IN ({}) "
        "AND (title IS NOT NULL OR content IS NOT NULL)".format(
            ",".join(f"'{s}'" for s in PREF_SOURCES)
        )
    ).fetchall()

    for r in rows:
        text = ((r[2] or "") + " " ).lower()
        month = r[3] if r[3] and len(r[3]) >= 7 else None
        hit_topics = set()

        # 检查每个信号词是否命中
        for sig, topic_key in signal_to_topic.items():
            if sig in text:
                # 过滤噪音(虽然主题词表已筛选,双保险)
                if is_noise(sig):
                    continue
                hit_topics.add(topic_key)

        for topic_key in hit_topics:
            td = topic_data[topic_key]
            td["total"] += 1
            td["by_source"][r[1]] += 1
            if month:
                td["months"][month] += 1
            if len(td["evidence_ids"]) < MAX_EVIDENCE_LINKS:
                td["evidence_ids"].append(r[0])

    return topic_data


def classify_preference(td: dict, recent_months: list[str]) -> tuple[str, str] | None:
    """判定主题的偏好强度档位。返回 (subtype, description) 或 None。"""
    recent_set = set(recent_months)
    total = td["total"]
    months = td["months"]
    name = td["name"]

    if total < PREF_MIN_TOTAL:
        return None

    active_months = len(months)
    monthly_avg = round(total / active_months, 1) if active_months else 0

    # 近3月数据
    recent_monthly = sorted([(m, c) for m, c in months.items() if m in recent_set])
    recent_count = sum(c for _, c in recent_monthly)
    recent_data_months = len(recent_monthly)
    recent_peak = max((c for _, c in recent_monthly), default=0)
    last_month_count = recent_monthly[-1][1] if recent_monthly else 0
    if len(recent_monthly) >= 2:
        prev_avg = sum(c for _, c in recent_monthly[:-1]) / len(recent_monthly[:-1])
    else:
        prev_avg = last_month_count

    # 规则1(优先): declining(衰减关注)
    if (active_months >= PREF_DECLINING_MIN_HISTORY
            and total >= PREF_DECLINING_MIN_HISTORY_TOTAL
            and recent_data_months >= PREF_DECLINING_MIN_RECENT_DATA
            and prev_avg > 0
            and last_month_count < prev_avg * PREF_DECLINING_LAST_RATIO
            and last_month_count < PREF_DECLINING_LAST_ABS):
        return (
            "declining_interest",
            f"你曾经偶尔关注{name}(历史{active_months}个月/{total}次),近期已淡出"
        )

    # 规则2: continuous_high(持续-高频)
    if (active_months >= PREF_CONT_HIGH_MIN_MONTHS
            and monthly_avg >= PREF_CONT_HIGH_MIN_MONTHLY_AVG
            and recent_peak >= PREF_CONT_HIGH_RECENT_PEAK):
        return (
            "continuous_high",
            f"你持续高频关注{name}(跨{active_months}个月,月均{monthly_avg}次,近期仍活跃)"
        )

    # 规则3: continuous_low(持续-低频,长期反复但每次不多)
    if (active_months >= PREF_CONT_LOW_MIN_MONTHS
            and total >= PREF_CONT_LOW_MIN_TOTAL
            and recent_count >= PREF_CONT_LOW_RECENT_MIN):
        return (
            "continuous_low",
            f"你长期持续关注{name}(跨{active_months}个月,共{total}次,低频但反复)"
        )

    # 规则4: occasional(偶尔关注)
    if (total >= PREF_OCCASIONAL_MIN_TOTAL
            and monthly_avg <= PREF_OCCASIONAL_MAX_MONTHLY_AVG
            and recent_count >= PREF_OCCASIONAL_RECENT_MIN):
        return (
            "occasional_interest",
            f"你偶尔关注{name}(共{total}次,月均{monthly_avg},近期仍在看)"
        )

    # 规则5: surging(阶段性重度使用)
    peak_month_count = max(months.values()) if months else 0
    if peak_month_count >= PREF_SURGING_MIN_PEAK and active_months <= PREF_SURGING_MAX_MONTHS:
        return (
            "surging_interest",
            f"你阶段性重度使用{name}(单月峰值{peak_month_count}次,集中在某段时间)"
        )

    # 兜底:有命中但不满足上述,记为"轻度关注"
    if total >= PREF_MIN_TOTAL:
        return (
            "light_interest",
            f"你轻度关注过{name}(共{total}次)"
        )

    return None


def build_preference_memory(con: sqlite3.Connection, now: str) -> dict:
    """主流程:抽取所有 preference 记忆。"""
    recent_months = _get_recent_months(con, 3)
    topic_data = collect_topic_hits(con)

    stats = {
        "started_at": now,
        "rules_version": RULES_VERSION,
        "recent_months": recent_months,
        "total_topics": len(TOPICS),
        "by_subtype": defaultdict(list),
        "skipped": [],
    }

    n_inserted = 0
    for topic_key, td in topic_data.items():
        result = classify_preference(td, recent_months)
        if result is None:
            stats["skipped"].append({
                "topic": td["name"],
                "total": td["total"],
                "reason": "命中太少(<5),信号不足",
            })
            continue

        subtype, description = result
        memory_id = sha256_text(f"preference|{subtype}|{topic_key}")
        evidence_ids = unique_evidence_ids(td["evidence_ids"], limit=MAX_EVIDENCE_LINKS)
        metadata = build_governance_metadata(
            source="preference:google",
            evidence_ids=evidence_ids,
            confidence=0.7,
            merge_key=f"preference|{subtype}|{topic_key}",
            last_seen=load_last_seen(con, evidence_ids),
            extra={
            "rules_version": RULES_VERSION,
            "topic_key": topic_key,
            "total_hits": td["total"],
            "active_months": len(td["months"]),
            "by_source": dict(td["by_source"]),
            "top_signals": td["signals"][:8],
            },
        )

        con.execute(
            "INSERT OR REPLACE INTO memory_items "
            "(memory_id, memory_type, memory_subtype, subject, description, "
            " confidence, evidence_count, metadata, created_at) "
            "VALUES (?, 'preference', ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, subtype, td["name"], description,
             0.7, td["total"], json.dumps(metadata, ensure_ascii=False), now)
        )
        for eid in evidence_ids:
            con.execute(
                "INSERT INTO memory_links (memory_id, target_type, target_id, relation) "
                "VALUES (?, 'event', ?, 'evidenced_by')",
                (memory_id, eid)
            )

        stats["by_subtype"][subtype].append({
            "memory_id": memory_id,
            "topic": td["name"],
            "description": description,
            "evidence_count": td["total"],
            "by_source": dict(td["by_source"]),
        })
        n_inserted += 1

    con.commit()
    stats["inserted"] = n_inserted
    return stats


def write_report(stats: dict, analysis_dir: Path) -> None:
    ensure_dirs([analysis_dir])
    lines = [
        "# 偏好记忆抽取报告(preference v1)",
        "",
        f"- 抽取时间: {stats['started_at']}",
        f"- 规则版本: {stats['rules_version']}",
        f"- 近3月判定: {', '.join(stats['recent_months'])}",
        f"- 主题总数: {stats['total_topics']}",
        f"- 抽取偏好数: {stats['inserted']}",
        "",
        "> 数据源: YouTube(看什么) + Gemini Apps(问什么) + Search(搜什么)",
        "> 方法: 信号词归并 + 噪音过滤 + 强度判定(复用tooling逻辑)",
        "",
        "## 抽取出的偏好(按强度档位)",
        "",
    ]

    subtype_labels = {
        "continuous_high": "持续-高频关注(长期高频,近期仍活跃)",
        "continuous_low": "持续-低频关注(长期反复,每次不多)",
        "declining_interest": "衰减关注(曾经关注,近期淡出)",
        "occasional_interest": "偶尔关注(低频但持续)",
        "surging_interest": "阶段性重度使用(短期密集投入)",
        "light_interest": "轻度关注(有命中但不显著)",
    }

    for subtype, label in subtype_labels.items():
        items = stats["by_subtype"].get(subtype, [])
        lines.append(f"### {label}({len(items)} 条)")
        lines.append("")
        if not items:
            lines.append("_(无)_")
            lines.append("")
            continue
        lines.append("| 主题 | 描述 | 证据数 | 来源分布 |")
        lines.append("|------|------|--------|---------|")
        for it in items:
            src_str = ", ".join(f"{k}:{v}" for k, v in it["by_source"].items())
            lines.append(f"| {it['topic']} | {it['description']} | {it['evidence_count']} | {src_str} |")
        lines.append("")

    if stats["skipped"]:
        lines.append(f"## 跳过的主题({len(stats['skipped'])} 个)")
        lines.append("")
        for sk in stats["skipped"]:
            lines.append(f"- {sk['topic']}: {sk['total']}次 - {sk['reason']}")
        lines.append("")

    lines.append("## 验证说明")
    lines.append("")
    lines.append("- 主题归并是否准确?有没有该归没归的?")
    lines.append("- 强度档位是否符合你的实际关注情况?")
    lines.append("- 来源分布(YT/Gemini/Search)是否合理?")
    lines.append("")
    lines.append("不准确的地方在每行后面标注反馈。")
    lines.append("")
    lines.append("---")
    lines.append(f"*规则版本 {RULES_VERSION} · 信号词归并+强度判定*")

    (analysis_dir / "preference_report.md").write_text("\n".join(lines), encoding="utf-8")
    stats_clean = {**stats, "by_subtype": dict(stats["by_subtype"])}
    write_json(analysis_dir / "preference_report.json", stats_clean)


def main() -> None:
    print("=" * 60)
    print("偏好记忆抽取 build_preference_memory.py")
    print(f"  memory_type: preference ({RULES_VERSION})")
    print(f"  数据源: {', '.join(PREF_SOURCES)}")
    print("=" * 60)

    if not UNIFIED_DB.exists():
        print(f"\n[ERROR] 统合库不存在: {UNIFIED_DB}")
        sys.exit(1)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(UNIFIED_DB)
    try:
        print("\n[1/4] 确保表结构存在...")
        ensure_schema(con)
        print("    [OK] memory_items 已就绪(与tooling共用)")

        print("\n[2/4] 清空旧 preference 记忆(幂等)...")
        reset_preference_memory(con)
        print("    [OK] 已清空")

        print("\n[3/4] 扫描信号词 + 归并主题 + 判强度...")
        stats = build_preference_memory(con, now)
        print(f"    [OK] {stats['total_topics']}个主题 -> 抽取 {stats['inserted']} 条偏好")
        for subtype, items in stats["by_subtype"].items():
            print(f"      {subtype}: {len(items)} 条")

        print("\n[4/4] 生成报告...")
        write_report(stats, ANALYSIS_DIR)
        print(f"    [OK] {ANALYSIS_DIR / 'preference_report.md'}")
    finally:
        con.close()

    print("\n" + "=" * 60)
    print("完成。请打开 preference_report.md 验证偏好抽取质量。")
    print("=" * 60)


if __name__ == "__main__":
    main()
