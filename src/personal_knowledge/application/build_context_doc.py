"""生成 AI 长期上下文文档 person_profile.md(阶段二)。

从统合库(画像/统计)和 chroma(向量库)派生一份精炼的用户画像文档,
可直接注入 AI 助手的 system prompt,让 AI 不用重复询问就了解:
- 这个用户是谁、关注什么
- 用什么工具、什么工作模式
- 有哪些活跃项目
- 如何按需检索更多细节

所有内容从数据派生,非主观判断。文档定位是"个人数据系统画像",
不是心理诊断(沿用阶段一 README 的边界声明)。

输出: integration/analysis/ai_context/person_profile.md

运行: python integration\\scripts\\build_context_doc.py
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from personal_knowledge.core.chroma_client import ChromaClient, ChromaError


ROOT = Path(__file__).resolve().parents[3]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
OUT_DIR = ROOT / "integration" / "analysis" / "ai_context"
OUT_FILE = OUT_DIR / "person_profile.md"
COLLECTION_NAME = "personal_events"


def load_stats(db_path: Path = UNIFIED_DB) -> dict:
    """从统合库加载画像统计(复用阶段一的纯净分类和思考模式)。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    stats: dict = {}

    # 总览
    stats["total_events"] = con.execute("SELECT COUNT(*) FROM unified_events").fetchone()[0]
    stats["source_counts"] = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT source, COUNT(*) FROM unified_events GROUP BY source ORDER BY 2 DESC"
        )
    }
    months = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT month FROM unified_events WHERE length(month)>=7 ORDER BY month"
        )
        if r[0]
    ]
    stats["month_range"] = (months[0], months[-1]) if months else ("?", "?")
    stats["active_months"] = len(months)

    # 纯净分类 top
    has_catv2 = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_categories_v2'"
    ).fetchone()
    if has_catv2:
        stats["top_categories"] = [
            {"name": r[0], "count": r[1]}
            for r in con.execute(
                "SELECT c.category_v2, COUNT(*) n FROM event_categories_v2 c "
                "JOIN unified_events ue ON ue.event_id=c.event_id "
                "GROUP BY c.category_v2 ORDER BY n DESC LIMIT 8"
            )
        ]

    # 服务/工具 top
    stats["top_services"] = [
        {"name": r[0] or "(未标记)", "count": r[1]}
        for r in con.execute(
            "SELECT service, COUNT(*) n FROM unified_events "
            "GROUP BY service ORDER BY n DESC LIMIT 10"
        )
    ]

    # 思考模式 top(从 build_deep_profiles 输出的 CSV 读,或这里重算)
    # 这里简单重算:用 content_rich + PURE 规则
    import personal_knowledge.core.rules as rules
    has_rich = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_events_rich'"
    ).fetchone()
    if has_rich:
        thinking = Counter()
        for r in con.execute(
            "SELECT ue.title, r.content_rich FROM unified_events ue "
            "LEFT JOIN unified_events_rich r ON r.event_id=ue.event_id"
        ):
            text = f"{r['title'] or ''} {r['content_rich'] or ''}".lower()
            label = rules.PURE_THINKING_DEFAULT
            for lab, keys, _ in rules.PURE_THINKING_RULES:
                if any(k.lower() in text for k in keys):
                    label = lab
                    break
            thinking[label] += 1
        total_t = sum(thinking.values()) or 1
        stats["top_thinking"] = [
            {"name": lab, "count": cnt, "share": round(cnt / total_t, 3)}
            for lab, cnt in thinking.most_common(7)
        ]

    # 跨模块链路统计
    has_links_v2 = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_links_v2'"
    ).fetchone()
    if has_links_v2:
        stats["chain_links"] = con.execute(
            "SELECT COUNT(*) FROM entity_links_v2 WHERE relation LIKE '%chain%'"
        ).fetchone()[0]
        stats["shared_terms"] = con.execute(
            "SELECT COUNT(*) FROM entity_links_v2 WHERE relation='shared_project_term'"
        ).fetchone()[0]

    con.close()
    return stats


def load_vector_stats() -> dict:
    """从 chroma 加载向量库统计。"""
    try:
        client = ChromaClient()
        cols = {c["name"]: c for c in client.list_collections()}
        if COLLECTION_NAME not in cols:
            return {"available": False}
        coll = client.get_or_create_collection(COLLECTION_NAME)
        count = coll.count()
        # 抽样元数据分布
        sample = coll.get(limit=500, include=["metadatas"])
        source_dist = Counter(m.get("source", "?") for m in sample.get("metadatas", []))
        cat_dist = Counter(m.get("category_v2", "?") for m in sample.get("metadatas", []))
        return {
            "available": True,
            "count": count,
            "source_dist": dict(source_dist.most_common()),
            "top_cats_sample": dict(cat_dist.most_common(5)),
        }
    except (ChromaError, Exception) as e:
        return {"available": False, "error": str(e)[:100]}


def extract_active_projects(stats: dict) -> list[str]:
    """从跨模块共享项目名提炼活跃项目主题。"""
    # 从 top_categories 里挑出"项目/工作流"类,以及有明显主题的
    projects = []
    for cat in stats.get("top_categories", []):
        name = cat["name"]
        if "项目" in name or "工作流" in name or "工业" in name or "学习" in name:
            projects.append(f"{name}({cat['count']}条)")
    return projects[:5]


