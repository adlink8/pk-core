"""Wave 9.4: 仅用 accepted judgments 重建 conversation_graph.duckdb。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SQLITE_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
DUCKDB_PATH = ROOT / "integration" / "db" / "conversation_graph.duckdb"
SUMMARIES_JSON = ROOT / "integration" / "analysis" / "ai_context" / "conversation_summaries.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS g_session (
    session_id TEXT PRIMARY KEY,
    source TEXT,
    main_topic TEXT
);
CREATE TABLE IF NOT EXISTS g_turn (
    node_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    turn_no INTEGER,
    main_topic TEXT,
    narrative TEXT,
    source_refs_json TEXT
);
CREATE TABLE IF NOT EXISTS g_topic (
    topic TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS g_tool (
    tool_name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS e_relation (
    candidate_id TEXT PRIMARY KEY,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence DOUBLE,
    evidence_refs_json TEXT,
    source_session_id TEXT,
    target_session_id TEXT
);
CREATE TABLE IF NOT EXISTS e_turn_topic (
    node_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    PRIMARY KEY (node_id, topic)
);
CREATE TABLE IF NOT EXISTS e_turn_tool (
    node_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    PRIMARY KEY (node_id, tool_name)
);
"""


def load_turn_map() -> dict[str, dict]:
    data = json.loads(SUMMARIES_JSON.read_text(encoding="utf-8"))
    out = {}
    for session in data:
        sid = session["session_id"]
        topic = session.get("main_topic", "")
        source = session.get("meta", {}).get("source", "")
        for turn_no, turn in enumerate(session.get("turn_summaries", []), 1):
            node_id = f"{sid}#{turn.get('turn_id') or f't{turn_no}'}"
            out[node_id] = {
                "node_id": node_id,
                "session_id": sid,
                "turn_id": turn.get("turn_id") or "",
                "turn_no": turn_no,
                "main_topic": topic,
                "narrative": (turn.get("narrative") or "").strip(),
                "source_refs": list(dict.fromkeys(turn.get("source_refs") or [])),
                "tools_used": list(dict.fromkeys(turn.get("tools_used") or [])),
                "source": source,
            }
    return out


def load_accepted_edges() -> list[dict]:
    con = sqlite3.connect(SQLITE_DB)
    con.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT j.candidate_id, j.relation_type, j.confidence, j.evidence_refs_json, "
            "c.source_node_id, c.target_node_id, c.source_session_id, c.target_session_id "
            "FROM graph_relation_judgments j "
            "JOIN graph_relation_candidates c ON c.candidate_id = j.candidate_id "
            "WHERE j.gate_status='accepted' ORDER BY j.candidate_id"
        )
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def build(write: bool) -> dict:
    import duckdb

    turn_map = load_turn_map()
    edges = load_accepted_edges()
    node_ids = set()
    for e in edges:
        node_ids.add(e["source_node_id"])
        node_ids.add(e["target_node_id"])

    sessions = {}
    topics = set()
    tools = set()
    turns = []
    for node_id in sorted(node_ids):
        t = turn_map.get(node_id)
        if not t:
            continue
        sessions[t["session_id"]] = {"source": t["source"], "main_topic": t["main_topic"]}
        if t["main_topic"]:
            topics.add(t["main_topic"])
        tools.update(t["tools_used"])
        turns.append(t)

    stats = {
        "accepted_edges": len(edges),
        "turn_nodes": len(turns),
        "session_nodes": len(sessions),
        "topic_nodes": len(topics),
        "tool_nodes": len(tools),
    }
    if not write:
        return stats

    if DUCKDB_PATH.exists():
        DUCKDB_PATH.unlink()
    con = duckdb.connect(str(DUCKDB_PATH))
    try:
        con.execute(SCHEMA_SQL)
        for sid, meta in sessions.items():
            con.execute("INSERT INTO g_session VALUES (?, ?, ?)", (sid, meta["source"], meta["main_topic"]))
        for t in turns:
            con.execute(
                "INSERT INTO g_turn VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    t["node_id"], t["session_id"], t["turn_id"], t["turn_no"],
                    t["main_topic"], t["narrative"], json.dumps(t["source_refs"], ensure_ascii=False),
                ),
            )
            if t["main_topic"]:
                con.execute("INSERT OR IGNORE INTO g_topic VALUES (?)", (t["main_topic"],))
                con.execute("INSERT OR IGNORE INTO e_turn_topic VALUES (?, ?)", (t["node_id"], t["main_topic"]))
            for tool in t["tools_used"]:
                con.execute("INSERT OR IGNORE INTO g_tool VALUES (?)", (tool,))
                con.execute("INSERT OR IGNORE INTO e_turn_tool VALUES (?, ?)", (t["node_id"], tool))
        for e in edges:
            con.execute(
                "INSERT INTO e_relation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e["candidate_id"], e["source_node_id"], e["target_node_id"], e["relation_type"],
                    e["confidence"], e["evidence_refs_json"], e["source_session_id"], e["target_session_id"],
                ),
            )
        stats["relation_rows"] = con.execute("SELECT COUNT(*) FROM e_relation").fetchone()[0]
    finally:
        con.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Wave 9.4 build conversation graph")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥")
        return 2
    if not args.dry_run and not args.write:
        print("[error] 必须指定 --dry-run 或 --write")
        return 2
    stats = build(write=args.write)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.write:
        print(f"[write] {DUCKDB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
