# -*- coding: utf-8 -*-
# 检索查询脚本：python query.py "问题" [-k 5] [--rerank]
# 层1(用户原话)+层2(会话要点) 合并向量检索；--rerank 用本机 jina-reranker 精排
import sqlite3, json, sys, urllib.request, subprocess
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"

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
    """Jev-Style-0.8B 判定腿（Windows 原生进程内直调，去 WSL）"""
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

def main():
    args = sys.argv[1:]
    rerank = "--rerank" in args
    judge = "--judge" in args
    if "--rerank" in args: args.remove("--rerank")
    if "--judge" in args: args.remove("--judge")
    k = 5
    if "-k" in args:
        i = args.index("-k"); k = int(args[i + 1]); args = args[:i] + args[i + 2:]
    q = " ".join(args)
    if not q:
        print("用法: python query.py \"问题\" [-k 5] [--rerank]"); return
    docs = load_pool()
    qv = np.array(embed(q), dtype=np.float32)
    M = np.array([d["vec"] for d in docs], dtype=np.float32)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)
    pool = []
    for i in order[:max(k * 10, 50)]:
        docs[i]["score"] = float(scores[i])
        pool.append(docs[i])
    if judge:
        print("(Jev 裁判中，0.8B 快判 top-20...)")
        judged = judge_candidates(q, pool, topn=20)
        if judged:
            pool = judged + [d for d in pool if "jev" not in d]
            pool = pool[:k] + [d for d in pool[k:] if d.get("jev", 0) > 0][:k]
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
        print(f"   session={d['sid'][:52]}  seg={d['seg']} range={d['range']}  score={d['score']:.4f}")

if __name__ == "__main__":
    main()
