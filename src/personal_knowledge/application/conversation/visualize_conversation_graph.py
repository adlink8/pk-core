"""Conversation graph 可视化导出。

从 conversation_graph.duckdb 读取当前 accepted 图谱，导出本地交互式 HTML。

运行:
  python -m personal_knowledge.application.conversation.visualize_conversation_graph
  python -m personal_knowledge.application.conversation.visualize_conversation_graph --open
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

import duckdb
import networkx as nx
from pyvis.network import Network

ROOT = Path(__file__).resolve().parents[4]
DUCKDB_PATH = ROOT / "integration" / "db" / "conversation_graph.duckdb"
OUT_HTML = ROOT / "integration" / "analysis" / "ai_context" / "conversation_graph.html"

NODE_COLORS = {
    "turn": "#4f46e5",
    "session": "#0f766e",
    "topic": "#d97706",
    "tool": "#dc2626",
}

EDGE_COLORS = {
    "same_problem": "#2563eb",
    "follow_up": "#10b981",
    "preference_signal": "#f59e0b",
    "subproblem_of": "#8b5cf6",
    "temporal_next": "#6b7280",
    "tool_used_for": "#ef4444",
    "contradiction": "#b91c1c",
    "turn_topic": "#94a3b8",
    "turn_tool": "#fca5a5",
}


def short(text: str, limit: int = 180) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def load_graph() -> nx.MultiDiGraph:
    if not DUCKDB_PATH.exists():
        raise FileNotFoundError(f"缺少图数据库: {DUCKDB_PATH}")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        g = nx.MultiDiGraph()

        for session_id, source, main_topic in con.execute(
            "select session_id, source, main_topic from g_session"
        ).fetchall():
            g.add_node(
                session_id,
                node_type="session",
                label=short(main_topic or session_id, 48),
                title=f"session_id: {session_id}\nsource: {source}\nmain_topic: {main_topic}",
                color=NODE_COLORS["session"],
            )

        for node_id, session_id, turn_id, turn_no, main_topic, narrative, source_refs_json in con.execute(
            "select node_id, session_id, turn_id, turn_no, main_topic, narrative, source_refs_json from g_turn"
        ).fetchall():
            refs = []
            try:
                refs = json.loads(source_refs_json or "[]")
            except json.JSONDecodeError:
                refs = []
            label = f"T{turn_no} {short(main_topic or turn_id or node_id, 34)}"
            title = (
                f"node_id: {node_id}\n"
                f"session_id: {session_id}\n"
                f"turn_id: {turn_id}\n"
                f"turn_no: {turn_no}\n"
                f"main_topic: {main_topic}\n\n"
                f"narrative:\n{short(narrative, 1200)}\n\n"
                f"source_refs:\n" + "\n".join(refs[:12])
            )
            g.add_node(
                node_id,
                node_type="turn",
                label=label,
                title=title,
                color=NODE_COLORS["turn"],
            )
            g.add_edge(session_id, node_id, relation_type="contains_turn", color="#475569", width=1)

        for (topic,) in con.execute("select topic from g_topic").fetchall():
            topic_id = f"topic::{topic}"
            g.add_node(
                topic_id,
                node_type="topic",
                label=short(topic, 42),
                title=f"topic: {topic}",
                color=NODE_COLORS["topic"],
            )

        for (tool_name,) in con.execute("select tool_name from g_tool").fetchall():
            tool_id = f"tool::{tool_name}"
            g.add_node(
                tool_id,
                node_type="tool",
                label=short(tool_name, 32),
                title=f"tool: {tool_name}",
                color=NODE_COLORS["tool"],
            )

        for node_id, topic in con.execute("select node_id, topic from e_turn_topic").fetchall():
            topic_id = f"topic::{topic}"
            g.add_edge(node_id, topic_id, relation_type="turn_topic", color=EDGE_COLORS["turn_topic"], width=1)

        for node_id, tool_name in con.execute("select node_id, tool_name from e_turn_tool").fetchall():
            tool_id = f"tool::{tool_name}"
            g.add_edge(node_id, tool_id, relation_type="turn_tool", color=EDGE_COLORS["turn_tool"], width=1)

        for row in con.execute(
            "select candidate_id, source_node_id, target_node_id, relation_type, confidence, evidence_refs_json from e_relation"
        ).fetchall():
            candidate_id, source_node_id, target_node_id, relation_type, confidence, evidence_refs_json = row
            try:
                refs = json.loads(evidence_refs_json or "[]")
            except json.JSONDecodeError:
                refs = []
            title = (
                f"candidate_id: {candidate_id}\n"
                f"relation_type: {relation_type}\n"
                f"confidence: {confidence}\n\n"
                f"evidence_refs:\n" + "\n".join(refs[:12])
            )
            g.add_edge(
                source_node_id,
                target_node_id,
                relation_type=relation_type,
                color=EDGE_COLORS.get(relation_type, "#64748b"),
                width=1 + float(confidence or 0.0) * 3,
                title=title,
                confidence=float(confidence or 0.0),
                candidate_id=candidate_id,
            )
        return g
    finally:
        con.close()


def render_html(g: nx.MultiDiGraph, open_browser: bool) -> Path:
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    net = Network(
        height="980px",
        width="100%",
        bgcolor="#0b1020",
        font_color="#e5e7eb",
        directed=True,
        select_menu=False,
        filter_menu=False,
        neighborhood_highlight=True,
        cdn_resources="in_line",
    )
    undirected = g.to_undirected()
    for node_id, data in g.nodes(data=True):
        degree = undirected.degree(node_id)
        size = 16 + min(degree, 8) * 3
        shape = "dot"
        if data.get("node_type") == "session":
            shape = "box"
            size = 22
        elif data.get("node_type") == "topic":
            shape = "diamond"
        elif data.get("node_type") == "tool":
            shape = "triangle"
        net.add_node(
            node_id,
            label=data.get("label", node_id),
            title=data.get("title", node_id),
            color=data.get("color", "#64748b"),
            shape=shape,
            size=size,
        )
    for source, target, edge_data in g.edges(data=True):
        relation_type = edge_data.get("relation_type", "")
        net.add_edge(
            source,
            target,
            label=relation_type,
            title=edge_data.get("title", relation_type),
            color=edge_data.get("color", "#64748b"),
            width=edge_data.get("width", 1),
            arrows="to",
        )
    net.set_options(json.dumps({
        "physics": {
            "forceAtlas2Based": {
                "gravitationalConstant": -70,
                "centralGravity": 0.01,
                "springLength": 160,
                "springConstant": 0.04,
            },
            "minVelocity": 0.5,
            "solver": "forceAtlas2Based",
        },
        "interaction": {
            "hover": True,
            "tooltipDelay": 120,
            "navigationButtons": True,
            "keyboard": True,
        },
        "edges": {
            "smooth": False,
            "font": {"size": 10, "align": "middle"}
        }
    }))
    net.generate_html(notebook=False)
    OUT_HTML.write_text(net.html or "", encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(OUT_HTML.as_uri())
        except Exception:
            pass
    return OUT_HTML


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 conversation graph 交互式 HTML")
    parser.add_argument("--open", action="store_true", help="导出后尝试自动打开浏览器")
    args = parser.parse_args(argv)

    g = load_graph()
    out = render_html(g, open_browser=args.open)
    relation_edges = sum(1 for _, _, d in g.edges(data=True) if d.get("relation_type") not in {"contains_turn", "turn_topic", "turn_tool"})
    print(f"[write] {out}")
    print(f"nodes={g.number_of_nodes()} edges={g.number_of_edges()} relation_edges={relation_edges}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
