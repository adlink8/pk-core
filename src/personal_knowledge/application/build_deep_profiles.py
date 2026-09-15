from __future__ import annotations

import csv
import html
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

# 引入共享规则与工具(阶段1提取)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_knowledge.core import rules as _rules
from personal_knowledge.core.common import norm  # noqa: E402  (Phase 13: 移除本地重复 norm 定义)


ROOT = Path(__file__).resolve().parents[3]
INTEGRATED = ROOT / "integration"
INTEGRATED_ANALYSIS = INTEGRATED / "analysis"
INTEGRATED_DB = INTEGRATED / "db" / "personal_system.sqlite"

MODULES = {
    "Google": ROOT / "Google",
    "GPT": ROOT / "GPT",
    "Agent": ROOT / "Agent",
}

# 输出文件后缀:--use-merged 时加 "_dedup",避免覆盖全量产物。
# 由 main() 设置,read/write 函数读取。空串=全量模式(默认,向后兼容)。
OUTPUT_SUFFIX = ""

# 分类与思考模式规则已迁移到 rules.py(阶段1统一)。
# 使用 _rules.PURE_TOPIC_RULES / _rules.PURE_THINKING_RULES(剥离元数据污染)。
# 旧规则 _rules.TOPIC_RULES / _rules.THINKING_RULES 保留在 rules.py 中作对照基线。


def ensure_dirs() -> None:
    INTEGRATED_ANALYSIS.mkdir(parents=True, exist_ok=True)
    for module in MODULES.values():
        (module / "analysis").mkdir(parents=True, exist_ok=True)


def read_events(use_merged: bool = False) -> list[dict]:
    """读取统合事件,并 LEFT JOIN 语义增强层(content_rich / category_v2)。

    增强表由 enrich_unified_events.py 生成。若增强表不存在(用户只跑了
    build_integrated_system 没跑 enrich),回退到旧 content/category,保证向后兼容。

    use_merged: True=按合并层去重视图,只保留每个 L1/L2 簇的代表点 + 所有独立
        事件,重复成员排除。这样画像统计不会被高频重复事件(如 26 条相同的
        "文档产物")虚高。需要先运行 build_merge_layer.py 构建合并层;
        合并层不存在时静默回退到全量(并打印提示)。
    """
    con = sqlite3.connect(INTEGRATED_DB)
    con.row_factory = sqlite3.Row
    # 检测增强表是否存在,不存在则回退
    has_rich = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_events_rich'"
    ).fetchone() is not None
    has_catv2 = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_categories_v2'"
    ).fetchone() is not None

    # 合并层可用性(use_merged 模式下才检测)
    if use_merged:
        has_merge = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='merge_members'"
        ).fetchone() is not None
        if not has_merge:
            print("[warn] --use-merged 但合并层未构建,回退全量。"
                  "请先运行 build_merge_layer.py")
            use_merged = False

    if has_rich and has_catv2:
        if use_merged:
            # 去重视图:排除非代表的合并成员(只留代表 + 独立事件)。
            # 一个 event_id 被排除当且仅当:它在 merge_members 里,且
            # is_representative=0。代表点(is_representative=1)和不在
            # 合并表里的独立事件都保留。
            rows = [
                dict(r)
                for r in con.execute(
                    "select ue.*, r.content_rich, c.category_v2 "
                    "from unified_events ue "
                    "left join unified_events_rich r on r.event_id = ue.event_id "
                    "left join event_categories_v2 c on c.event_id = ue.event_id "
                    "where ue.event_id not in ("
                    "  select event_id from merge_members where is_representative = 0"
                    ")"
                )
            ]
        else:
            rows = [
                dict(r)
                for r in con.execute(
                    "select ue.*, r.content_rich, c.category_v2 "
                    "from unified_events ue "
                    "left join unified_events_rich r on r.event_id = ue.event_id "
                    "left join event_categories_v2 c on c.event_id = ue.event_id"
                )
            ]
    else:
        rows = [dict(r) for r in con.execute("select * from unified_events")]
    con.close()
    return rows


def read_table_counts() -> list[dict]:
    con = sqlite3.connect(INTEGRATED_DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("select * from input_tables order by source, table_name")]
    con.close()
    return rows


def event_text(event: dict) -> str:
    """拼接用于分类的文本。

    修复(阶段1):剥离 service / source_table 元数据字段。
    老版本拼入了这两个字段,导致 Agent 模块(其 service=Codex/Claude、
    source_table=sessions/skills 等几乎必然含 "agent/codex/skills/memory")
    被 TOPIC_RULES/THINKING_RULES 的元数据词 100% 自我命中。

    现在只用 title + content_rich(优先)/content + category_v2(优先)/category,
    让分类反映"用户真实在做什么",而不是"数据来自哪个工具"。
    """
    content = norm(event.get("content_rich")) or norm(event.get("content"))
    category = norm(event.get("category_v2")) or norm(event.get("category"))
    return " ".join(
        [
            norm(event.get("title")),
            content,
            category,
        ]
    )


