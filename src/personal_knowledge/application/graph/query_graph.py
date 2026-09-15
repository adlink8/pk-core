"""记忆图谱查询与可视化(里程碑5b)。

提供两类能力:
  1. 图遍历查询(命令行)
     - neighbors:  某节点的N跳邻居("从Codex出发2跳能到什么?")
     - path:        两节点最短路径("ASMR和GSD怎么关联?")
     - common:      共同邻居("哪些记忆同时连了Codex和Claude?")
     - hub:         枢纽节点(连接最多的)
     - component:   连通分量(图里有几个"岛")
  2. 交互式可视化(pyvis → HTML)
     浏览器打开,可拖拽/缩放/点击节点查看详情

运行:
  python integration\\scripts\\query_graph.py visualize           # 生成可视化HTML
  python integration\\scripts\\query_graph.py neighbors "Codex" 2 # 查2跳邻居
  python integration\\scripts\\query_graph.py path "Codex" "ASMR/助眠放松"
  python integration\\scripts\\query_graph.py common "Codex" "Claude"
  python integration\\scripts\\query_graph.py hub                 # 枢纽节点
"""

from __future__ import annotations

import json
import sqlite3
import sys
import webbrowser
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_knowledge.core.common import ensure_dirs

import networkx as nx


ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
from personal_knowledge.core.project_paths import STAGE1_PROFILE_DIR as ANALYSIS_DIR  # stage1 reports

# 节点配色(按 memory_type)
TYPE_COLORS = {
    "tooling": "#3b82f6",     # 蓝
    "preference": "#10b981",  # 绿
    "capability": "#f59e0b",  # 橙
    "fact": "#8b5cf6",        # 紫
    "project": "#ec4899",     # 粉
    "habit": "#06b6d4",       # 青
}

# 边配色(按 relation)
RELATION_COLORS = {
    "uses_tool": "#3b82f6",
    "relates_to_topic": "#10b981",
    "enables": "#8b5cf6",
    "embodies": "#06b6d4",
    "same_subject": "#ef4444",
}

