# -*- coding: utf-8 -*-
# 检索服务核心（2026-09-29 MCP 接入）：把 query.py main() 里的编排逻辑抽成可 import 函数。
# 为何：MCP 常驻进程需要结构化结果与进程级语料缓存，不能用 CLI 的打印式 main()；
#       CLI(query.py) 与 MCP(handler) 共用本模块，检索逻辑单一来源。
# 怎么：search() 返回 {engine, notices, hits}；query.py 只剩参数解析+打印，
#       输出与重构前逐位一致（有基线回归验证）。语料缓存按向量库文件 mtime 失效。
# 隔离：本模块只依赖同目录 query.py 的原子函数与 conversation_fts，不碰 unified_search 旧检索。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

_POOL_CACHE = None
_POOL_MTIME = None


def _Q():
    # 完整包路径导入（2026-09-29）：裸名 import query/serving 会与本仓库其他
    # 同名模块遮蔽冲突（实测 retrieval/serving.py 抢占），包路径不可能撞。
    _src = r"D:/ADLINK/数据分析/src"
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from personal_knowledge.retrieval.vector import query as Q
    return Q


def load_pool_cached():
    """进程级语料缓存：向量库文件 mtime 变化即重载（手动增量压缩后自动生效）。"""
    global _POOL_CACHE, _POOL_MTIME
    Q = _Q()
    mtime = os.path.getmtime(Q.DB)
    if _POOL_CACHE is None or mtime != _POOL_MTIME:
        _POOL_CACHE = Q.load_pool()
        _POOL_MTIME = mtime
    return _POOL_CACHE


def fts_results(q, k):
    """FTS 兜底检索，返回 (rows, relaxed)。rows 供 CLI 与 MCP 共用；
    整句零命中时放宽为分词合并（FTS 是消息级 AND，长自然句常零命中）。"""
    _src = r"D:/ADLINK/数据分析/src"
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from personal_knowledge.retrieval.conversation_fts import search_sessions
    rows = search_sessions(q, limit=k)
    relaxed = False
    if not rows:
        terms = [t for t in q.split() if len(t) >= 2][:6]
        merged = {}
        for t in terms:
            try:
                for r in search_sessions(t, limit=max(k * 3, 15)):
                    m = merged.setdefault(r["session_id"], dict(r))
                    m["hits"] += r["hits"]
                    m["_terms"] = m.get("_terms", 0) + 1
                    if r["best_score"] > m["best_score"]:
                        m["best_score"], m["snippet"] = r["best_score"], r["snippet"]
            except Exception:
                continue
        rows = sorted(merged.values(),
                      key=lambda r: (-r.get("_terms", 1), -r["hits"]))[:k]
        relaxed = bool(rows)
    return rows, relaxed


def _fts_block(q, k, notices):
    """FTS 结果块；索引库也失败时返回 failed 标记（CLI 据此保留退出码 1 旧行为）。"""
    try:
        rows, relaxed = fts_results(q, k)
    except Exception as e:
        notices.append("FTS 兜底检索也失败（索引库不可用？）：%r" % e)
        return {"engine": "fts", "failed": True, "notices": notices, "hits": []}
    notices.extend(_fts_notices(rows, relaxed))
    return {"engine": "fts", "notices": notices, "hits": _fts_hits(rows, relaxed)}


def search(q, k=5, judge_mode="fallback", collapse=True, force_fts=False, rerank=False):
    """统一检索入口。返回 {"engine": "vector"|"fts", "notices": [..], "hits": [..]}。
    vector hit: {kind, label, sid, seg, range, score, twin_canonical?}
    fts hit:    {kind:"fts", session_id, hits, snippet, agent, started_at, bm25, terms?}
    notices 顺序即 CLI 原打印顺序（重构前逐位一致）。
    rerank: 仅 judge_mode=never 时生效的 jina-reranker 精排（旧 CLI 行为）。"""
    notices = []
    if force_fts:
        notices.append("(--fts：强制 FTS 全文检索)")
        return _fts_block(q, k, notices)
    docs = load_pool_cached()
    try:
        qv = np.array(_Q().embed(q), dtype=np.float32)
    except Exception as e:
        notices.append("(警告: 嵌入服务不可用[%s]，自动降级 FTS 全文检索)" % e)
        return _fts_block(q, k, notices)
    Q = _Q()
    M = np.array([d["vec"] for d in docs], dtype=np.float32)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)
    n_recall = max(k * 10, 50)
    s2c = Q.load_twin_map() if collapse else None
    pool_idx, n_dupes = [], 0
    if s2c:
        seen = set()
        for i in order:
            rep = s2c.get(docs[i]["sid"])
            if rep is not None:
                if rep in seen:
                    if len(pool_idx) < n_recall:
                        n_dupes += 1
                    continue
                seen.add(rep)
                docs[i]["twin_canonical"] = rep
            pool_idx.append(i)
            if len(pool_idx) >= n_recall:
                break
    else:
        pool_idx = list(order[:n_recall])
    pool = []
    for i in pool_idx:
        docs[i]["score"] = float(scores[i])
        pool.append(docs[i])
    if n_dupes:
        notices.append("(twin-collapse: 原始召回窗口内 %d 条同簇副本已归并)" % n_dupes)
    if judge_mode == "always":
        notices.append("(Jev 裁判中，0.8B 快判 top-20...)")
        judged = Q.judge_candidates(q, pool, topn=20)
        if judged:
            pool = judged + [d for d in pool if "jev" not in d]
            pool = pool[:k] + [d for d in pool[k:] if d.get("jev", 0) > 0][:k]
    elif judge_mode == "fallback":
        fire, why = Q.should_fallback(pool)
        if fire:
            notices.append("(纯向量不可靠[%s]，Jev 裁判重排 top-20...)" % why)
            judged = Q.judge_candidates(q, pool, topn=20)
            if judged:
                pool = judged + [d for d in pool if "jev" not in d]
                pool = pool[:k] + [d for d in pool[k:] if d.get("jev", 0) > 0][:k]
        else:
            notices.append("(纯向量置信充足，跳过裁判)")
    elif rerank:
        Q._patch_jina_reranker()
        from sentence_transformers import CrossEncoder
        ce = CrossEncoder("jinaai/jina-reranker-v2-base-multilingual", trust_remote_code=True)
        pairs = [(q, d["label"]) for d in pool]
        rs = ce.predict(pairs)
        pool = [d for _, d in sorted(zip(rs, pool), key=lambda x: -float(x[0]))][:k]
        notices.append("(jina-reranker 精排后)")
    hits = []
    for d in pool[:k]:
        h = {key: d[key] for key in ("kind", "label", "sid", "seg", "range", "score") if key in d}
        if d.get("twin_canonical"):
            h["twin_canonical"] = d["twin_canonical"]
        hits.append(h)
    return {"engine": "vector", "notices": notices, "hits": hits}


def _fts_notices(rows, relaxed):
    out = []
    if relaxed:
        out.append("(FTS 整句无命中，已放宽为分词合并——结果排序较向量路径粗)")
    if not rows:
        out.append("(FTS 无命中。注意：FTS 只覆盖权威库已入库会话)")
    return out


def _fts_hits(rows, relaxed):
    hits = []
    for r in rows:
        h = {"kind": "fts", "session_id": r["session_id"], "hits": r["hits"],
             "snippet": r["snippet"], "agent": r.get("agent") or "",
             "started_at": r.get("started_at") or "", "bm25": r["best_score"]}
        if relaxed:
            h["terms"] = r.get("_terms", 0)
        hits.append(h)
    return hits
