# -*- coding: utf-8 -*-
# 检索查询脚本：python query.py "问题" [-k 5] [--judge|--no-judge|--judge-mode M] [--no-collapse] [--rerank] [--fts]
# 层1(用户原话)+层2(会话要点) 合并向量检索；--judge-mode always = 旧行为(全量 Jev 重排)
#
# ============================== 2026-09-28 服务侧改动 ==============================
# 改动一：twin-collapse 召回聚合去重（默认开启，--no-collapse 关闭）
#   为何：全库存在跨通道双胞胎会话（legacy 聚合通道镜像原生通道）与同家族深重合会话，
#         同一内容的多份副本挤占 top-k 名额，并把近重复簇内的混淆放大（昨晚 6 道未进
#         前三的题全部栽在簇内混淆）。映射文件 var/db/twin_map.json 由 twin_map.py 生成。
#   怎么：召回打分后按 session_to_canonical 归并——只有 twin_map 里成簇（>=2 成员）的
#         会话才参与归并，同簇文档只保留最高分一条，空出的名额按分数顺位让给不同簇；
#         不在映射里的会话一条都不动。不改索引库内容、不重建向量、不动 full_run/embed_index。
#   回退：① 传 --no-collapse 立即关闭；② 删除 var/db/twin_map.json 即自动降级为
#         不归并（fail-open，仅打警告）；③ git checkout 本文件。
#
# 改动二：裁判 fallback（默认新行为；--judge / --judge-mode always 切回旧的"永远重排"）
#   为何：昨晚全卷实测"永远重排"是净负：纯向量 Hit@1=153/173，重排后 147/173；
#         裁判帮倒忙 21 题 vs 改善 14 题。全量重排花算力还降准确率。
#   怎么：默认只在纯向量"不可靠"时才调 Jev 重排，触发条件（二选一）：
#         (a) top3 最高分 < TH_ABS=0.58：低置信保险丝。昨晚命中题 top1 分 p01=0.5833、
#             min=0.5619——此线以下几乎从无命中，纯向量在盲猜。
#         (b) 领先分差 < TH_MARGIN=0.02：collapse 后第一名簇与次名簇分差过小 = "无簇命中"
#             （top3 没有形成分离的领先簇）。依据：昨晚 8 道未进前三题分差全部 <=0.0169，
#             而命中题分差 median=0.0361、仅 32% 低于 0.02——0.02 是昨晚数据上唯一能
#             区分翻车题的信号（8/8 捕获，32% 触发率，裁判算力降约 2/3）。
#         注：任务口径里"top3 无簇命中"若解释为"top3 缺同簇互证"不可用——45% 命中题也
#         无互证（区分度为零），而翻车题里 2 道恰有互证；分差才是可测判据。
#   回退：--judge 或 --judge-mode always = 完整恢复旧"永远重排"；--judge-mode never =
#         纯向量。不改本文件中 get_judge/judge_candidates 的任何行为。
#
# 改动三：嵌入服务宕机自动降级 FTS（2026-09-28 用户拍板唯一项）
#   为何：查询必须先过 Ollama(localhost:11434) 生成向量，该服务挂了检索直接崩——
#         检索可用性存在单点。FTS 全文索引(var/db/conversation_fts.sqlite)是独立通道，
#         不依赖嵌入服务，可作为兜底。
#   怎么：embed() 抛异常（连不上/超时/服务错误）时不再崩溃，自动改走
#         conversation_fts.search_sessions() 会话级检索（bm25 排序，输出格式对齐），
#         并打印降级警告；--fts 可强制走 FTS（测试/省 GPU 用）。
#   回退：本改动只在嵌入失败分支生效，嵌入正常时行为与改动一/二完全一致；
#         git checkout 本文件即整体回退。
# ===================================================================================
import sqlite3, json, sys, urllib.request, subprocess
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
TWIN_MAP = r"D:/ADLINK/数据分析/var/db/twin_map.json"
TH_ABS = 0.58      # fallback 触发阈值 a：top3 最高分下限（依据见文件头改动二）
TH_MARGIN = 0.02   # fallback 触发阈值 b：领先簇分差下限（依据见文件头改动二）

