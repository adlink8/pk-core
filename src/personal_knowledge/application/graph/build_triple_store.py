"""Wave 8: 三库统一灌库(SQLite + Chroma 向量库 + DuckDB 图库)。

把重构后的 conversation_summaries.json(逐 turn 叙述摘要)一次性写入三库,
各库职责分离、可独立查询,但数据来源单一、保证一致性:

  SQLite  (personal_system.sqlite)
    表 conversation_sessions / conversation_turns_summary
    用途: 结构化查询、统计、源数据管理。与现有 unified_events 等表隔离。

  Chroma  (conversation_turns collection, 复用现有)
    用途: 语义检索。每个 turn 叙述向量化,支持"按语义找做过什么"。
    复用 build_conversation_vector_store 的 load/embed 逻辑。

  DuckDB  (conversation_graph.duckdb, 新建)
    节点: Session / Turn / Tool / Topic
    边:   e_next_turn(时序) / e_used_tool / e_session_topic
    用途: 关系/因果查询、跨 session 关联发现。用关系表 + 递归 CTE 表达图。

幂等: 三库都用"先删后建"或 upsert,可重复运行。
      SQLite 用 INSERT OR REPLACE,DuckDB 全量重建,Chroma 删 collection 重建。

用法:
  python build_triple_store.py --dry-run              # 看三库各会写多少,不实际写
  python build_triple_store.py --write                # 三库全灌
  python build_triple_store.py --write --only sqlite  # 只灌 SQLite
  python build_triple_store.py --write --only chroma  # 只灌向量库
  python build_triple_store.py --write --only duckdb  # 已禁用，改用 build_conversation_graph.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from personal_knowledge.core import local_embed as embed_mod
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError

ROOT = Path(__file__).resolve().parents[4]
SUMMARIES_JSON = ROOT / "integration" / "analysis" / "ai_context" / "conversation_summaries.json"
SQLITE_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
DUCKDB_PATH = ROOT / "integration" / "db" / "conversation_graph.duckdb"
CHROMA_COLLECTION = "conversation_turns"
MIN_NARRATIVE_LEN = 20   # turn 叙述最短长度,短于此跳过(无语义价值)


@dataclass
class TurnUnit:
    """统一的 turn 数据单元,三库共用。"""
    session_id: str
    turn_no: int
    turn_id: str | None
    narrative: str
    main_topic: str
    source: str
    tools_used: list[str]
    source_refs: list[str]
    raw_messages: int
    deduped_messages: int


def load_units() -> list[TurnUnit]:
    """从 conversation_summaries.json 加载所有 turn 为统一单元。

    过滤掉 narrative 异常的 turn(** 残留、过短),这些是 LLM 输出层瑕疵,
    入库前剔除避免污染三库。
    """
    if not SUMMARIES_JSON.exists():
        print(f"[error] 缺少 summary 产物: {SUMMARIES_JSON.relative_to(ROOT)}")
        print("        先运行: python -m personal_knowledge.application.conversation.summary --write")
        return []
    data = json.loads(SUMMARIES_JSON.read_text(encoding="utf-8"))
    units: list[TurnUnit] = []
    skipped = 0
    for session in data:
        sid = session["session_id"]
        topic = session.get("main_topic", "") or ""
        meta = session.get("meta", {})
        source = meta.get("source", "Agent")
        for turn_no, turn in enumerate(session.get("turn_summaries", []), 1):
            narrative = (turn.get("narrative") or "").strip()
            # 剔除 ** 残留和过短叙述(LLM 输出层瑕疵)
            if narrative.strip().strip("*") == "" or len(narrative) < MIN_NARRATIVE_LEN:
                skipped += 1
                continue
            units.append(TurnUnit(
                session_id=sid,
                turn_no=turn_no,
                turn_id=turn.get("turn_id"),
                narrative=narrative,
                main_topic=topic,
                source=source,
                tools_used=turn.get("tools_used", []),
                source_refs=turn.get("source_refs", []),
                raw_messages=meta.get("raw_messages", 0),
                deduped_messages=meta.get("deduped_messages", 0),
            ))
    print(f"加载 turn 单元: {len(units)} 个可入库(跳过 {skipped} 个异常/过短)")
    return units


# ============================ SQLite 层 ============================

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id      TEXT PRIMARY KEY,
    main_topic      TEXT,
    source          TEXT,
    turn_count      INTEGER,
    raw_messages    INTEGER,
    deduped_messages INTEGER,
    tool_call_count INTEGER,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversation_turns_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    turn_no         INTEGER NOT NULL,
    turn_id         TEXT,
    narrative       TEXT NOT NULL,
    tools_used      TEXT,          -- 逗号分隔
    source_ref      TEXT,          -- 首个证据引用(回原文)
    main_topic      TEXT,
    UNIQUE(session_id, turn_no),
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_cts_session ON conversation_turns_summary(session_id);
CREATE INDEX IF NOT EXISTS idx_cts_turn ON conversation_turns_summary(session_id, turn_no);
"""


