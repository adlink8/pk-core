"""MVP 语义层检索适配器 — 把 var/db/semantic_mvp_v3.sqlite 接入检索层。

这是 MVP 语义压缩产物（tools/semantic/mvp_semantic_compress.py 的在产库：173 张会话卡 +
1,037 条 ku_facts）"转正"的第一步：给检索层一个只读适配器，不改动在役的
unified_search / semantic_search，也不写入该库。

检索策略（向量优先，失败回退）：search_cards 优先走 Chroma 向量检索——仅当
构建登记（SEMANTIC_INDEX_REGISTRY，由 tools/semantic/build_semantic_vector_store.py
维护）存在 active build 且 chroma 可达、本机 embedding 模型可用时启用；任何一步
失败（无登记 / 服务不可达 / 集合缺失 / 模型缺失）都无声回退到纯 sqlite 关键词
检索（标准库 sqlite3，不建 FTS 索引——库以 mode=ro 打开）。两个公开接口的
签名不变，摘要行在原字段之外附 ``meta={"mode": "vector"|"keyword"}`` 标注实际
路径；调用方（MCP 工具 / REST /search/cards 路由）零改动。

注意：向量路径要求环境变量 ``PERSONAL_DATA_EMBED_MODEL_PATH`` 指向
bge-small-zh-v1.5 模型目录（本机 embedding，不联网）；未设置时模型不可用，
自动走关键词回退（待 runtime_config 默认模型路径修复后消除该要求）。

打分规则（命中数 × 字段权重）：
  ku_facts.fact            权重 4  （人工最凝练的可长期成立事实）
  session_cards.purpose    权重 3  （80 字内的会话目的）
  session_cards.summary_md 权重 2  （300 字内纪要）
  session_cards.card_json  权重 1  （原始卡全文，含 purpose/summary，仅兜底）
事实命中按 session_id 归并到所属会话卡；只统计 status='active' 的事实
（superseded 属于历史版本，不进检索面）。

分词：ASCII 标识符复用管线同款正则 [A-Za-z_][A-Za-z0-9_\\-.\\\\/]{3,}；
中文取连续 CJK 段的 2-gram（单字段落为单字），子串 LIKE 命中即可，
简单可靠，避免引入分词依赖。

CLI:
  python -m personal_knowledge.retrieval.semantic_cards "AI-Memory"
  python -m personal_knowledge.retrieval.semantic_cards "Dockerfile 代理" --limit 8
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

from personal_knowledge.core import local_embed
from personal_knowledge.core.chroma_client import ChromaClient
from personal_knowledge.core.project_paths import VAR_DB

# MVP v3 产物库（tools/semantic/mvp_semantic_compress.py V3_DB 的转正读取路径）。
# 路径约定取自 core/project_paths.py 的 VAR_DB；只读打开，绝不写入。
CARDS_DB_PATH = VAR_DB / "semantic_mvp_v3.sqlite"

# 语义向量索引构建登记（tools/semantic/build_semantic_vector_store.py 写入）。
# 向量路径仅在登记存在 active build 且 chroma 可达时启用，否则回退关键词。
SEMANTIC_INDEX_REGISTRY = VAR_DB / "semantic_index_registry.json"

# 向量检索每条查询取回的邻居数 = limit * 该倍数（同会话聚合后仍够填满 limit）。
VECTOR_TOP_MULTIPLIER = 4

# ASCII 标识符最短 3 字符（{2,}）：3 字符短 id（hy3、gpu、api）是高频查询词，
# {3,} 会把它们整体丢弃导致关键词/向量回退双双零召回。
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-.\\/]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

FIELD_WEIGHTS = {"purpose": 3.0, "summary_md": 2.0, "card_json": 1.0, "fact": 4.0}


def open_cards_db(path: Path | str | None = None) -> sqlite3.Connection:
    """只读打开 MVP 语义库（uri mode=ro）。返回 row_factory=Row 的连接。

    默认路径 CARDS_DB_PATH = var/db/semantic_mvp_v3.sqlite；调用方可用
    path 参数指向其它世代（如 v2）或测试夹具库。调用方负责 close()。
    """
    p = Path(path).resolve() if path else CARDS_DB_PATH.resolve()
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tokenize(query: str) -> list[str]:
    """查询词 -> 去重后的小写 needle 列表（ASCII 标识符 + 中文 2-gram）。"""
    needles: list[str] = []
    for tok in _ASCII_TOKEN_RE.findall(query or ""):
        t = tok.lower().strip(".-\\/\\_")
        if t and t not in needles:
            needles.append(t)
    for run in _CJK_RUN_RE.findall(query or ""):
        grams = [run[i : i + 2] for i in range(len(run) - 1)] if len(run) > 1 else [run]
        for g in grams:
            if g not in needles:
                needles.append(g)
    return needles


def _like_escape(needle: str) -> str:
    """LIKE 转义（needle 可含 % _ \\，token 正则允许下划线）。"""
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _count_hits(needle: str, text: str | None) -> int:
    if not text:
        return 0
    return text.lower().count(needle)


def _load_active_build(registry_path: Path | None = None) -> dict | None:
    """读向量索引登记，返回第一个 active build（无登记/损坏/无 active → None）。"""
    path = Path(registry_path) if registry_path else SEMANTIC_INDEX_REGISTRY
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for build in data.get("builds", []) if isinstance(data, dict) else []:
        if isinstance(build, dict) and build.get("status") == "active" and build.get("collection"):
            return build
    return None


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """'http://127.0.0.1:8001' -> ('127.0.0.1', 8001)；解析失败用默认端点。"""
    host, port = "127.0.0.1", 8001
    rest = str(endpoint or "").split("://", 1)[-1]
    h, _, p = rest.rpartition(":")
    if h:
        host = h
    if p.isdigit():
        port = int(p)
    return host, port


def _vector_search(query: str, limit: int, con: sqlite3.Connection | None = None) -> list[dict] | None:
    """向量优先检索。返回 None 表示向量路径不可用（调用方无声回退关键词）。

    打分：chroma cosine 距离 d -> 相似度 max(0, 1-d)，同会话取最大相似度聚合；
    fact 类命中计入 fact_hits、其文档文本进 matched_facts（摘要行只留前 2 条）；
    purpose 向量 metadata 不含正文，统一从 sqlite 会话卡回填。
    任何一步失败（无登记/服务不可达/集合缺失/模型缺失）都返回 None。
    """
    try:
        build = _load_active_build()
        if build is None:
            return None
        host, port = _split_endpoint(build.get("chroma_endpoint") or "")
        client = ChromaClient(host=host, port=port)
        names = {c.get("name") for c in client.list_collections()}
        if build["collection"] not in names:
            return None
        coll = client.get_or_create_collection(build["collection"])
        qvec = local_embed.embed(query)
        res = coll.query(query_embeddings=[list(qvec)],
                         n_results=max(1, int(limit) * VECTOR_TOP_MULTIPLIER))
        ids = res["ids"][0]
        distances = res["distances"][0]
        metadatas = res["metadatas"][0]
        documents = res["documents"][0]
    except Exception as exc:
        # 向量路径失效必须留痕：此处静默吞错过曾把 embedding 模型路径缺陷
        # 掩盖成"全库关键词降级"，检索质量塌方且无从排查。
        print(f"[semantic_cards] vector search unavailable, keyword fallback: {exc}", file=sys.stderr)
        return None

    entries: dict[str, dict] = {}
    for dist, meta, doc in zip(distances, metadatas, documents):
        meta = meta or {}
        sid = str(meta.get("session_id") or "")
        if not sid:
            continue
        try:
            sim = max(0.0, 1.0 - float(dist))
        except (TypeError, ValueError):
            continue
        entry = entries.setdefault(sid, {
            "session_id": sid, "purpose": None, "score": 0.0,
            "fact_hits": 0, "matched_facts": [],
        })
        entry["score"] = max(entry["score"], sim)
        if meta.get("kind") == "fact":
            entry["fact_hits"] += 1
            entry["matched_facts"].append(doc or "")

    sids = list(entries)
    if sids:
        own = con is None
        if own:
            con = open_cards_db()
        try:
            qmarks = ",".join("?" * len(sids))
            for row in con.execute(
                f"select session_id, purpose from session_cards where session_id in ({qmarks})",
                sids,
            ):
                entries[row["session_id"]]["purpose"] = row["purpose"]
        finally:
            if own:
                con.close()

    results = [e for e in entries.values() if e["score"] > 0]
    for e in results:
        e["matched_facts"] = e["matched_facts"][:2]
    results.sort(key=lambda e: (-e["score"], e["session_id"]))
    return results[: max(1, int(limit))]


def _with_mode(rows: list[dict], mode: str) -> list[dict]:
    """给摘要行附 meta={"mode": ...}，标注本次检索实际走的路径。"""
    for row in rows:
        row["meta"] = {"mode": mode}
    return rows


def search_cards(query: str, limit: int = 8, con: sqlite3.Connection | None = None) -> list[dict]:
    """检索会话卡：向量优先（mode=vector），任一环节不可用则无声回退关键词
    （mode=keyword）。返回按分排序的摘要行（最多 limit 条）。

    每行: {session_id, purpose, score, fact_hits, matched_facts, meta={"mode": ...}}。
    向量路径要求登记文件存在 active build、chroma 可达、且本机 embedding 模型
    可用（PERSONAL_DATA_EMBED_MODEL_PATH 指向 bge-small-zh-v1.5 目录）。
    con 为 None 时自行 open_cards_db()（用完即关）。
    """
    if not (query or "").strip():
        return []
    vector_rows = _vector_search(query, limit, con)
    if vector_rows is not None:
        return _with_mode(vector_rows, "vector")
    return _with_mode(_keyword_search(query, limit, con), "keyword")


def _keyword_search(query: str, limit: int = 8, con: sqlite3.Connection | None = None) -> list[dict]:
    """关键词检索会话卡，返回按分排序的摘要行（最多 limit 条）。

    SQL LIKE 做候选预筛（库只读、无 FTS），Python 精确计命中数并加权。
    每行: {session_id, purpose, score, fact_hits, matched_facts}（不带 meta）。
    con 为 None 时自行 open_cards_db()（用完即关）。
    """
    needles = _tokenize(query)
    if not needles:
        return []
    own = con is None
    if own:
        con = open_cards_db()
    try:
        escaped = [_like_escape(n) for n in needles]
        like_params = [f"%{e}%" for e in escaped]
        card_like = " OR ".join(
            "(coalesce(purpose,'') || char(10) || coalesce(summary_md,'') || char(10)"
            " || coalesce(card_json,'')) LIKE ? ESCAPE '\\'"
            for _ in escaped
        )
        cards = con.execute(
            f"select session_id, purpose, summary_md, card_json from session_cards where {card_like}",
            like_params,
        ).fetchall()
        fact_like = " OR ".join("fact LIKE ? ESCAPE '\\'" for _ in escaped)
        facts = con.execute(
            f"select session_id, fact from ku_facts where status='active' and ({fact_like})",
            like_params,
        ).fetchall()

        entries: dict[str, dict] = {}
        carded: set[str] = set()
        for row in cards:
            sid = row["session_id"]
            carded.add(sid)
            score = 0.0
            for n in needles:
                score += _count_hits(n, row["purpose"]) * FIELD_WEIGHTS["purpose"]
                score += _count_hits(n, row["summary_md"]) * FIELD_WEIGHTS["summary_md"]
                score += _count_hits(n, row["card_json"]) * FIELD_WEIGHTS["card_json"]
            entries[sid] = {
                "session_id": sid,
                "purpose": row["purpose"],
                "score": score,
                "fact_hits": 0,
                "matched_facts": [],
            }
        for row in facts:
            sid = row["session_id"]
            hits = sum(_count_hits(n, row["fact"]) for n in needles)
            if hits <= 0:
                continue
            entry = entries.get(sid)
            if entry is None:  # 卡字段无命中、仅事实命中：先占位
                entry = entries[sid] = {
                    "session_id": sid, "purpose": None, "score": 0.0,
                    "fact_hits": 0, "matched_facts": [],
                }
            entry["score"] += hits * FIELD_WEIGHTS["fact"]
            entry["fact_hits"] += 1
            entry["matched_facts"].append(row["fact"])

        # 仅事实命中的会话若其实有卡，回填 purpose 供展示（打分不变：
        # 卡字段含关键词的话早已进入 LIKE 候选集）
        orphan_sids = [sid for sid in entries if sid not in carded]
        if orphan_sids:
            qmarks = ",".join("?" * len(orphan_sids))
            for row in con.execute(
                f"select session_id, purpose from session_cards where session_id in ({qmarks})",
                orphan_sids,
            ):
                entries[row["session_id"]]["purpose"] = row["purpose"]

        results = [e for e in entries.values() if e["score"] > 0]
        for e in results:
            e["matched_facts"] = e["matched_facts"][:2]  # 摘要行只带前两条命中事实
        results.sort(key=lambda e: (-e["score"], e["session_id"]))
        return results[: max(1, int(limit))]
    finally:
        if own:
            con.close()


def get_card(session_id: str, con: sqlite3.Connection | None = None) -> dict | None:
    """完整会话卡 + 其 active facts（含 evidence_refs 列表）。无卡返回 None。"""
    own = con is None
    if own:
        con = open_cards_db()
    try:
        row = con.execute(
            "select session_id, purpose, summary_md, card_json, n_messages, truncated,"
            " model, input_tokens, output_tokens, created_at, chunk_count"
            " from session_cards where session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        card = dict(row)
        try:
            card["card"] = json.loads(card.pop("card_json") or "{}")
        except (TypeError, ValueError):
            card["card"] = {}
        facts = []
        for f in con.execute(
            "select fact_key, fact, evidence_refs, confidence, valid_from,"
            " supersedes, status from ku_facts"
            " where session_id=? and status='active' order by valid_from, fact_key",
            (session_id,),
        ):
            d = dict(f)
            try:
                d["evidence_refs"] = json.loads(d.pop("evidence_refs") or "[]")
            except (TypeError, ValueError):
                d["evidence_refs"] = []
            facts.append(d)
        card["facts"] = facts
        return card
    finally:
        if own:
            con.close()


def abbrev_sid(session_id: str, width: int = 12) -> str:
    """会话 id 缩写（展示用）：'v2|cs|034f94cecd4d…' -> 'cs:034f94cecd4d'。"""
    kind, _, tail = session_id.rpartition("|")
    prefix = (kind.rpartition("|")[2] or kind) if kind else ""
    return f"{prefix}:{tail[:width]}" if prefix else session_id[: width + 3]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="MVP 语义层关键词检索（var/db/semantic_mvp_v3.sqlite 只读）",
    )
    parser.add_argument("query", help="查询词（ASCII 标识符或中文）")
    parser.add_argument("--limit", type=int, default=8, help="返回条数（默认 8）")
    parser.add_argument("--db", default=None, help="覆盖库路径（默认 var/db/semantic_mvp_v3.sqlite）")
    parser.add_argument("--mode", choices=("auto", "keyword"), default="auto",
                        help="auto=向量优先（失败回退关键词）；keyword=强制关键词打分")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    con = open_cards_db(args.db)
    try:
        if args.mode == "keyword":
            rows = _with_mode(_keyword_search(args.query, limit=args.limit, con=con), "keyword")
        else:
            rows = search_cards(args.query, limit=args.limit, con=con)
        if rows:
            print(f"mode={rows[0]['meta']['mode']} 共 {len(rows)} 条")
        for i, r in enumerate(rows, 1):
            purpose = (r["purpose"] or "(无卡，仅事实命中)")[:60]
            score_fmt = ".3f" if r.get("meta", {}).get("mode") == "vector" else ".1f"
            print(f"{i}. score={r['score']:{score_fmt}} f={r['fact_hits']}  "
                  f"{abbrev_sid(r['session_id'])}  {purpose}")
        if not rows:
            print("(无命中)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