def classify(text: str, rules: list[tuple[str, list[str], str]] | list[tuple[str, list[str]]], default: str) -> str:
    low = text.lower()
    for item in rules:
        label = item[0]
        keywords = item[1]
        if any(k.lower() in low for k in keywords):
            return label
    return default


def top_counter(rows: list[dict], key: str, limit: int = 12) -> list[dict]:
    counts = Counter(norm(r.get(key)) or "未标记" for r in rows)
    return [{"name": name, "count": count} for name, count in counts.most_common(limit)]


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def month_value(event: dict) -> str:
    month = norm(event.get("month"))
    if len(month) >= 7:
        return month[:7]
    event_time = norm(event.get("event_time"))
    if len(event_time) >= 7:
        return event_time[:7]
    return "未知"


def build_growth_rows(events: list[dict]) -> list[dict]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for event in events:
        counts[(month_value(event), event["source"])] += 1
    rows = [
        {"month": month, "source": source, "event_count": count}
        for (month, source), count in sorted(counts.items())
    ]
    return rows


def plot_growth(rows: list[dict], output: Path, title: str, sources: list[str] | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    months = sorted({r["month"] for r in rows if r["month"] != "未知"})
    if not months:
        return
    if sources is None:
        sources = sorted({r["source"] for r in rows})
    lookup = {(r["month"], r["source"]): int(r["event_count"]) for r in rows}
    plt.figure(figsize=(12, 5))
    bottom = [0 for _ in months]
    for source in sources:
        values = [lookup.get((m, source), 0) for m in months]
        plt.bar(months, values, bottom=bottom, label=source)
        bottom = [b + v for b, v in zip(bottom, values)]
    plt.title(title)
    plt.xlabel("Month")
    plt.ylabel("Event count")
    plt.xticks(rotation=45, ha="right")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output, dpi=160)
    plt.close()


def module_specific_metrics(source: str, module_events: list[dict], table_counts: list[dict]) -> list[dict]:
    rows = [
        {"metric": "统一事件数", "value": len(module_events), "note": "进入统合数据库的标准化事件数量"},
        {"metric": "活跃月份数", "value": len({month_value(e) for e in module_events if month_value(e) != "未知"}), "note": "可识别月份的覆盖范围"},
        {"metric": "原始表数量", "value": len([r for r in table_counts if r["source"] == source]), "note": "该模块进入统合层的输入表数量"},
    ]
    if source == "Google":
        rows.append({"metric": "主要含义", "value": "搜索、YouTube、Gemini、Maps", "note": "外部信息输入和兴趣行为"})
    elif source == "GPT":
        rows.append({"metric": "主要含义", "value": "ChatGPT 对话、消息、关键词、附件", "note": "显性提问、任务拆解和内容产出"})
    elif source == "Agent":
        rows.append({"metric": "主要含义", "value": "本机 Agent 会话、skills、memory、工具数据", "note": "执行系统、自动化能力和长期记忆"})
    return rows


def focus_rows(source: str, events: list[dict]) -> list[dict]:
    """统计关注主题/思考模式/服务/分类。

    修复(阶段1):主题和思考模式改用 PURE 规则(_rules.PURE_TOPIC_RULES /
    PURE_THINKING_RULES),且 event_text 已剥离元数据,修复 Agent 自我命中。
    保留"原始分类"维度透传 category_v2/category,供对照。
    """
    topic_counts = Counter()
    thinking_counts = Counter()
    service_counts = Counter()
    category_counts = Counter()
    for event in events:
        text = event_text(event)
        topic_counts[classify(text, _rules.PURE_TOPIC_RULES, _rules.PURE_TOPIC_DEFAULT)] += 1
        # 思考模式用 PURE 规则(三元组格式)
        thinking_rules_two = [(label, keys) for label, keys, _ in _rules.PURE_THINKING_RULES]
        thinking_counts[classify(text, thinking_rules_two, _rules.PURE_THINKING_DEFAULT)] += 1
        service_counts[norm(event.get("service")) or "未标记"] += 1
        category = norm(event.get("category_v2")) or norm(event.get("category")) or "未标记"
        category_counts[category] += 1

    rows = []
    for rank, (name, count) in enumerate(topic_counts.most_common(12), start=1):
        rows.append({"source": source, "dimension": "关注主题", "rank": rank, "name": name, "event_count": count})
    for rank, (name, count) in enumerate(thinking_counts.most_common(12), start=1):
        rows.append({"source": source, "dimension": "思考模式", "rank": rank, "name": name, "event_count": count})
    for rank, (name, count) in enumerate(service_counts.most_common(12), start=1):
        rows.append({"source": source, "dimension": "服务/工具", "rank": rank, "name": name, "event_count": count})
    for rank, (name, count) in enumerate(category_counts.most_common(12), start=1):
        rows.append({"source": source, "dimension": "原始分类", "rank": rank, "name": name, "event_count": count})
    return rows