def write_sqlite(units: list[TurnUnit], dry: bool) -> dict:
    """写入 SQLite: conversation_sessions + conversation_turns_summary。"""
    stats = {"target": "SQLite", "dry": dry}
    if dry:
        sessions = {u.session_id for u in units}
        stats["sessions"] = len(sessions)
        stats["turns"] = len(units)
        return stats
    import sqlite3
    SQLITE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SQLITE_DB)
    con.executescript(SQLITE_SCHEMA)
    # 先清旧数据(幂等:可重复运行)
    con.execute("DELETE FROM conversation_turns_summary")
    con.execute("DELETE FROM conversation_sessions")

    # 聚合 session 级数据
    sess_map: dict[str, dict] = {}
    for u in units:
        s = sess_map.setdefault(u.session_id, {
            "main_topic": u.main_topic, "source": u.source,
            "turn_count": 0, "raw_messages": 0, "deduped_messages": 0,
            "tools": set(),
        })
        s["turn_count"] += 1
        s["tools"].update(u.tools_used)

    for sid, s in sess_map.items():
        con.execute(
            "INSERT OR REPLACE INTO conversation_sessions "
            "(session_id, main_topic, source, turn_count, raw_messages, deduped_messages, tool_call_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, s["main_topic"], s["source"], s["turn_count"],
             s["raw_messages"], s["deduped_messages"], len(s["tools"])),
        )
    for u in units:
        con.execute(
            "INSERT OR REPLACE INTO conversation_turns_summary "
            "(session_id, turn_no, turn_id, narrative, tools_used, source_ref, main_topic) "
            "VALUES (?,?,?,?,?,?,?)",
            (u.session_id, u.turn_no, u.turn_id, u.narrative,
             ",".join(u.tools_used), u.source_refs[0] if u.source_refs else "",
             u.main_topic),
        )
    con.commit()
    stats["sessions"] = con.execute("SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]
    stats["turns"] = con.execute("SELECT COUNT(*) FROM conversation_turns_summary").fetchone()[0]
    con.close()
    return stats


# ============================ Chroma 向量库层 ============================

def write_chroma(units: list[TurnUnit], dry: bool) -> dict:
    """写入 Chroma conversation_turns collection(复用现有 collection)。

    每个 turn 向量化,id = {session_id}#{turn_id or turn_no} 做幂等键。
    全量重建:先删 collection 再建(与 build_conversation_vector_store 一致)。
    """
    stats = {"target": "Chroma", "dry": dry, "vectorizable": len(units)}
    if dry:
        return stats
    client = ChromaClient(host="localhost", port=8001)
    # 全量重建:删旧 collection(chroma_client 的删除方法是 delete_collection_by_name)
    try:
        client.delete_collection_by_name(name=CHROMA_COLLECTION)
    except (ChromaError, Exception):
        pass  # 不存在则忽略
    coll = client.get_or_create_collection(name=CHROMA_COLLECTION)

    ok, msg, dim = embed_mod.verify_model()
    if not ok:
        stats["error"] = f"embed 模型不可用: {msg}"
        return stats
    stats["embed_dim"] = dim

    BATCH = 32
    written = 0
    for i in range(0, len(units), BATCH):
        batch = units[i:i + BATCH]
        texts = [f"{u.main_topic}。{u.narrative}" if u.main_topic else u.narrative
                 for u in batch]
        embs = embed_mod.embed_batch(texts)
        ids, embeddings, documents, metadatas = [], [], [], []
        for u, emb, text in zip(batch, embs, texts):
            if emb is None:
                continue
            unit_id = f"{u.session_id}#{u.turn_id or f't{u.turn_no}'}"
            ids.append(unit_id)
            embeddings.append(emb)
            documents.append(text)
            metadatas.append({
                "session_id": u.session_id,
                "turn_id": u.turn_id or "",
                "turn_no": u.turn_no,
                "main_topic": u.main_topic[:100],
                "source": u.source,
                "event_type": "conversation_turn",
                "tools_used": ",".join(u.tools_used)[:200],
            })
        if ids:
            coll.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
            written += len(ids)
    stats["written"] = written
    stats["collection_count"] = coll.count()
    return stats


# ============================ DuckDB 图库层 ============================
#
# ⚠️ DEPRECATED (2026-06-28, Phase 07 Wave 8.3.3)
# 图库部分**暂停使用**,原因:
#   1. 当前"关系"是从 LLM 压缩叙述里启发式抽取的,非真实因果/语义关系。
#   2. Wave 8 压缩质量收口前生成,产物含 ** 瑕疵 turn,关系边含噪声。
#   3. 关系抽取方案未经验证,不能作为可信关系源。
# 处置:Wave 9(图库真关系重做)会用新方案重新生成。在此之前不要灌新数据。
# 详见: integration/db/DEPRECATED.md
# SQLite 层和 Chroma 向量库层不受本废弃影响,正常使用。

DUCKDB_SCHEMA = """
-- 节点表
CREATE TABLE IF NOT EXISTS g_session (
    session_id   TEXT PRIMARY KEY,
    main_topic   TEXT,
    source       TEXT,
    turn_count   INTEGER
);

CREATE TABLE IF NOT EXISTS g_turn (
    turn_pk      INTEGER PRIMARY KEY,   -- 自增主键(图节点 id)
    session_id   TEXT NOT NULL,
    turn_no      INTEGER NOT NULL,
    narrative    TEXT NOT NULL,
    source_ref   TEXT,
    main_topic   TEXT
);

CREATE TABLE IF NOT EXISTS g_tool (
    tool_name    TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS g_topic (
    topic        TEXT PRIMARY KEY
);

-- 边表(关系表表达图)
CREATE TABLE IF NOT EXISTS e_next_turn (
    turn_pk      INTEGER NOT NULL,      -- 当前 turn
    next_turn_pk INTEGER NOT NULL,      -- 下一个 turn(同 session 时序)
    PRIMARY KEY (turn_pk, next_turn_pk)
);

CREATE TABLE IF NOT EXISTS e_used_tool (
    turn_pk      INTEGER NOT NULL,
    tool_name    TEXT NOT NULL,
    PRIMARY KEY (turn_pk, tool_name)
);

CREATE TABLE IF NOT EXISTS e_session_topic (
    session_id   TEXT NOT NULL,
    topic        TEXT NOT NULL,
    PRIMARY KEY (session_id, topic)
);
"""


def write_duckdb(units: list[TurnUnit], dry: bool) -> dict:
    """Deprecated: 禁用旧伪关系 DuckDB 写入入口。"""
    _ = units, dry
    raise RuntimeError(
        "DuckDB pseudo-graph path is deprecated; use \"python -m personal_knowledge.application.conversation.build_conversation_graph --write\" instead."
    )


# ============================ 主流程 ============================

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="三库统一灌库 (SQLite + Chroma + DuckDB)")
    p.add_argument("--dry-run", action="store_true", help="只看各库会写多少,不实际写")
    p.add_argument("--write", action="store_true", help="实际写入三库")
    p.add_argument("--only", choices=["sqlite", "chroma", "duckdb"],
                   help="只灌指定库(默认三库全灌)")
    args = p.parse_args(argv)
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2
    if not args.dry_run and not args.write:
        print("加 --dry-run 预览或 --write 实际写入。")
        return 0

    units = load_units()
    if not units:
        return 1

    targets = [args.only] if args.only else ["sqlite", "chroma", "duckdb"]
    dry = args.dry_run
    print(f"\n{'[DRY-RUN]' if dry else '[WRITE]'} 灌库目标: {', '.join(targets)}")
    print("=" * 60)

    t0 = time.time()
    results = []
    for tgt in targets:
        t_start = time.time()
        try:
            if tgt == "sqlite":
                r = write_sqlite(units, dry)
            elif tgt == "chroma":
                r = write_chroma(units, dry)
            else:
                r = write_duckdb(units, dry)
            r["elapsed"] = round(time.time() - t_start, 1)
            r["status"] = "ok"
        except Exception as exc:
            r = {"target": tgt, "status": "error",
                 "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                 "elapsed": round(time.time() - t_start, 1)}
        results.append(r)
        # 实时打印每个库结果
        label = r["target"]
        if r["status"] == "ok":
            kv = " ".join(f"{k}={v}" for k, v in r.items()
                          if k not in ("target", "dry", "status", "elapsed"))
            print(f"  [{label}] OK {r['elapsed']}s | {kv}")
        else:
            print(f"  [{label}] ERR {r['elapsed']}s | {r['error']}")

    print("=" * 60)
    total = round(time.time() - t0, 1)
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"完成: {ok}/{len(results)} 库成功,总耗时 {total}s")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
