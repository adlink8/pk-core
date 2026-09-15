"""个人数据分析系统 —— 交互式仪表盘。

启动: streamlit run integration\\scripts\\dashboard.py
浏览器自动打开 http://localhost:8501

四个页面:
1. 总览       —— 三源事件总数、按月增长图、修复前后分类对比
2. 模块下钻    —— 选 Google/GPT/Agent,看该模块关注点/思考模式/服务分布,按月/服务下钻
3. 事件明细    —— 全量事件可搜索/过滤,显示真实对话内容(content_rich)
4. 跨模块链路  —— 展示"搜索→提问→执行"时序链(架构图核心承诺)

数据源: integration/db/personal_system.sqlite
        含增强表 unified_events_rich / event_categories_v2 / entity_links_v2
        (由 enrich_unified_events.py 生成)
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import personal_knowledge.core.rules as _rules  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
INTEGRATED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"


# === 数据加载(带缓存)===

@st.cache_data(ttl=300, show_spinner="加载统合数据...")
def load_events(use_merged: bool = False) -> pd.DataFrame:
    """加载 unified_events + 增强表(content_rich / category_v2)。

    use_merged: True=去重视图,排除合并层中非代表的重复成员,只留代表+独立事件。
    """
    import sqlite3
    con = sqlite3.connect(INTEGRATED_DB)
    has_rich = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_events_rich'"
    ).fetchone() is not None
    has_catv2 = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_categories_v2'"
    ).fetchone() is not None

    # 去重视图需要合并层
    if use_merged:
        has_merge = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='merge_members'"
        ).fetchone() is not None
        if not has_merge:
            st.warning("合并层未构建,回退到原始视图。请先运行 `build_merge_layer.py`。")
            use_merged = False

    if has_rich and has_catv2:
        sql = (
            "SELECT ue.*, r.content_rich, c.category_v2 "
            "FROM unified_events ue "
            "LEFT JOIN unified_events_rich r ON r.event_id = ue.event_id "
            "LEFT JOIN event_categories_v2 c ON c.event_id = ue.event_id"
        )
        if use_merged:
            sql += (
                " WHERE ue.event_id NOT IN ("
                "  SELECT event_id FROM merge_members WHERE is_representative = 0"
                ")"
            )
        df = pd.read_sql_query(sql, con)
    else:
        df = pd.read_sql_query("SELECT * FROM unified_events", con)
        st.warning(
            "未检测到增强表(content_rich / category_v2)。请先运行:\n"
            "`python -m personal_knowledge.application.enrich_unified_events`\n"
            "当前显示的是修复前的污染数据。"
        )
    con.close()
    return df


@st.cache_data(ttl=300, show_spinner="加载跨模块链路...")
def load_links() -> pd.DataFrame:
    import sqlite3
    con = sqlite3.connect(INTEGRATED_DB)
    has_v2 = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_links_v2'"
    ).fetchone() is not None
    if has_v2:
        df = pd.read_sql_query("SELECT * FROM entity_links_v2", con)
    else:
        df = pd.DataFrame()
    con.close()
    return df


def data_status() -> tuple[bool, bool, bool]:
    """检查增强表是否存在,返回 (has_rich, has_catv2, has_links_v2)。"""
    import sqlite3
    con = sqlite3.connect(INTEGRATED_DB)
    res = []
    for tbl in ("unified_events_rich", "event_categories_v2", "entity_links_v2"):
        res.append(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone() is not None)
    con.close()
    return tuple(res)


@st.cache_data(ttl=120, show_spinner=False)
def merge_layer_status() -> dict:
    """检查合并层状态。返回 {available, n_clusters, compression, meta}。"""
    import sqlite3
    con = sqlite3.connect(INTEGRATED_DB)
    try:
        n = con.execute("SELECT COUNT(*) FROM merge_clusters").fetchone()[0]
        if n == 0:
            return {"available": False, "n_clusters": 0, "compression": 0, "meta": {}}
        meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM merge_build_meta")}
        return {
            "available": True,
            "n_clusters": n,
            "compression": float(meta.get("compression", 0)),
            "n_input": int(float(meta.get("n_input", 0))),
            "effective_events": int(float(meta.get("effective_events", 0))),
            "l1_clusters": int(float(meta.get("l1_clusters", 0))),
            "l2_clusters": int(float(meta.get("l2_clusters", 0))),
            "meta": meta,
        }
    except sqlite3.OperationalError:
        return {"available": False, "n_clusters": 0, "compression": 0, "meta": {}}
    finally:
        con.close()


@st.cache_data(ttl=60, show_spinner=False)
def vector_store_status() -> dict:
    """检查向量库状态。返回 {available, count, error}。"""
    try:
        from personal_knowledge.core.chroma_client import ChromaClient, ChromaError
        client = ChromaClient()
        cols = {c["name"] for c in client.list_collections()}
        if "personal_events" not in cols:
            return {"available": False, "count": 0, "error": "collection personal_events 不存在"}
        coll = client.get_or_create_collection("personal_events")
        return {"available": True, "count": coll.count(), "error": ""}
    except Exception as e:
        return {"available": False, "count": 0, "error": str(e)[:100]}


# === 页面:总览 ===

def page_overview(df: pd.DataFrame) -> None:
    st.header("📊 总览")
    st.caption("三源数据全貌 + 修复前后对比")

    # KPI 卡
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("统一事件总数", f"{len(df):,}")
    c2.metric("活跃月份", df["month"].replace("", pd.NA).dropna().nunique())
    has_links = not load_links().empty
    if has_links:
        links_df = load_links()
        chain_count = len(links_df[links_df["relation"].str.contains("chain", na=False)])
        c3.metric("跨模块时序链", f"{chain_count:,}")
        c4.metric("跨模块链接总数", f"{len(links_df):,}")
    else:
        c3.metric("跨模块时序链", "—")
        c4.metric("跨模块链接", "—")

    st.divider()

    # 按月增长堆叠图
    st.subheader("三源按月增长")
    growth = df[df["month"].astype(str).str.len() >= 7].copy()
    growth["month"] = growth["month"].astype(str).str[:7]
    monthly = growth.groupby(["month", "source"]).size().reset_index(name="count")
    pivot = monthly.pivot(index="month", columns="source", values="count").fillna(0)
    # 按月排序
    pivot = pivot.sort_index()
    if not pivot.empty:
        import plotly.graph_objects as go
        fig = go.Figure()
        for col in pivot.columns:
            fig.add_trace(go.Bar(x=pivot.index, y=pivot[col], name=col))
        fig.update_layout(
            barmode="stack",
            xaxis_title="月份",
            yaxis_title="事件数",
            height=420,
            legend_title="数据源",
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("无可用月份数据。")

    st.divider()

    # 修复前后分类对比
    st.subheader("分类修复前后对比")
    st.caption(
        "修复前:老规则含工具名/元数据词(agent/codex/skills),Agent 模块必然自我命中。  \n"
        "修复后:纯净规则只对真实内容分类,反映用户真实在做什么。"
    )
    col_old, col_new = st.columns(2)
    with col_old:
        st.markdown("**修复前(category_v1 透传 / 老规则)**")
        old_cat = _classify_with_old_rules(df)
        st.dataframe(old_cat.head(12), use_container_width=True, hide_index=True)
    with col_new:
        st.markdown("**修复后(category_v2 纯净规则)**")
        if "category_v2" in df.columns:
            new_cat = df["category_v2"].fillna("其他 / 未分类").value_counts().reset_index()
            new_cat.columns = ["分类", "事件数"]
            st.dataframe(new_cat.head(12), use_container_width=True, hide_index=True)
        else:
            st.warning("无 category_v2(未运行 enrich)")


def _classify_with_old_rules(df: pd.DataFrame) -> pd.DataFrame:
    """用老规则(含元数据)对 title+content 重新分类,展示污染效果。"""
    def classify_row(row):
        text = f"{row.get('title','') or ''} {row.get('content','') or ''}".lower()
        for topic, keys in _rules.TOPIC_RULES:
            if any(k.lower() in text for k in keys):
                return topic
        return "其他 / 未分类"
    cats = df.apply(classify_row, axis=1).value_counts().reset_index()
    cats.columns = ["分类", "事件数"]
    return cats


# === 页面:模块下钻 ===

def page_module_drilldown(df: pd.DataFrame) -> None:
    st.header("🔍 模块下钻")
    st.caption("选择数据源,查看该模块的关注点、思考模式和服务分布")

    sources = sorted(df["source"].dropna().unique())
    source = st.selectbox("选择数据源", sources, key="drill_source")

    sub = df[df["source"] == source].copy()
    if sub.empty:
        st.warning(f"无 {source} 数据")
        return

    # 模块概览
    c1, c2, c3 = st.columns(3)
    c1.metric("事件数", f"{len(sub):,}")
    c2.metric("活跃月份", sub["month"].replace("", pd.NA).dropna().nunique())
    c3.metric("服务/工具数", sub["service"].replace("", pd.NA).dropna().nunique())

    st.divider()

    # 三个维度的分布(用 PURE 规则重算,保证下钻一致)
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**关注主题(纯净分类)**")
        topic_counts = Counter()
        for _, row in sub.iterrows():
            cat = row.get("category_v2") if "category_v2" in sub.columns else None
            if not cat:
                text = f"{row.get('title','') or ''} {row.get('content_rich','') or row.get('content','') or ''}".lower()
                cat = _rules.PURE_TOPIC_DEFAULT
                for topic, keys in _rules.PURE_TOPIC_RULES:
                    if any(k.lower() in text for k in keys):
                        cat = topic
                        break
            topic_counts[cat] += 1
        topic_df = pd.DataFrame(topic_counts.most_common(10), columns=["主题", "事件数"])
        st.dataframe(topic_df, use_container_width=True, hide_index=True)

    with col2:
        st.markdown("**思考模式(纯净)**")
        thinking_counts = Counter()
        for _, row in sub.iterrows():
            text = f"{row.get('title','') or ''} {row.get('content_rich','') or row.get('content','') or ''}".lower()
            label = _rules.PURE_THINKING_DEFAULT
            for lab, keys, _ in _rules.PURE_THINKING_RULES:
                if any(k.lower() in text for k in keys):
                    label = lab
                    break
            thinking_counts[label] += 1
        think_df = pd.DataFrame(thinking_counts.most_common(10), columns=["思考模式", "事件数"])
        st.dataframe(think_df, use_container_width=True, hide_index=True)

    with col3:
        st.markdown("**服务/工具分布**")
        svc_counts = sub["service"].replace("", "未标记").value_counts().head(10).reset_index()
        svc_counts.columns = ["服务", "事件数"]
        st.dataframe(svc_counts, use_container_width=True, hide_index=True)

    st.divider()

    # 交互式下钻:选月份 + 选服务,看真实事件
    st.subheader(f"{source} 事件下钻")
    months = sorted([m for m in sub["month"].dropna().unique() if isinstance(m, str) and len(m) >= 7])
    sel_month = st.selectbox("按月份过滤(可选'全部')", ["全部"] + months, key="drill_month")
    services = sorted(sub["service"].replace("", "未标记").unique())
    sel_service = st.selectbox("按服务过滤(可选'全部')", ["全部"] + services, key="drill_service")

    filtered = sub.copy()
    if sel_month != "全部":
        filtered = filtered[filtered["month"].astype(str).str[:7] == sel_month]
    svc_filter = filtered["service"].replace("", "未标记")
    if sel_service != "全部":
        filtered = filtered[svc_filter == sel_service]

    st.caption(f"匹配 {len(filtered)} 条事件")
    _show_event_table(filtered, limit=50)


def _show_event_table(df: pd.DataFrame, limit: int = 100) -> None:
    """以可读方式展示事件表(优先 content_rich)。"""
    if df.empty:
        st.info("无匹配事件")
        return
    show = df.head(limit).copy()
    # 选择展示列
    content_col = "content_rich" if "content_rich" in show.columns and show["content_rich"].notna().any() else "content"
    cat_col = "category_v2" if "category_v2" in show.columns else "category"
    display = show[["event_time", "source", "service", cat_col, "title", content_col]].copy()
    display.columns = ["时间", "源", "服务", "分类", "标题", "内容"]
    # 内容截断显示
    display["内容"] = display["内容"].fillna("").astype(str).str[:150]
    display["标题"] = display["标题"].fillna("").astype(str).str[:50]
    st.dataframe(display, use_container_width=True, hide_index=True, height=400)


# === 页面:事件明细 ===

def page_event_detail(df: pd.DataFrame) -> None:
    st.header("📋 事件明细")
    st.caption("全量事件搜索与过滤。这是查看你具体在做什么的核心入口。")

    # 过滤侧栏
    with st.expander("🔎 过滤条件", expanded=True):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            sources = sorted(df["source"].dropna().unique())
            sel_sources = st.multiselect("数据源", sources, default=sources, key="detail_sources")
        with col2:
            if "category_v2" in df.columns:
                cats = sorted(df["category_v2"].dropna().unique())
                sel_cats = st.multiselect("分类(v2)", cats, default=[], key="detail_cats")
            else:
                sel_cats = []
        with col3:
            months = sorted([m for m in df["month"].dropna().unique() if isinstance(m, str) and len(m) >= 7])
            sel_months = st.multiselect("月份", months, default=[], key="detail_months")
        with col4:
            services = sorted(df["service"].replace("", pd.NA).dropna().unique())
            sel_services = st.multiselect("服务", services, default=[], key="detail_services")
        keyword = st.text_input("关键词搜索(标题+内容)", "", key="detail_keyword")

    filtered = df[df["source"].isin(sel_sources)] if sel_sources else df
    if sel_cats:
        filtered = filtered[filtered["category_v2"].isin(sel_cats)]
    if sel_months:
        filtered = filtered[filtered["month"].astype(str).str[:7].isin(sel_months)]
    if sel_services:
        filtered = filtered[filtered["service"].isin(sel_services)]
    if keyword.strip():
        kw = keyword.strip().lower()
        mask = (
            filtered["title"].fillna("").astype(str).str.lower().str.contains(kw, regex=False)
            | filtered["content"].fillna("").astype(str).str.lower().str.contains(kw, regex=False)
        )
        if "content_rich" in filtered.columns:
            mask = mask | filtered["content_rich"].fillna("").astype(str).str.lower().str.contains(kw, regex=False)
        filtered = filtered[mask]

    st.caption(f"匹配 {len(filtered)} / {len(df)} 条事件")
    _show_event_table(filtered, limit=200)

    # 选中事件查看详情
    if not filtered.empty:
        st.divider()
        st.subheader("事件详情(点击查看完整内容)")
        titles = filtered.apply(
            lambda r: f"[{r['source']}] {(r.get('title') or '(无标题)')[:40]} | {r.get('event_time','')}", axis=1
        ).tolist()
        sel_idx = st.selectbox("选择事件", range(len(titles)), format_func=lambda i: titles[i], key="detail_sel")
        if sel_idx is not None:
            row = filtered.iloc[sel_idx]
            c1, c2 = st.columns([1, 2])
            with c1:
                st.markdown(f"**源**: {row['source']}")
                st.markdown(f"**类型**: {row.get('event_type','')}")
                st.markdown(f"**服务**: {row.get('service','')}")
                st.markdown(f"**时间**: {row.get('event_time','')}")
                st.markdown(f"**分类v2**: {row.get('category_v2','')}")
            with c2:
                st.markdown("**标题**")
                st.write(row.get("title") or "(无)")
                st.markdown("**内容(原始)**")
                st.text_area("", row.get("content") or "(无)", height=120, key="detail_raw_content", label_visibility="collapsed")
                if "content_rich" in row.index and row.get("content_rich"):
                    st.markdown("**内容(增强后,真实对话)**")
                    st.text_area("", row["content_rich"], height=200, key="detail_rich_content", label_visibility="collapsed")


# === 页面:跨模块链路 ===

def page_cross_module(df: pd.DataFrame) -> None:
    st.header("🔗 跨模块链路")
    st.caption("架构图核心承诺:把独立的模块数据建立起整体链接 —— 搜索→提问→执行")

    links = load_links()
    if links.empty:
        st.warning(
            "未检测到 entity_links_v2 表。请运行:\n"
            "`python -m personal_knowledge.application.enrich_unified_events`"
        )
        return

    # 链路类型分布
    st.subheader("链路类型分布")
    rel_counts = links["relation"].value_counts().reset_index()
    rel_counts.columns = ["链路类型", "数量"]
    rel_desc = {
        "search_to_execute_chain": "搜索→提问→执行(完整链)",
        "search_to_ask_chain": "搜索→提问(半链)",
        "shared_project_term": "共享项目名(跨模块同主题)",
        "shared_domain": "共享域名(跨模块同站点)",
        "same_domain_repeat": "同域名重复访问",
    }
    rel_counts["说明"] = rel_counts["链路类型"].map(rel_desc).fillna("")
    st.dataframe(rel_counts, use_container_width=True, hide_index=True)

    st.divider()

    # 时序链详情(只看 chain 类)
    chain_links = links[links["relation"].str.contains("chain", na=False)].copy()
    st.subheader(f"时序链详情({len(chain_links)} 条)")

    chain_type = st.selectbox(
        "链路类型",
        ["search_to_execute_chain", "search_to_ask_chain"] if not chain_links.empty else [],
        key="chain_type",
    )
    sel = chain_links[chain_links["relation"] == chain_type].head(30)

    if sel.empty:
        st.info("无该类型链路")
        return

    # 为每条链展示 from→to 事件
    for i, (_, row) in enumerate(sel.iterrows()):
        with st.expander(f"链 #{i+1}  |  {row['evidence']}", expanded=(i < 3)):
            from_ev = df[df["event_id"] == row["from_event_id"]]
            to_ev = df[df["event_id"] == row["to_event_id"]]
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**起点 [{row['from_event_id'][:8]}]**")
                if not from_ev.empty:
                    e = from_ev.iloc[0]
                    st.markdown(f"- 源: {e['source']}")
                    st.markdown(f"- 时间: {e.get('event_time','')}")
                    st.markdown(f"- 标题: {e.get('title','') or '(无)'}")
                    content = e.get("content_rich") or e.get("content") or ""
                    st.text(content[:200])
            with c2:
                st.markdown(f"**终点 [{row['to_event_id'][:8]}]**")
                if not to_ev.empty:
                    e = to_ev.iloc[0]
                    st.markdown(f"- 源: {e['source']}")
                    st.markdown(f"- 时间: {e.get('event_time','')}")
                    st.markdown(f"- 标题: {e.get('title','') or '(无)'}")
                    content = e.get("content_rich") or e.get("content") or ""
                    st.text(content[:200])

    st.divider()

    # 共享项目名(跨模块同主题)
    st.subheader("共享项目名(跨模块同主题)")
    shared = links[links["relation"] == "shared_project_term"].head(20)
    if shared.empty:
        st.info("无共享项目名链接")
    else:
        for _, row in shared.iterrows():
            st.markdown(f"**{row['matched_term']}** — {row['evidence']}")
            from_ev = df[df["event_id"] == row["from_event_id"]]
            to_ev = df[df["event_id"] == row["to_event_id"]]
            c1, c2 = st.columns(2)
            with c1:
                if not from_ev.empty:
                    e = from_ev.iloc[0]
                    st.caption(f"[{e['source']}] {(e.get('title') or '(无)')[:50]}")
# === 主入口 ===


def page_vector_search() -> None:
    """向量检索页:语义搜索用户历史事件。"""
    st.header("🧠 向量检索")
    st.caption("用自然语言语义搜索你的历史数据(跨 Google/GPT/Agent 三源)。这是阶段二的核心 —— 让 AI 能按需召回你的真实经历。")

    # 向量库状态
    vstatus = vector_store_status()
    if not vstatus["available"]:
        st.warning(
            f"向量库不可用:{vstatus['error']}\n\n"
            "请先运行:\n"
            "```\npython -m personal_knowledge.retrieval.build_vector_store\n```"
        )
        return

    c1, c2 = st.columns([1, 4])
    with c1:
        st.metric("向量库已索引", f"{vstatus['count']:,}")

    st.divider()

    # 检索界面
    col1, col2, col3 = st.columns([4, 1, 1])
    with col1:
        query = st.text_input(
            "🔍 输入查询(自然语言)",
            value="",
            placeholder="例如:PPT 排版怎么做 / 数据库调试 / 论文修改",
            key="vsearch_query",
        )
    with col2:
        source_filter = st.selectbox("数据源", ["全部", "Google", "GPT", "Agent"], key="vsearch_source")
    with col3:
        top_k = st.select_slider("返回条数", options=[3, 5, 8, 10, 15], value=5, key="vsearch_topk")

    if not query.strip():
        st.info("👆 输入查询开始语义搜索。首次查询会加载本地 embedding 模型(约 1 秒)。")
        # 示例查询
        st.markdown("**试试这些示例查询:**")
        examples = ["PPT 排版", "数据库报错怎么调试", "论文修改", "数据分析 sqlite", "Obsidian 笔记"]
        cols = st.columns(len(examples))
        for i, ex in enumerate(examples):
            if cols[i].button(ex, key=f"example_{i}"):
                st.session_state["vsearch_query"] = ex
                st.rerun()
        return

    # 执行检索
    with st.spinner("语义检索中...(本地 GPU 推理)"):
        try:
            from personal_knowledge.retrieval.search_vectors import search
            results = search(
                query,
                top_k=top_k,
                source=None if source_filter == "全部" else source_filter,
            )
        except Exception as e:
            st.error(f"检索失败:{e}")
            return

    if not results:
        st.warning("无匹配结果。可能是查询太特殊,或向量库还在构建中。")
        return

    st.success(f"找到 {len(results)} 条相关结果(按语义相似度降序)")

    # 展示结果卡片
    for i, r in enumerate(results, 1):
        # 相关度标记
        if r["score"] >= 0.7:
            badge = "🟢 高度相关"
        elif r["score"] >= 0.5:
            badge = "🟡 相关"
        else:
            badge = "⚪ 弱相关"

        title = r["title"] or "(无标题)"
        with st.expander(
            f"#{i} {badge} [{r['score']:.3f}] [{r['source']}] {title[:40]}",
            expanded=(i <= 3),
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("相似度", f"{r['score']:.3f}")
            c2.metric("数据源", r["source"])
            c3.metric("服务", r["service"] or "—")
            c4.metric("时间", r["event_time"][:10] if r["event_time"] else "—")

            st.markdown(f"**分类**:{r['category_v2']}")
            st.markdown(f"**标题**:{title}")
            st.markdown("**内容**:")
            content = r["content"]
            st.text(content[:500] + ("…" if len(content) > 500 else ""))


def main() -> None:
    st.set_page_config(
        page_title="个人数据分析仪表盘",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # 侧栏
    with st.sidebar:
        st.markdown("## 📊 个人数据分析")
        st.caption("本地交互式仪表盘")
        st.divider()
        has_rich, has_catv2, has_links = data_status()
        st.markdown("**数据状态**")
        st.markdown(f"{'✅' if has_rich else '⚠️'} content_rich 增强文本")
        st.markdown(f"{'✅' if has_catv2 else '⚠️'} category_v2 纯净分类")
        st.markdown(f"{'✅' if has_links else '⚠️'} entity_links_v2 跨模块链路")
        vstatus = vector_store_status()
        if vstatus["available"]:
            st.markdown(f"✅ 向量库({vstatus['count']:,} 条)")
        else:
            st.markdown("⚠️ 向量库(未构建)")

        # 合并层状态 + 视图切换
        st.divider()
        mstatus = merge_layer_status()
        if mstatus["available"]:
            st.markdown(f"✅ 合并层({mstatus['n_clusters']} 簇, 压缩 {mstatus['compression']:.1%})")
            view_mode = st.radio(
                "📊 视图模式",
                ["原始(全量)", "去重(合并层)"],
                index=0,
                key="view_mode",
                help="去重视图排除 L1/L2 重复成员,只留代表+独立事件。",
            )
            use_merged = view_mode.startswith("去重")
            with st.expander("合并层详情"):
                st.markdown(f"- 输入: {mstatus['n_input']:,}")
                st.markdown(f"- 等效: {mstatus['effective_events']:,}")
                st.markdown(f"- L1 真重复: {mstatus['l1_clusters']} 簇")
                st.markdown(f"- L2 同主题: {mstatus['l2_clusters']} 簇")
        else:
            st.markdown("⚠️ 合并层(未构建)")
            st.caption("运行 `build_merge_layer.py` 后可启用去重视图")
            use_merged = False

        if not (has_rich and has_catv2 and has_links):
            st.info(
                "增强表未完整。请运行:\n"
                "```\npython -m personal_knowledge.application.enrich_unified_events\n```"
            )
        st.divider()
        st.caption("数据源:\nintegration/db/\npersonal_system.sqlite")

    # 加载数据(根据视图模式)
    df = load_events(use_merged=use_merged)

    # 导航
    page = st.sidebar.radio(
        "页面",
        ["📊 总览", "🔍 模块下钻", "📋 事件明细", "🔗 跨模块链路", "🧠 向量检索"],
        key="nav",
    )

    if page == "📊 总览":
        page_overview(df)
    elif page == "🔍 模块下钻":
        page_module_drilldown(df)
    elif page == "📋 事件明细":
        page_event_detail(df)
    elif page == "🔗 跨模块链路":
        page_cross_module(df)
    elif page == "🧠 向量检索":
        page_vector_search()


if __name__ == "__main__":
    main()