def build_markdown(stats: dict, vstats: dict) -> str:
    """组装 person_profile.md 内容。"""
    lines: list[str] = []
    p = lines.append

    p("# 个人系统画像(AI 长期上下文)")
    p("")
    p("> 本文档由个人数据分析系统自动生成,全部内容从本地行为数据派生。")
    p("> 用于作为 AI 助手的长期上下文,帮助 AI 理解用户的工具偏好、关注主题和工作模式。")
    p("> 这是行为数据的统计画像,**不是心理诊断**。")
    p("")

    # 数据概览
    p("## 数据概览")
    p("")
    p(f"- 统一事件总数:**{stats['total_events']:,}** 条")
    sc = stats["source_counts"]
    src_str = " / ".join(f"{k} {v:,}" for k, v in sc.items())
    p(f"- 三源分布:{src_str}")
    mr = stats["month_range"]
    p(f"- 时间跨度:{mr[0]} ~ {mr[1]}(活跃 {stats['active_months']} 个月)")
    if vstats.get("available"):
        p(f"- 向量库已索引:**{vstats['count']:,}** 条可语义检索事件")
    p("")

    # 工具偏好
    p("## 工具偏好")
    p("")
    p("用户最常使用的服务和/工具(按事件数):")
    p("")
    p("| 排名 | 工具/服务 | 事件数 |")
    p("|---|---|---|")
    for i, svc in enumerate(stats["top_services"][:8], 1):
        p(f"| {i} | {svc['name']} | {svc['count']:,} |")
    p("")

    # 关注主题
    if stats.get("top_categories"):
        p("## 关注主题")
        p("")
        p("用户主要关注的内容领域(基于真实行为内容分类,已剔除工具元数据污染):")
        p("")
        p("| 主题 | 事件数 |")
        p("|---|---|")
        for cat in stats["top_categories"]:
            p(f"| {cat['name']} | {cat['count']:,} |")
        p("")

    # 思考模式
    if stats.get("top_thinking"):
        p("## 工作/思考模式")
        p("")
        p("从行为痕迹推断的工作倾向(非心理诊断,是「认知操作系统」的性能剖析):")
        p("")
        p("| 模式 | 事件数 | 占比 |")
        p("|---|---|---|")
        for t in stats["top_thinking"]:
            p(f"| {t['name']} | {t['count']:,} | {t['share']*100:.1f}% |")
        p("")

    # 跨模块协作
    if stats.get("chain_links"):
        p("## 跨模块协作")
        p("")
        chain_n = stats["chain_links"]
        p(f"- 已识别 **{chain_n}** 条「搜索→提问→执行」完整链路")
        p("  (用户在 Google 搜索后,经 GPT 提问,最终在 Agent 工具中执行)")
        if stats.get("shared_terms"):
            shared_n = stats["shared_terms"]
            p(f"- 跨模块共享项目主题 **{shared_n}** 个")
        p("")

    # 活跃项目
    projects = extract_active_projects(stats)
    if projects:
        p("## 活跃项目主题")
        p("")
        p("从数据中识别出的活跃工作方向:")
        p("")
        for proj in projects:
            p(f"- {proj}")
        p("")

    # 使用说明(给 AI)
    p("## AI 使用说明")
    p("")
    p("### 如何检索用户历史")
    p("当需要了解用户过去如何处理某类问题时,调用语义检索:")
    p("")
    p("```python")
    p("# Run from the project root with the installed personal_knowledge package")
    p("from vector.search_vectors import search")
    p("# 返回与查询语义最相关的历史事件(跨 Google/GPT/Agent 三源)")
    p("results = search('PPT 排版怎么做', top_k=5)")
    p("# 可按源过滤: search('数据库调试', source='Agent')")
    p("```")
    p("")
    p("### 数据源含义")
    p("- **Google**:外部信息输入(搜索、YouTube、地图、Gemini 提问)")
    p("- **GPT**:显性提问与内容生产(ChatGPT 对话)")
    p("- **Agent**:本机执行系统(Codex/Claude/Cursor 等工具的会话、skills、memory)")
    p("- 三者构成「输入 → 思考 → 执行 → 记忆」的闭环")
    p("")
    p("### 注意事项")
    p("- 事件内容(content_rich)是真实行为记录,可作为用户偏好的客观证据")
    p("- 分类(category_v2)已剔除工具元数据污染,反映真实内容主题")
    p("- score 是语义相似度(0~1,越高越相关),1.0 表示语义完全一致")
    p("")
    p("---")
    p(f"*生成时间:{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    p(f"*数据源:personal_system.sqlite + chroma {COLLECTION_NAME}*")

    return "\n".join(lines)


def main() -> None:
    print("=" * 60)
    print("生成 AI 长期上下文文档 person_profile.md")
    print("=" * 60)

    print("[1/3] 加载统合库统计...")
    stats = load_stats()
    print(f"    事件 {stats['total_events']:,} 条, 活跃 {stats['active_months']} 月")

    print("[2/3] 加载向量库统计...")
    vstats = load_vector_stats()
    if vstats.get("available"):
        print(f"    向量库 {vstats['count']:,} 条")
    else:
        print(f"    向量库不可用({vstats.get('error', '未构建')})")

    print("[3/3] 生成文档...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md = build_markdown(stats, vstats)
    OUT_FILE.write_text(md, encoding="utf-8")
    print(f"    -> {OUT_FILE} ({len(md)} 字符)")

    print()
    print("=" * 60)
    print("完成。person_profile.md 可直接注入 AI system prompt。")
    print("=" * 60)


if __name__ == "__main__":
    main()
