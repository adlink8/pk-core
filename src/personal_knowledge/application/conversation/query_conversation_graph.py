"""Wave 9.4 smoke queries for conversation_graph.duckdb."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DUCKDB_PATH = ROOT / "integration" / "db" / "conversation_graph.duckdb"


def run_smoke() -> int:
    import duckdb

    if not DUCKDB_PATH.exists():
        print(f"[error] 图库不存在: {DUCKDB_PATH}")
        return 1
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        queries = [
            ("relation_count_by_type", "SELECT relation_type, COUNT(*) c FROM e_relation GROUP BY 1 ORDER BY c DESC, relation_type"),
            ("cross_session_edges", "SELECT relation_type, source_session_id, target_session_id, confidence FROM e_relation WHERE source_session_id <> target_session_id ORDER BY confidence DESC LIMIT 5"),
            ("preference_signal_examples", "SELECT candidate_id, source_node_id, target_node_id, confidence FROM e_relation WHERE relation_type='preference_signal' ORDER BY confidence DESC LIMIT 5"),
            ("follow_up_examples", "SELECT candidate_id, source_node_id, target_node_id, confidence FROM e_relation WHERE relation_type='follow_up' ORDER BY confidence DESC LIMIT 5"),
            ("most_connected_turns", "SELECT source_node_id AS node_id, COUNT(*) c FROM e_relation GROUP BY 1 ORDER BY c DESC, node_id LIMIT 5"),
        ]
        for name, sql in queries:
            print("=" * 70)
            print(name)
            rows = con.execute(sql).fetchall()
            for row in rows:
                print(row)
            if not rows:
                print("(no rows)")
    finally:
        con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Query conversation graph")
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args(argv)
    if args.smoke:
        return run_smoke()
    print("[error] 当前只支持 --smoke")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