def embed(text):
    body = json.dumps({"model": "bge-m3", "input": [text]}).encode("utf-8")
    req = urllib.request.Request("http://localhost:11434/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["embeddings"][0]

def load_pool():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    docs = []
    for sid, sj, agent, seg, r0, r1, emb in con.execute(
            "SELECT s.canonical_session_id, s.summary_json, s.agent, s.seg_no, s.range_start, "
            "s.range_end, v.embedding FROM summaries s JOIN summary_vectors v "
            "USING(summary_id) WHERE s.status='ok'"):
        s = json.loads(sj)
        def L(k):
            v = s.get(k)
            if isinstance(v, list):
                return v
            return [str(v)] if v else []
        body = " ".join([str(s.get("theme", ""))] + [str(x) for x in (L("did") + L("asks"))[:4]])
        docs.append({"id": f"{sid}|seg{seg}", "sid": sid, "agent": agent, "kind": "summary",
                     "seg": seg, "range": (r0, r1),
                     "label": "[要点] " + str(s.get("theme", "")), "text": body[:600],
                     "vec": json.loads(emb)})
    for cid, sid, o0, o1, text, emb in con.execute(
            "SELECT c.chunk_id, c.canonical_session_id, c.ord_start, c.ord_end, c.text, c.embedding "
            "FROM quote_chunks c WHERE EXISTS (SELECT 1 FROM summaries s WHERE "
            "s.canonical_session_id=c.canonical_session_id AND s.status='ok')"):
        docs.append({"id": cid, "sid": sid, "agent": "", "kind": "quote", "seg": 0,
                     "range": (o0, o1), "label": "[原话] " + text[:120].replace("\n", " "),
                     "text": text[:800], "vec": json.loads(emb)})
    return docs

# ---------------- twin-collapse（改动一实现，详见文件头） ----------------
_TWIN_S2C = None
_TWIN_LOADED = False

def load_twin_map():
    """读 twin_map.json 的 session_to_canonical；缺失/损坏时 fail-open 返回 None。"""
    global _TWIN_S2C, _TWIN_LOADED
    if _TWIN_LOADED:
        return _TWIN_S2C
    _TWIN_LOADED = True
    try:
        with open(TWIN_MAP, encoding="utf-8") as f:
            m = json.load(f)
        s2c = m["session_to_canonical"]
        if not isinstance(s2c, dict):
            raise ValueError("session_to_canonical 不是 dict")
        _TWIN_S2C = {str(a): str(b) for a, b in s2c.items()}
    except Exception as e:
        print("(警告: %s 缺失/损坏(%s)，twin-collapse 降级为不归并)" % (TWIN_MAP, e))
        _TWIN_S2C = None
    return _TWIN_S2C

def twin_collapse(pool, s2c):
    """按 twin 簇贪心去重：只有 s2c 里映射的会话才参与（同簇只留最高分一条）；
    未映射会话的文档一律原样保留。pool 须已按分数降序。"""
    seen, out = set(), []
    for d in pool:
        rep = s2c.get(d["sid"])
        if rep is not None:
            if rep in seen:
                continue
            seen.add(rep)
            d["twin_canonical"] = rep
        out.append(d)
    return out

def _patch_jina_reranker():
    import torch
    import transformers.models.xlm_roberta.modeling_xlm_roberta as _m
    if not hasattr(_m, "create_position_ids_from_input_ids"):
        def create_position_ids_from_input_ids(input_ids, padding_idx, past_key_values_length=0):
            mask = input_ids.ne(padding_idx).int()
            incremental_indices = (torch.cumsum(mask, dim=1).type_as(mask) + past_key_values_length) * mask
            return incremental_indices.long() + padding_idx
        _m.create_position_ids_from_input_ids = create_position_ids_from_input_ids

_JUDGE_ENGINE = None

def get_judge():
    global _JUDGE_ENGINE
    if _JUDGE_ENGINE is None:
        sys.path.insert(0, r"D:/Ollama/dl/jev-style-v3")
        from jev_style_decision_gguf import JevStyleDecisionGGUF
        _JUDGE_ENGINE = JevStyleDecisionGGUF(r"D:/Ollama/dl/jev-style-v3", quant="Q4_K_M",
                                             binary=r"D:/Ollama/dl/jev-style-v3/build/jev-score.exe")
    return _JUDGE_ENGINE

def judge_candidates(query, pool, topn=20):
    """Jev-Style-0.8B 判定腿（Windows 原生进程内直调，去 WSL）——原逻辑零改动"""
    engine = get_judge()
    judged = []
    for i, d in enumerate(pool[:topn]):
        try:
            state = "用户查询：" + query + "\n\n候选内容：\n" + d.get("text", d["label"])
            r = engine.decide(state, "这段候选内容与用户查询相关吗？",
                              options={"relevant": "候选直接讨论或回答了查询所问的内容",
                                       "irrelevant": "候选与查询所问无关"},
                              category="general_relevance")
            d["jev"] = r["probabilities"]["relevant"] if r["answer"] == "relevant" else 0.0
        except Exception as e:
            d["jev"] = 0.0
        judged.append(d)
    return sorted(judged, key=lambda d: -d.get("jev", 0))

def should_fallback(pool):
    """fallback 触发判定（改动二实现，阈值依据见文件头）。
    pool: collapse 后按分数降序的候选。返回 (bool, 原因字符串)。"""
    top3 = pool[:3]
    if not top3:
        return True, "空候选"
    s_max = max(d["score"] for d in top3)
    if s_max < TH_ABS:
        return True, "top3最高分%.4f<%.2f(低置信)" % (s_max, TH_ABS)
    # 领先簇分差：第一名与第一个不同簇候选的分差
    rep0 = top3[0].get("twin_canonical", top3[0]["sid"])
    for d in pool[1:]:
        if d.get("twin_canonical", d["sid"]) != rep0:
            margin = top3[0]["score"] - d["score"]
            if margin < TH_MARGIN:
                return True, "领先分差%.4f<%.2f(无领先簇)" % (margin, TH_MARGIN)
            break
    return False, ""

def fts_fallback(q, k):
    """嵌入服务不可用时的 FTS 会话级兜底检索（改动三实现，详见文件头）。
    复用 conversation_fts.search_sessions（独立索引库，权威库只读）。
    FTS 是消息级 AND：整句多短语必须同现于一条消息，长自然句常零命中；
    故整句无命中时放宽为分词检索、按会话合并（先命中词数、再总命中数排序），
    与向量语义的差距在输出里明示。"""
    sys.path.insert(0, r"D:/ADLINK/数据分析/src/personal_knowledge")
    from retrieval.conversation_fts import search_sessions
    try:
        results = search_sessions(q, limit=k)
    except Exception as e:
        print("FTS 兜底检索也失败（索引库不可用？）：%r" % e)
        return 1
    relaxed = False
    if not results:
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
        results = sorted(merged.values(),
                         key=lambda r: (-r.get("_terms", 1), -r["hits"]))[:k]
        relaxed = bool(results)
        if relaxed:
            print("(FTS 整句无命中，已放宽为分词合并——结果排序较向量路径粗)")
    if not results:
        print("(FTS 无命中。注意：FTS 只覆盖权威库已入库会话)")
        return 0
    for rank, r in enumerate(results, 1):
        extra = f"  命中词{r.get('_terms', '?')}个" if relaxed else ""
        print(f"{rank}. [FTS] hits={r['hits']}{extra}  {r['snippet'][:90].replace(chr(10), ' ')}")
        print(f"   session={r['session_id'][:52]}  agent={r['agent'] or '?'}  "
              f"started={(r['started_at'] or '?')[:16]}  bm25={r['best_score']:.3f}")
    return 0

def main():
    args = sys.argv[1:]
    rerank = "--rerank" in args
    force_fts = "--fts" in args
    if "--fts" in args: args.remove("--fts")
    judge_mode = None
    # 兼容旧开关：--judge=always，--no-judge=never；--judge-mode 显式优先
    if "--judge" in args:
        judge_mode = "always"; args.remove("--judge")
    if "--no-judge" in args:
        judge_mode = "never"; args.remove("--no-judge")
    if "--judge-mode" in args:
        i = args.index("--judge-mode"); judge_mode = args[i + 1]
        assert judge_mode in ("always", "fallback", "never"), "judge-mode 取值错误"
        args = args[:i] + args[i + 2:]
    collapse = "--no-collapse" not in args
    if "--no-collapse" in args: args.remove("--no-collapse")
    if "--rerank" in args: args.remove("--rerank")
    k = 5
    if "-k" in args:
        i = args.index("-k"); k = int(args[i + 1]); args = args[:i] + args[i + 2:]
    q = " ".join(args)
    if not q:
        print('用法: python query.py "问题" [-k 5] [--judge|--no-judge|--judge-mode always|fallback|never] [--no-collapse] [--rerank] [--fts]')
        return
    if judge_mode is None:
        judge_mode = "fallback"  # 2026-09-28 起新默认，旧"永远重排"用 --judge 切回
    if force_fts:
        print("(--fts：强制 FTS 全文检索)")
        return fts_fallback(q, k)
    docs = load_pool()
    try:
        qv = np.array(embed(q), dtype=np.float32)
    except Exception as e:
        print("(警告: 嵌入服务不可用[%s]，自动降级 FTS 全文检索)" % e)
        return fts_fallback(q, k)
    M = np.array([d["vec"] for d in docs], dtype=np.float32)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)
    n_recall = max(k * 10, 50)
    s2c = load_twin_map() if collapse else None
    pool_idx, n_dupes = [], 0
    if s2c:
        # 只有映射簇内的文档参与归并（同簇只留最高分），名额让给不同簇；未映射会话不动
        seen = set()
        for i in order:
            rep = s2c.get(docs[i]["sid"])
            if rep is not None:
                if rep in seen:
                    if len(pool_idx) < n_recall:
                        n_dupes += 1  # 只统计本应进召回池却被归并的同簇副本
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
        print("(twin-collapse: 原始召回窗口内 %d 条同簇副本已归并)" % n_dupes)
    if judge_mode == "always":
        print("(Jev 裁判中，0.8B 快判 top-20...)")
        judged = judge_candidates(q, pool, topn=20)
        if judged:
            pool = judged + [d for d in pool if "jev" not in d]
            pool = pool[:k] + [d for d in pool[k:] if d.get("jev", 0) > 0][:k]
    elif judge_mode == "fallback":
        fire, why = should_fallback(pool)
        if fire:
            print("(纯向量不可靠[%s]，Jev 裁判重排 top-20...)" % why)
            judged = judge_candidates(q, pool, topn=20)
            if judged:
                pool = judged + [d for d in pool if "jev" not in d]
                pool = pool[:k] + [d for d in pool[k:] if d.get("jev", 0) > 0][:k]
        else:
            print("(纯向量置信充足，跳过裁判)")
    elif rerank:
        _patch_jina_reranker()
        from sentence_transformers import CrossEncoder
        ce = CrossEncoder("jinaai/jina-reranker-v2-base-multilingual", trust_remote_code=True)
        pairs = [(q, d["label"]) for d in pool]
        rs = ce.predict(pairs)
        pool = [d for _, d in sorted(zip(rs, pool), key=lambda x: -float(x[0]))][:k]
        print("(jina-reranker 精排后)")
    for rank, d in enumerate(pool[:k], 1):
        print(f"{rank}. {d['kind']}  {d['label'][:90]}")
        print(f"   session={d['sid'][:52]}  seg={d['seg']} range={d['range']}  score={d['score']:.4f}"
              + (f"  [twin簇代表 {d['twin_canonical'][:40]}]" if d.get("twin_canonical") else ""))

if __name__ == "__main__":
    main()