def thinking_profile(events: list[dict], source: str = "All") -> list[dict]:
    """思考模式画像。

    修复(阶段1):改用 PURE_THINKING_RULES,剥离元数据污染。
    evidence 优先用 content_rich(真实对话)而非旧 content(可能是 uuid)。
    """
    counts = Counter()
    evidence = defaultdict(list)
    for event in events:
        text = event_text(event)
        thinking_rules_two = [(label, keys) for label, keys, _ in _rules.PURE_THINKING_RULES]
        label = classify(text, thinking_rules_two, _rules.PURE_THINKING_DEFAULT)
        counts[label] += 1
        if len(evidence[label]) < 3:
            title = norm(event.get("title")) or (norm(event.get("content_rich")) or norm(event.get("content")))[:90]
            evidence[label].append(title)

    explanations = {label: explanation for label, _, explanation in _rules.PURE_THINKING_RULES}
    rows = []
    total = max(sum(counts.values()), 1)
    for rank, (label, count) in enumerate(counts.most_common(), start=1):
        rows.append(
            {
                "source": source,
                "rank": rank,
                "thinking_pattern": label,
                "event_count": count,
                "share": round(count / total, 4),
                "interpretation": explanations.get(label, "未形成明确模式，可能需要更多内容语义分析。"),
                "evidence_examples": " | ".join(evidence[label]),
            }
        )
    return rows


def data_flow_rows(table_counts: list[dict], events: list[dict]) -> list[dict]:
    source_counts = Counter(e["source"] for e in events)
    rows = []
    for source in MODULES:
        raw_tables = [r for r in table_counts if r["source"] == source]
        rows.extend(
            [
                {
                    "source": source,
                    "flow_step": "1 原始数据",
                    "location": f"{source}/raw",
                    "description": "平台导出、工具本地文件、会话、memory、skills 等未加工数据。",
                    "record_count": "",
                },
                {
                    "source": source,
                    "flow_step": "2 结构化数据",
                    "location": f"{source}/structured",
                    "description": f"抽取为 SQLite/CSV。进入统合的输入表 {len(raw_tables)} 个。",
                    "record_count": sum(int(r["row_count"]) for r in raw_tables),
                },
                {
                    "source": source,
                    "flow_step": "3 分析数据",
                    "location": f"{source}/analysis",
                    "description": "生成模块报告、module_profile、增长图和关注点统计。",
                    "record_count": source_counts[source],
                },
                {
                    "source": source,
                    "flow_step": "4 统合模块",
                    "location": "integration/db/personal_system.sqlite",
                    "description": "统一事件、实体和跨模块关系，形成个人系统级画像。",
                    "record_count": source_counts[source],
                },
            ]
        )
    return rows


def markdown_table(rows: list[dict], columns: list[str], limit: int | None = None) -> str:
    shown = rows[:limit] if limit else rows
    lines = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in shown:
        lines.append("|" + "|".join(html.escape(str(row.get(c, ""))) for c in columns) + "|")
    return "\n".join(lines)


def esc(value: object) -> str:
    return html.escape(str(value))