LLM_STATUS_COLORS = {
    "accepted": "#f59e0b",
    "review": "#f97316",
}


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def table_columns(con: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({name})")}


def format_llm_edge_title(row: dict[str, Any]) -> str:
    reason = " ".join(str(row.get("reason") or "").split()).strip() or "(empty reason)"
    return (
        f"LLM judgment relation: {row['relation_type']}\n"
        f"status: {row['gate_status']}\n"
        f"confidence: {float(row['confidence'] or 0.0):.2f}\n"
        f"candidate_id: {row['candidate_id']}\n"
        f"reason: {reason}"
    )


def load_llm_relation_edges(con: sqlite3.Connection, memories: dict[str, dict]) -> tuple[list[dict[str, Any]], list[str]]:
    required_tables = ("memory_relation_candidates", "memory_relation_judgments")
    missing_tables = [name for name in required_tables if not table_exists(con, name)]
    if missing_tables:
        missing = ", ".join(missing_tables)
        return [], [f"[warn] 跳过 LLM relation 可视化: 缺少表 {missing}"]

    judgment_columns = table_columns(con, "memory_relation_judgments")
    if "candidate_reason" in judgment_columns:
        reason_expr = "j.candidate_reason"
    elif "reason" in judgment_columns:
        reason_expr = "j.reason"
    else:
        reason_expr = "''"

    sql = """
        SELECT
            c.candidate_id,
            c.source_memory_id,
            c.target_memory_id,
            j.relation_type,
            j.gate_status,
            j.confidence,
            {reason_expr} AS reason
        FROM memory_relation_judgments j
        JOIN memory_relation_candidates c ON c.candidate_id = j.candidate_id
        WHERE j.gate_status IN ('accepted', 'review')
          AND j.relation_type != 'no_relation'
        ORDER BY j.gate_status, j.confidence DESC, c.candidate_id
    """.format(reason_expr=reason_expr)
    try:
        cur = con.execute(sql)
        columns = [item[0] for item in cur.description or []]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        return [], [f"[warn] 跳过 LLM relation 可视化: {exc}"]

    edges = []
    skipped = 0
    for row in rows:
        source_id = row.get("source_memory_id")
        target_id = row.get("target_memory_id")
        if source_id not in memories or target_id not in memories:
            skipped += 1
            continue
        edges.append({
            "from_memory_id": source_id,
            "to_memory_id": target_id,
            "relation": row["relation_type"],
            "strength": float(row.get("confidence") or 0.0),
            "edge_source": "llm_judgment",
            "gate_status": row["gate_status"],
            "confidence": float(row.get("confidence") or 0.0),
            "candidate_id": row["candidate_id"],
            "reason": row.get("reason") or "",
            "label": f"LLM:{row['relation_type']}",
            "title": format_llm_edge_title(row),
        })

    warnings = []
    if skipped:
        warnings.append(f"[warn] 已跳过 {skipped} 条 LLM 边: 节点未出现在当前 memory_items 图中")
    return edges, warnings


def load_graph(
    con: sqlite3.Connection, *, include_llm_relations: bool = False
) -> tuple[nx.MultiDiGraph, dict, list[str]]:
    """从数据库加载图。返回 (G, node_info)。"""
    memories = {}
    for r in con.execute(
        "SELECT memory_id, memory_type, memory_subtype, subject, description "
        "FROM memory_items WHERE NOT (memory_type='capability' AND memory_subtype='minor_capability')"
    ):
        memories[r[0]] = {
            "memory_id": r[0], "memory_type": r[1], "memory_subtype": r[2],
            "subject": r[3], "description": r[4],
        }

    G = nx.MultiDiGraph()
    for mid, m in memories.items():
        G.add_node(mid, **m)
    for r in con.execute(
        "SELECT from_memory_id, to_memory_id, relation, strength FROM memory_relations"
    ):
        if r[0] in memories and r[1] in memories:
            G.add_edge(r[0], r[1], relation=r[2], strength=r[3], edge_source="rule")

    warnings: list[str] = []
    if include_llm_relations:
        llm_edges, llm_warnings = load_llm_relation_edges(con, memories)
        warnings.extend(llm_warnings)
        for edge in llm_edges:
            G.add_edge(
                edge["from_memory_id"],
                edge["to_memory_id"],
                relation=edge["relation"],
                strength=edge["strength"],
                edge_source=edge["edge_source"],
                gate_status=edge["gate_status"],
                confidence=edge["confidence"],
                candidate_id=edge["candidate_id"],
                reason=edge["reason"],
                label=edge["label"],
                title=edge["title"],
            )
    return G, memories, warnings


def find_node_by_subject(G: nx.MultiDiGraph, subject: str) -> str | None:
    """按 subject 模糊找节点。"""
    subject_lower = subject.lower().strip()
    # 精确匹配
    for n, d in G.nodes(data=True):
        if d.get("subject", "").lower() == subject_lower:
            return n
    # 包含匹配
    for n, d in G.nodes(data=True):
        if subject_lower in d.get("subject", "").lower():
            return n
    return None


def cmd_neighbors(G, args):
    """N跳邻居查询。"""
    if len(args) < 2:
        print("用法: neighbors <subject> <跳数>")
        return
    subject, hops = args[0], int(args[1])
    node = find_node_by_subject(G, subject)
    if not node:
        print(f"[not found] 找不到节点: {subject}")
        return

    # 用弱连通的N跳邻居(忽略方向)
    undirected = G.to_undirected()
    neighbors = set()
    current = {node}
    for h in range(hops):
        next_level = set()
        for n in current:
            next_level.update(undirected.neighbors(n))
        next_level -= neighbors | current | {node}
        neighbors |= next_level
        current = next_level
        if not current:
            break

    subj = G.nodes[node]["subject"]
    print(f"\n从 [{subj}] 出发 {hops} 跳邻居({len(neighbors)} 个):\n")
    # 按层级展示
    current = {node}
    visited = {node}
    for h in range(1, hops + 1):
        next_level = set()
        for n in current:
            for nb in undirected.neighbors(n):
                if nb not in visited:
                    next_level.add(nb)
                    visited.add(nb)
        if next_level:
            print(f"  第{h}跳:")
            for nb in next_level:
                d = G.nodes[nb]
                print(f"    [{d['memory_type']:11s}] {d['subject']}")
            current = next_level
        else:
            break


def cmd_path(G, args):
    """最短路径查询。"""
    if len(args) < 2:
        print("用法: path <subject1> <subject2>")
        return
    n1 = find_node_by_subject(G, args[0])
    n2 = find_node_by_subject(G, args[1])
    if not n1:
        print(f"[not found] 找不到节点: {args[0]}")
        return
    if not n2:
        print(f"[not found] 找不到节点: {args[1]}")
        return

    undirected = G.to_undirected()
    try:
        path = nx.shortest_path(undirected, n1, n2)
    except nx.NetworkXNoPath:
        s1 = G.nodes[n1]["subject"]
        s2 = G.nodes[n2]["subject"]
        print(f"\n[{s1}] 和 [{s2}] 之间没有路径(在不同连通分量)")
        return

    print(f"\n最短路径({len(path)-1} 跳):\n")
    for i, n in enumerate(path):
        d = G.nodes[n]
        prefix = "  起点" if i == 0 else ("  终点" if i == len(path) - 1 else f"  第{i}跳")
        print(f"{prefix}: [{d['memory_type']:11s}] {d['subject']}")
        if i < len(path) - 1:
            # 找边的relation
            edge_data = G.get_edge_data(n, path[i + 1]) or G.get_edge_data(path[i + 1], n)
            if edge_data:
                rels = list(edge_data.values())
                rel = rels[0].get("relation", "?")
                print(f"         └──{rel}──>")


def cmd_common(G, args):
    """共同邻居查询。"""
    if len(args) < 2:
        print("用法: common <subject1> <subject2>")
        return
    n1 = find_node_by_subject(G, args[0])
    n2 = find_node_by_subject(G, args[1])
    if not n1 or not n2:
        print("[not found] 找不到节点")
        return

    undirected = G.to_undirected()
    nb1 = set(undirected.neighbors(n1))
    nb2 = set(undirected.neighbors(n2))
    common = nb1 & nb2

    s1, s2 = G.nodes[n1]["subject"], G.nodes[n2]["subject"]
    print(f"\n[{s1}] 和 [{s2}] 的共同邻居({len(common)} 个):\n")
    for c in common:
        d = G.nodes[c]
        print(f"  [{d['memory_type']:11s}] {d['subject']}")
    if not common:
        print("  (无共同邻居)")


def cmd_hub(G, args):
    """枢纽节点(度数最高)。"""
    undirected = G.to_undirected()
    degrees = sorted(undirected.degree(), key=lambda x: -x[1])
    print(f"\n枢纽节点(连接最多,Top 15):\n")
    print(f"  {'记忆':30s} {'类型':12s} {'连接数'}")
    print(f"  {'-'*30} {'-'*12} {'-'*6}")
    for n, deg in degrees[:15]:
        if deg == 0:
            break
        d = G.nodes[n]
        print(f"  {d['subject'][:30]:30s} {d['memory_type']:12s} {deg}")


def cmd_component(G, args):
    """连通分量。"""
    undirected = G.to_undirected()
    components = sorted(nx.connected_components(undirected), key=len, reverse=True)
    print(f"\n连通分量({len(components)} 个):\n")
    for i, comp in enumerate(components[:5], 1):
        print(f"  分量{i}({len(comp)}节点):")
        for n in comp:
            d = G.nodes[n]
            print(f"    [{d['memory_type']:11s}] {d['subject']}")
        print()
    if len(components) > 5:
        print(f"  ... 还有 {len(components) - 5} 个小分量")


def cmd_visualize(G, args):
    """生成 pyvis 交互式可视化。"""
    from pyvis.network import Network

    has_llm_edges = any(
        ed.get("edge_source") == "llm_judgment"
        for _, _, _, ed in G.edges(keys=True, data=True)
    )
    out_path = ANALYSIS_DIR / ("memory_graph_llm.html" if has_llm_edges else "memory_graph.html")
    ensure_dirs([ANALYSIS_DIR])

    net = Network(
        height="900px", width="100%",
        bgcolor="#1a1a2e", font_color="white",
        directed=True,
        # 不用 select_menu/filter_menu——它们引用本地 lib/tom-select 会致空白
        select_menu=False, filter_menu=False,
        # cdn_resources="in_line" 把 vis-network JS 内嵌进 HTML,不依赖网络
        cdn_resources="in_line",
        neighborhood_highlight=True,
    )

    # 添加节点
    undirected = G.to_undirected()
    for n, d in G.nodes(data=True):
        mtype = d.get("memory_type", "?")
        color = TYPE_COLORS.get(mtype, "#999999")
        degree = undirected.degree(n)
        # 节点大小按度数(枢纽更大)
        size = 15 + degree * 4
        title = f"{d.get('description', '')}\n类型: {mtype}\n连接数: {degree}"
        net.add_node(
            n, label=d.get("subject", "?"), title=title,
            color=color, size=size, shape="dot",
        )

    # 添加边
    for u, v, k, ed in G.edges(keys=True, data=True):
        rel = ed.get("relation", "?")
        edge_source = ed.get("edge_source", "rule")
        if edge_source == "llm_judgment":
            status = ed.get("gate_status", "review")
            color = LLM_STATUS_COLORS.get(status, "#f59e0b")
            width = 1 + float(ed.get("confidence", 0.0)) * 3
            label = ed.get("label", f"LLM:{rel}")
            title = ed.get("title", rel)
            net.add_edge(
                u, v,
                label=label,
                color=color,
                width=width,
                title=title,
                dashes=True,
            )
        else:
            color = RELATION_COLORS.get(rel, "#666666")
            width = 1 + ed.get("strength", 0.5) * 2
            net.add_edge(u, v, label=rel, color=color, width=width, title=rel)

    # 物理布局(力导向)
    net.set_options(json.dumps({
        "physics": {
            "forceAtlas2Based": {
                "gravitationalConstant": -50,
                "centralGravity": 0.01,
                "springLength": 150,
                "springConstant": 0.05,
            },
            "minVelocity": 0.5,
            "solver": "forceAtlas2Based",
        },
        "interaction": {
            "hover": True,
            "tooltipDelay": 100,
            "navigationButtons": True,
            "keyboard": True,
        },
    }))

    net.generate_html(notebook=False)
    out_path.write_text(net.html or "", encoding="utf-8")
    print(f"\n可视化已生成: {out_path}")
    print(f"   节点 {G.number_of_nodes()} / 边 {G.number_of_edges()}")
    print(f"\n   图例(节点颜色):")
    for t, c in TYPE_COLORS.items():
        print(f"     - {t} ({c})")
    print(f"\n   打开 HTML 文件即可交互(拖拽/缩放/点击)。")

    # 自动打开浏览器
    try:
        webbrowser.open(out_path.as_uri())
        print(f"   opened in browser")
    except Exception:
        print(f"   (请手动打开)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="记忆图谱查询与可视化")
    parser.add_argument(
        "--include-llm-relations",
        action="store_true",
        help="visualize 时额外加载 memory_relation_candidates + memory_relation_judgments",
    )
    parser.add_argument("command", choices=["visualize", "neighbors", "path", "common", "hub", "component"],
                        help="命令")
    parser.add_argument("args", nargs="*", help="命令参数")
    a = parser.parse_args()

    if not UNIFIED_DB.exists():
        print(f"[missing] 统合库不存在: {UNIFIED_DB}")
        sys.exit(1)

    con = sqlite3.connect(UNIFIED_DB)
    try:
        G, memories, warnings = load_graph(con, include_llm_relations=a.include_llm_relations)
        print(f"图: {G.number_of_nodes()} 节点 / {G.number_of_edges()} 边")
        for warning in warnings:
            print(warning)
    finally:
        con.close()

    commands = {
        "visualize": cmd_visualize,
        "neighbors": cmd_neighbors,
        "path": cmd_path,
        "common": cmd_common,
        "hub": cmd_hub,
        "component": cmd_component,
    }
    commands[a.command](G, a.args)


if __name__ == "__main__":
    main()