def fmt_int(value: object) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def fmt_share(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return str(value)


def html_table(rows: list[dict], columns: list[str], labels: dict[str, str] | None = None, limit: int | None = None) -> str:
    labels = labels or {}
    shown = rows[:limit] if limit else rows
    thead = "".join(f"<th>{esc(labels.get(c, c))}</th>" for c in columns)
    body = []
    for row in shown:
        cells = []
        for col in columns:
            value = row.get(col, "")
            if col in {"event_count", "record_count", "value"} and str(value).isdigit():
                value = fmt_int(value)
            elif col == "share":
                value = fmt_share(value)
            cells.append(f"<td>{esc(value)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class=\"table-wrap\"><table><thead><tr>{thead}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def metric_cards(cards: list[dict]) -> str:
    rendered = []
    for card in cards:
        rendered.append(
            f"""
            <article class="metric-card">
              <div class="metric-label">{esc(card.get("label", ""))}</div>
              <div class="metric-value">{esc(card.get("value", ""))}</div>
              <div class="metric-note">{esc(card.get("note", ""))}</div>
            </article>
            """
        )
    return "<section class=\"metric-grid\">" + "".join(rendered) + "</section>"


def bar_list(rows: list[dict], name_key: str, value_key: str, limit: int = 8) -> str:
    shown = rows[:limit]
    max_value = max([int(r.get(value_key) or 0) for r in shown] or [1])
    items = []
    for row in shown:
        value = int(row.get(value_key) or 0)
        width = max(3, round(value / max_value * 100))
        items.append(
            f"""
            <div class="bar-row">
              <div class="bar-meta"><span>{esc(row.get(name_key, ""))}</span><strong>{fmt_int(value)}</strong></div>
              <div class="bar-track"><div class="bar-fill" style="width:{width}%"></div></div>
            </div>
            """
        )
    return "<div class=\"bar-list\">" + "".join(items) + "</div>"


def report_shell(title: str, subtitle: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f7fb;
      --panel: #ffffff;
      --panel-strong: #101828;
      --ink: #172033;
      --muted: #667085;
      --line: #e6e9f0;
      --blue: #2563eb;
      --cyan: #0891b2;
      --green: #16a34a;
      --amber: #d97706;
      --violet: #7c3aed;
      --shadow: 0 18px 45px rgba(22, 31, 51, .09);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, Arial, 'Microsoft YaHei', sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, rgba(37, 99, 235, .10), transparent 25%),
        radial-gradient(circle at 82% 0%, rgba(8, 145, 178, .10), transparent 28%),
        var(--bg);
      line-height: 1.58;
    }}
    .page {{ width: min(1180px, calc(100vw - 40px)); margin: 0 auto; padding: 28px 0 52px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
    .brand {{ display: flex; gap: 10px; align-items: center; font-weight: 760; letter-spacing: .2px; }}
    .brand-mark {{ width: 34px; height: 34px; border-radius: 8px; background: linear-gradient(135deg, var(--blue), var(--cyan)); box-shadow: var(--shadow); }}
    .timestamp {{ color: var(--muted); font-size: 13px; }}
    .nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 18px; }}
    .nav a {{ text-decoration: none; color: #344054; background: rgba(255,255,255,.78); border: 1px solid var(--line); padding: 8px 11px; border-radius: 999px; font-size: 13px; font-weight: 680; }}
    .nav a:hover {{ color: var(--blue); border-color: rgba(37,99,235,.35); }}
    .hero {{
      background: linear-gradient(135deg, #111827 0%, #1f3a67 58%, #0e7490 100%);
      border-radius: 18px;
      padding: 34px;
      color: white;
      box-shadow: var(--shadow);
      overflow: hidden;
      position: relative;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      right: -90px;
      top: -90px;
      width: 260px;
      height: 260px;
      border: 1px solid rgba(255,255,255,.18);
      transform: rotate(18deg);
    }}
    .kicker {{ color: #bae6fd; font-size: 13px; text-transform: uppercase; letter-spacing: .12em; font-weight: 740; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(30px, 4vw, 52px); line-height: 1.05; letter-spacing: 0; }}
    .hero p {{ width: min(780px, 100%); margin: 0; color: #dbeafe; font-size: 16px; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 18px 0; }}
    .metric-card {{ background: rgba(255,255,255,.92); border: 1px solid rgba(230,233,240,.9); border-radius: 14px; padding: 18px; box-shadow: 0 10px 24px rgba(22,31,51,.06); min-height: 126px; }}
    .metric-label {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .metric-value {{ font-size: 28px; font-weight: 780; color: #111827; }}
    .metric-note {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
    .section-grid {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(340px, .9fr); gap: 18px; margin-top: 18px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 22px; box-shadow: 0 8px 26px rgba(22,31,51,.05); }}
    .panel h2 {{ margin: 0 0 8px; font-size: 20px; letter-spacing: 0; }}
    .panel h3 {{ margin: 18px 0 8px; font-size: 15px; color: #344054; }}
    .lede {{ color: var(--muted); margin: 0 0 16px; }}
    .insight-list {{ margin: 0; padding-left: 20px; }}
    .insight-list li {{ margin: 8px 0; }}
    .chart-img {{ width: 100%; border: 1px solid var(--line); border-radius: 12px; background: #fff; }}
    .bar-list {{ display: grid; gap: 12px; }}
    .bar-meta {{ display: flex; justify-content: space-between; gap: 12px; font-size: 14px; }}
    .bar-meta span {{ overflow-wrap: anywhere; }}
    .bar-track {{ height: 9px; border-radius: 999px; background: #eef2f7; overflow: hidden; }}
    .bar-fill {{ height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--blue), var(--cyan)); }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 640px; background: white; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #f8fafc; color: #475467; font-size: 12px; font-weight: 760; }}
    tr:last-child td {{ border-bottom: none; }}
    .flow {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
    .flow-step {{ border: 1px solid var(--line); border-radius: 12px; padding: 12px; background: #fbfcff; }}
    .flow-step strong {{ display: block; font-size: 13px; margin-bottom: 4px; }}
    .flow-step span {{ color: var(--muted); font-size: 12px; }}
    .source-pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .pill {{ display: inline-flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 999px; background: #eef4ff; color: #1d4ed8; font-size: 13px; font-weight: 680; }}
    .footer-note {{ margin-top: 18px; color: var(--muted); font-size: 13px; }}
    details {{ margin-top: 14px; border: 1px solid var(--line); border-radius: 12px; background: #fbfcff; padding: 12px 14px; }}
    summary {{ cursor: pointer; font-weight: 720; color: #344054; }}
    details p {{ margin: 10px 0 0; color: var(--muted); }}
    @media (max-width: 900px) {{
      .metric-grid, .section-grid, .flow {{ grid-template-columns: 1fr; }}
      .hero {{ padding: 24px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <div class="topbar">
      <div class="brand"><span class="brand-mark"></span><span>Personal Data Intelligence</span></div>
      <div class="timestamp">Static HTML report · local evidence</div>
    </div>
    <nav class="nav">
      <a href="#summary">Overview</a>
      <a href="#growth">Growth</a>
      <a href="#attention">Attention</a>
      <a href="#thinking">Thinking</a>
      <a href="#flow">Flow</a>
      <a href="#appendix">Appendix</a>
    </nav>
    <section class="hero">
      <div class="kicker">Data Analytics Report</div>
      <h1>{esc(title)}</h1>
      <p>{esc(subtitle)}</p>
    </section>
    {body}
  </main>
</body>
</html>
"""


def render_module_html(source: str, metrics: list[dict], top_topics: list[dict], top_thinking: list[dict]) -> str:
    metric_lookup = {m["metric"]: m for m in metrics}
    cards = [
        {"label": "统一事件", "value": fmt_int(metric_lookup.get("统一事件数", {}).get("value", 0)), "note": "进入统合层的标准化事件"},
        {"label": "活跃月份", "value": metric_lookup.get("活跃月份数", {}).get("value", 0), "note": "可识别时间跨度"},
        {"label": "输入表", "value": metric_lookup.get("原始表数量", {}).get("value", 0), "note": "已接入统合层"},
        {"label": "模块角色", "value": source, "note": metric_lookup.get("主要含义", {}).get("value", "")},
    ]
    body = f"""
    {metric_cards(cards)}
    <section class="section-grid">
      <article id="summary" class="panel">
        <h2>Executive Summary</h2>
        <p class="lede">{esc(source)} module_profile描述这个来源在个人数据系统中的信号角色：它贡献什么数据、增长是否稳定、关注点集中在哪里，以及对个人 AI 上下文有什么用。</p>
        <ul class="insight-list">
          <li><strong>模块定位：</strong>{esc(metric_lookup.get("主要含义", {}).get("note", "该模块提供个人数据系统的一个独立侧面。"))}</li>
          <li><strong>证据基础：</strong>画像来自本地 SQLite/CSV 的结构化结果，HTML 只做展示，不改动raw。</li>
          <li><strong>使用方式：</strong>用于判断该来源是否需要追加导入、清理去重、或者作为个人模型上下文。</li>
        </ul>
      </article>
      <article id="growth" class="panel">
        <h2>数据增长</h2>
        <p class="lede">按月查看该来源进入统合层的事件增长。</p>
        <img class="chart-img" src="module_profile_growth_chart.png" alt="{esc(source)} 数据增长图">
      </article>
    </section>
    <section class="section-grid">
      <article id="attention" class="panel">
        <h2>关注点排行</h2>
        <p class="lede">按主题聚合后的高频信号。</p>
        {bar_list(top_topics, "name", "event_count", 8)}
      </article>
      <article id="thinking" class="panel">
        <h2>思考模式推断</h2>
        <p class="lede">基于行为文本的模式识别，不是心理诊断。</p>
        {html_table(top_thinking, ["rank", "thinking_pattern", "event_count", "share", "interpretation"], {"rank": "#", "thinking_pattern": "模式", "event_count": "事件", "share": "占比", "interpretation": "解释"}, 6)}
      </article>
    </section>
    <section id="appendix" class="panel">
      <h2>审计表</h2>
      {html_table(metrics, ["metric", "value", "note"], {"metric": "指标", "value": "值", "note": "说明"})}
      <details>
        <summary>Evidence files</summary>
        <p>module_profile.json、module_profile_focus.csv、module_profile_thinking_mode.csv、module_profile_growth_monthly.csv、module_profile_growth_chart.png。</p>
      </details>
    </section>
    """
    return report_shell(f"{source} module_profile", f"{source} 数据模块的行为信号、增长和关注点分析。", body)


def render_integrated_html(
    events: list[dict],
    source_counts: Counter,
    flow: list[dict],
    top_focus: list[dict],
    top_services: list[dict],
    thinking: list[dict],
) -> str:
    cards = [
        {"label": "统一事件", "value": fmt_int(len(events)), "note": "三源进入统合层的事件总量"},
        {"label": "Google", "value": fmt_int(source_counts.get("Google", 0)), "note": "外部信息输入与兴趣行为"},
        {"label": "GPT", "value": fmt_int(source_counts.get("GPT", 0)), "note": "提问、对话和内容生产"},
        {"label": "Agent", "value": fmt_int(source_counts.get("Agent", 0)), "note": "执行、skills、memory 和本机工具痕迹"},
    ]
    flow_by_source = defaultdict(list)
    for row in flow:
        flow_by_source[row["source"]].append(row)
    flow_html = []
    for source, rows in flow_by_source.items():
        steps = "".join(
            f"<div class=\"flow-step\"><strong>{esc(r['flow_step'])}</strong><span>{esc(r['location'])}</span></div>"
            for r in rows
        )
        flow_html.append(f"<h3>{esc(source)}</h3><div class=\"flow\">{steps}</div>")
    body = f"""
    {metric_cards(cards)}
    <section id="summary" class="panel">
      <h2>Executive Summary</h2>
      <p class="lede">profile把 Google、GPT、Agent 三类数据拼成一条个人数据链路：外部输入进入思考层，再进入本机 Agent 执行和长期能力沉淀。</p>
      <ul class="insight-list">
        <li><strong>数据流向已经闭环：</strong>raw进入结构化层，生成分析画像，再汇总到统合 SQLite。</li>
        <li><strong>关注点集中在 AI、工具链、项目工作流和数据系统：</strong>这些主题可以直接转化为个人 AI 助手的长期上下文。</li>
        <li><strong>思考模式偏执行系统建设：</strong>高频行为显示你更关注把混乱资料变成可运行链路，而不是只做一次性查询。</li>
      </ul>
      <div class="source-pills">
        <span class="pill">Google · input</span>
        <span class="pill">GPT · reasoning</span>
        <span class="pill">Agent · execution</span>
        <span class="pill">SQLite · memory spine</span>
      </div>
    </section>
    <section class="section-grid">
      <article id="growth" class="panel">
        <h2>数据增长</h2>
        <p class="lede">按月观察三类来源进入统合层的事件增长。</p>
        <img class="chart-img" src="profile_growth_chart.png" alt="统合数据增长图">
      </article>
      <article id="attention" class="panel">
        <h2>关注点</h2>
        <p class="lede">主题聚合后的主要注意力分布。</p>
        {bar_list(top_focus, "name", "event_count", 10)}
      </article>
    </section>
    <section class="section-grid">
      <article id="thinking" class="panel">
        <h2>个人思考模式</h2>
        <p class="lede">这是行为数据画像，用于复盘和建模，不是心理诊断。</p>
        {html_table(thinking[:8], ["rank", "thinking_pattern", "event_count", "share", "interpretation"], {"rank": "#", "thinking_pattern": "模式", "event_count": "事件", "share": "占比", "interpretation": "解释"})}
      </article>
      <article class="panel">
        <h2>服务和工具重心</h2>
        <p class="lede">帮助判断哪些平台正在成为你的个人系统入口。</p>
        {bar_list(top_services, "name", "event_count", 10)}
      </article>
    </section>
    <section id="flow" class="panel">
      <h2>数据流向</h2>
      <p class="lede">每个模块都遵循raw、structured、analysis、integration的证据链。</p>
      {''.join(flow_html)}
    </section>
    <section id="appendix" class="panel">
      <h2>Caveats and Assumptions</h2>
      <ul class="insight-list">
        <li>画像基于本地已接入 SQLite/CSV 的数据，不代表所有未导入数据。</li>
        <li>个人思考模式来自关键词和行为痕迹推断，适合做复盘和上下文工程，不适合当作心理诊断。</li>
        <li>后续重新导出数据后，先重建统合数据库，再重跑画像scripts即可刷新本报告。</li>
      </ul>
      <details>
        <summary>Evidence files</summary>
        <p>personal_system.sqlite、profile.json、profile_data_flow.csv、profile_growth_monthly.csv、profile_focus.csv、profile_module_focus.csv、profile_thinking_mode.csv、profile_growth_chart.png。</p>
      </details>
    </section>
    """
    return report_shell("个人数据profile", "从 Google、GPT、Agent 三类本地数据生成的个人系统级分析报告。", body)


def build_module_profile(source: str, events: list[dict], table_counts: list[dict]) -> dict:
    analysis_dir = MODULES[source] / "analysis"
    sfx = OUTPUT_SUFFIX
    module_growth = build_growth_rows(events)
    module_focus = focus_rows(source, events)
    module_thinking = thinking_profile(events, source)
    metrics = module_specific_metrics(source, events, table_counts)

    write_csv(analysis_dir / f"module_profile_metrics{sfx}.csv", metrics, ["metric", "value", "note"])
    write_csv(analysis_dir / f"module_profile_focus{sfx}.csv", module_focus, ["source", "dimension", "rank", "name", "event_count"])
    write_csv(analysis_dir / f"module_profile_growth_monthly{sfx}.csv", module_growth, ["month", "source", "event_count"])
    write_csv(analysis_dir / f"module_profile_thinking_mode{sfx}.csv", module_thinking, ["source", "rank", "thinking_pattern", "event_count", "share", "interpretation", "evidence_examples"])
    plot_growth(module_growth, analysis_dir / f"module_profile_growth_chart{sfx}.png", f"{source} monthly data growth{sfx}", [source])

    top_topics = [r for r in module_focus if r["dimension"] == "关注主题"][:8]
    top_thinking = module_thinking[:6]
    profile = {
        "source": source,
        "mode": "dedup" if sfx else "full",
        "metrics": metrics,
        "top_focus": top_topics,
        "thinking_profile": top_thinking,
        "growth_rows": module_growth,
        "caveat": "思考模式为基于本地数据文本的行为推断，不是心理诊断。",
    }
    write_json(analysis_dir / f"module_profile{sfx}.json", profile)

    md = f"""# {source} module_profile{('（' + sfx.lstrip('_') + '）') if sfx else ''}

## 结论

{source} 模块用于刻画个人数据系统中的一个侧面。它像一个神经系统里的感受器：记录外部输入、对话活动或 Agent 执行痕迹，然后把信号送入统合模块。

## 核心指标

{markdown_table(metrics, ["metric", "value", "note"])}

## 关注点

{markdown_table(top_topics, ["rank", "name", "event_count"])}

## 思考模式推断

说明：这是基于文本关键词和行为痕迹的画像推断，不是心理诊断。

{markdown_table(top_thinking, ["rank", "thinking_pattern", "event_count", "share", "interpretation"], 8)}

## 数据增长

- 明细：`module_profile_growth_monthly{sfx}.csv`
- 图表：`module_profile_growth_chart{sfx}.png`

## 该模块在个人系统中的用处

- 识别这个来源主要贡献什么类型的信号。
- 判断数据是否持续增长，是否需要批次化追加。
- 为profile提供可追溯证据，而不是只依赖主观记忆。
    """
    (analysis_dir / f"module_profile{sfx}.md").write_text(md, encoding="utf-8")
    (analysis_dir / f"module_profile{sfx}.html").write_text(render_module_html(source, metrics, top_topics, top_thinking), encoding="utf-8")
    return profile


def build_integrated_profile(events: list[dict], table_counts: list[dict], module_profiles: dict[str, dict]) -> None:
    sfx = OUTPUT_SUFFIX
    growth = build_growth_rows(events)
    focus = []
    for source in MODULES:
        focus.extend(focus_rows(source, [e for e in events if e["source"] == source]))
    integrated_focus = focus_rows("All", events)
    thinking = thinking_profile(events, "All")
    flow = data_flow_rows(table_counts, events)

    write_csv(INTEGRATED_ANALYSIS / f"profile_data_flow{sfx}.csv", flow, ["source", "flow_step", "location", "description", "record_count"])
    write_csv(INTEGRATED_ANALYSIS / f"profile_growth_monthly{sfx}.csv", growth, ["month", "source", "event_count"])
    write_csv(INTEGRATED_ANALYSIS / f"profile_focus{sfx}.csv", integrated_focus, ["source", "dimension", "rank", "name", "event_count"])
    write_csv(INTEGRATED_ANALYSIS / f"profile_module_focus{sfx}.csv", focus, ["source", "dimension", "rank", "name", "event_count"])
    write_csv(INTEGRATED_ANALYSIS / f"profile_thinking_mode{sfx}.csv", thinking, ["source", "rank", "thinking_pattern", "event_count", "share", "interpretation", "evidence_examples"])
    plot_growth(growth, INTEGRATED_ANALYSIS / f"profile_growth_chart{sfx}.png", f"Integrated monthly data growth{sfx}", list(MODULES))

    source_counts = Counter(e["source"] for e in events)
    profile = {
        "mode": "dedup" if sfx else "full",
        "event_count": len(events),
        "source_event_counts": dict(source_counts),
        "module_profiles": module_profiles,
        "top_focus": [r for r in integrated_focus if r["dimension"] == "关注主题"][:12],
        "thinking_profile": thinking[:8],
        "data_flow": flow,
        "caveat": "个人思考画像是行为数据推断，用于自我复盘、系统优化和模型上下文建设，不是医学或心理诊断。",
    }
    write_json(INTEGRATED_ANALYSIS / f"profile{sfx}.json", profile)

    top_focus = [r for r in integrated_focus if r["dimension"] == "关注主题"][:10]
    top_services = [r for r in integrated_focus if r["dimension"] == "服务/工具"][:10]
    md = f"""# profile{('（' + sfx.lstrip('_') + '）') if sfx else ''}

## 结论

当前个人数据系统已经从单点数据分析升级为三源profile：Google 代表外部信息输入，GPT 代表显性提问和内容生产，Agent 代表本机执行系统与长期能力沉淀。三者合在一起，形成“输入 - 思考 - 执行 - 记忆”的闭环。

## 数据流向

{markdown_table(flow, ["source", "flow_step", "location", "description", "record_count"])}

## 数据增长

- 明细：`profile_growth_monthly{sfx}.csv`
- 图表：`profile_growth_chart{sfx}.png`

当前进入统合层的事件数：{len(events)}。

{markdown_table([{"source": k, "event_count": v} for k, v in source_counts.most_common()], ["source", "event_count"])}

## 关注点

{markdown_table(top_focus, ["rank", "name", "event_count"])}

## 服务和工具重心

{markdown_table(top_services, ["rank", "name", "event_count"])}

## 个人思考模式

说明：以下是行为数据推断，不是心理诊断。它更像给自己的“认知操作系统”做性能剖析，类似看动漫角色的战斗风格统计：能说明常用招式和偏好，但不能穷尽整个人。

{markdown_table(thinking[:8], ["rank", "thinking_pattern", "event_count", "share", "interpretation"])}

## 具体用处

- 复盘：知道自己最近把注意力投向哪里，哪些主题只是浏览，哪些已经进入执行系统。
- 建模：可以作为个人 AI 助手的长期上下文，形成偏好、项目、工具链、学习主题的特征库。
- 决策：通过增长图判断哪些数据源在快速膨胀，优先做清理、去重、归档和自动化。
- 自我优化：观察“搜索输入 - GPT 思考 - Agent 执行”的链路是否闭环，避免只收藏、不产出。

## 产物索引

- `profile{sfx}.json`
- `profile_data_flow{sfx}.csv`
- `profile_growth_monthly{sfx}.csv`
- `profile_growth_chart{sfx}.png`
- `profile_focus{sfx}.csv`
- `profile_module_focus{sfx}.csv`
- `profile_thinking_mode{sfx}.csv`
"""
    (INTEGRATED_ANALYSIS / f"profile{sfx}.md").write_text(md, encoding="utf-8")
    (INTEGRATED_ANALYSIS / f"profile{sfx}.html").write_text(
        render_integrated_html(events, source_counts, flow, top_focus, top_services, thinking),
        encoding="utf-8",
    )


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="构建深度画像(module_profile + profile)")
    p.add_argument("--use-merged", action="store_true",
                   help="按合并层去重视图:只用代表点 + 独立事件做统计,"
                        "排除 L1/L2 重复成员,避免高频重复事件虚高计数。"
                        "产物文件加 _dedup 后缀,不覆盖全量版。")
    args = p.parse_args()

    global OUTPUT_SUFFIX
    OUTPUT_SUFFIX = "_dedup" if args.use_merged else ""

    ensure_dirs()
    events = read_events(use_merged=args.use_merged)
    table_counts = read_table_counts()
    mode_label = "去重视图" if args.use_merged else "全量"
    print(f"[build_deep_profiles] 模式={mode_label} 事件数={len(events)}")
    module_profiles = {}
    for source in MODULES:
        module_events = [event for event in events if event["source"] == source]
        module_profiles[source] = build_module_profile(source, module_events, table_counts)
    build_integrated_profile(events, table_counts, module_profiles)
    print(json.dumps({
        "status": "ok", "mode": mode_label, "events": len(events),
        "modules": list(MODULES),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
