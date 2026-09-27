# -*- coding: utf-8 -*-
# 全规模验收集复测：10 道原题 × 全量语料（层1+层2 合并），测 Hit@1/Hit@3/MRR，纯向量 vs 加 rerank
import sqlite3, json, urllib.request
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"

def _patch_jina_reranker():
    import torch
    import transformers.models.xlm_roberta.modeling_xlm_roberta as _m
    if not hasattr(_m, "create_position_ids_from_input_ids"):
        def create_position_ids_from_input_ids(input_ids, padding_idx, past_key_values_length=0):
            mask = input_ids.ne(padding_idx).int()
            incremental_indices = (torch.cumsum(mask, dim=1).type_as(mask) + past_key_values_length) * mask
            return incremental_indices.long() + padding_idx
        _m.create_position_ids_from_input_ids = create_position_ids_from_input_ids

def embed(texts):
    body = json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8")
    req = urllib.request.Request("http://localhost:11434/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["embeddings"]

def doc_text(sj):
    s = json.loads(sj)
    L = lambda k: s.get(k) or []
    parts = [s.get("theme", "")]
    for k in ("asks", "did", "decisions", "leftovers", "quotes", "keywords", "artifacts"):
        parts += [str(x) for x in L(k)]
    return " ".join(p for p in parts if p)

con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
docs = []
for sid, sj, seg, r0, r1, emb in con.execute(
        "SELECT s.canonical_session_id, s.summary_json, s.seg_no, s.range_start, s.range_end, v.embedding "
        "FROM summaries s JOIN summary_vectors v USING(summary_id) WHERE s.status='ok'"):
    docs.append({"sid": sid, "seg": seg, "kind": "summary", "label": json.loads(sj).get("theme", ""),
                 "range": (r0, r1), "vec": json.loads(emb)})
for cid, sid, o0, o1, text, emb in con.execute(
        "SELECT c.chunk_id, c.canonical_session_id, c.ord_start, c.ord_end, c.text, c.embedding "
        "FROM quote_chunks c WHERE EXISTS (SELECT 1 FROM summaries s WHERE "
        "s.canonical_session_id=c.canonical_session_id AND s.status='ok')"):
    docs.append({"sid": sid, "seg": 0, "kind": "quote", "label": text[:80].replace("\n", " "),
                 "range": (o0, o1), "vec": json.loads(emb)})
print(f"语料: 层2 {sum(1 for d in docs if d['kind']=='summary')} + 层1 {sum(1 for d in docs if d['kind']=='quote')}")

# 健全性检查：巨型会话分段是否在库且编号一致
giant_segs = con.execute(
    "SELECT seg_no, json_extract(summary_json,'$.theme') FROM summaries "
    "WHERE status='ok' AND canonical_session_id LIKE 'cs|legacy|cs/codex/019fae6a%' ORDER BY seg_no").fetchall()
print(f"巨型会话分段 {len(giant_segs)} 个: {[g[0] for g in giant_segs]}")

def sid_match(doc_sid, prefix):
    return doc_sid.startswith(prefix)

# 目标会话 id 前缀匹配
T = {}
for sid, seg in con.execute("SELECT canonical_session_id, seg_no FROM summaries WHERE status='ok'"):
    T.setdefault(sid, set()).add(seg)

QUERIES = [
    ("jev模型到底有什么用", "cs|zcode|sess_29d369d2", None),
    ("换章不会自动到开头 导致一直跳章", "cs|legacy|cs/codex/019fae6a", [4, 6, 7]),
    ("全部分析能后台进行吗 一边看小说一边进行", "cs|legacy|cs/codex/019fae6a", [7, 8]),
    ("Reader Chat 从喂什么答什么改成给一把查原文的钥匙", "cs|legacy|cs/codex/019fae6a", [2]),
    ("翻译调参方向废止 实时离线相似度98.54", "cs|legacy|cs/zcode/sess_228b941e", None),
    ("MT 100%恒定式废除 改独立mt-ref", "cs|legacy|cs/zcode/sess_228b941e", None),
    ("全库保持一致 大不了重新导入 反正会话都在磁盘里", "cs|zcode|sess_6014f616", None),
    ("启动时检查 embedding pending 的块 自动触发续跑", "cs|legacy|cs/codex/019fae6a", [3]),
    ("图片生成失败 请确认生图服务已启动", "cs|legacy|cs/codex/019fae6a", [6]),
    ("本机一共部署多少模型了 每个模型的参数和应用范围", "cs|zcode|sess_29d369d2", None),
]

M = np.array([d["vec"] for d in docs], dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
su_idx = [i for i, d in enumerate(docs) if d["kind"] == "summary"]
Ms = M[su_idx]
qs = embed([q for q, _, _ in QUERIES])

def _patch_jina_reranker():
    import torch
    import transformers.models.xlm_roberta.modeling_xlm_roberta as _m
    if not hasattr(_m, "create_position_ids_from_input_ids"):
        def create_position_ids_from_input_ids(input_ids, padding_idx, past_key_values_length=0):
            mask = input_ids.ne(padding_idx).int()
            incremental_indices = (torch.cumsum(mask, dim=1).type_as(mask) + past_key_values_length) * mask
            return incremental_indices.long() + padding_idx
        _m.create_position_ids_from_input_ids = create_position_ids_from_input_ids

res_sum_vec = {"h1": 0, "h3": 0, "rr": 0.0}
res_sum_rrk = {"h1": 0, "h3": 0, "rr": 0.0}
reranker = None
print()
for (q, prefix, segs), qe in zip(QUERIES, qs):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    # 主航道：摘要层单独排序
    sscores = Ms @ qv
    sorder = np.argsort(-sscores)
    scands = [docs[su_idx[i]] for i in sorder[:50]]
    rank = next((i + 1 for i in sorder if sid_match(docs[su_idx[i]]["sid"], prefix)
                 and (segs is None or docs[su_idx[i]]["seg"] in segs)), None)
    if rank == 1: res_sum_vec["h1"] += 1
    if rank and rank <= 3: res_sum_vec["h3"] += 1
    res_sum_vec["rr"] += (1.0 / rank) if rank else 0.0
    if reranker is None:
        _patch_jina_reranker()
        from sentence_transformers import CrossEncoder
        reranker = CrossEncoder("jinaai/jina-reranker-v2-base-multilingual", trust_remote_code=True)
    rs = reranker.predict([(q, d["label"]) for d in scands])
    r_order = np.argsort(-np.array(rs))
    rank2 = next((i + 1 for i in r_order if sid_match(scands[i]["sid"], prefix)
                  and (segs is None or scands[i]["seg"] in segs)), None)
    if rank2 == 1: res_sum_rrk["h1"] += 1
    if rank2 and rank2 <= 3: res_sum_rrk["h3"] += 1
    res_sum_rrk["rr"] += (1.0 / rank2) if rank2 else 0.0
    top3 = ["seg%s %s" % (scands2["seg"], scands2["label"][:30])
            for _, scands2 in sorted(zip(rs, scands), key=lambda x: -float(x[0]))[:3]]
    mark = "H1" if rank == 1 else ("H3" if rank and rank <= 3 else f"M{rank}")
    print(f"[{mark}/rerank#{rank2}] {q[:26]}")
    for t in top3: print(f"      {t}")

n = len(QUERIES)
print(f"\n=== 主航道(仅摘要层) 纯向量  Hit@1={res_sum_vec['h1']}/{n}  Hit@3={res_sum_vec['h3']}/{n}  MRR={res_sum_vec['rr']/n:.3f}")
print(f"=== 主航道(仅摘要层) +rerank Hit@1={res_sum_rrk['h1']}/{n}  Hit@3={res_sum_rrk['h3']}/{n}  MRR={res_sum_rrk['rr']/n:.3f}")
print("=== 对照（合并层1+层2 的严格单靶成绩见上一轮：纯向量 0/10，+rerank 4/10）===")

def sid_match(doc_sid, prefix):
    return doc_sid.startswith(prefix)

res_vec = {"h1": 0, "h3": 0, "rr": 0.0}
res_rrk = {"h1": 0, "h3": 0, "rr": 0.0}
reranker = None
print()
for (q, prefix, segs), qe in zip(QUERIES, qs):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)
    # 纯向量：session 级命中
    rank = next((i + 1 for i in order if sid_match(docs[i]["sid"], prefix)
                 and (segs is None or docs[i]["seg"] in segs)), None)
    if rank == 1: res_vec["h1"] += 1
    if rank and rank <= 3: res_vec["h3"] += 1
    res_vec["rr"] += (1.0 / rank) if rank else 0.0
    # rerank: 取向量 top50 重排
    cand = [docs[i] for i in order[:50]]
    if reranker is None:
        _patch_jina_reranker()
        from sentence_transformers import CrossEncoder
        reranker = CrossEncoder("jinaai/jina-reranker-v2-base-multilingual", trust_remote_code=True)
    rs = reranker.predict([(q, d["label"]) for d in cand])
    r_order = np.argsort(-np.array(rs))
    rank2 = next((i + 1 for i in r_order if sid_match(cand[i]["sid"], prefix)
                  and (segs is None or cand[i]["seg"] in segs)), None)
    if rank2 == 1: res_rrk["h1"] += 1
    if rank2 and rank2 <= 3: res_rrk["h3"] += 1
    res_rrk["rr"] += (1.0 / rank2) if rank2 else 0.0
    top3 = ["%s seg%s %s" % (cand2["kind"][:2], cand2["seg"], cand2["label"][:26])
            for _, cand2 in sorted(zip(rs, cand), key=lambda x: -float(x[0]))[:3]]
    print(f"[{'H1' if rank==1 else ('H3' if rank and rank<=3 else 'MISS')}/{rank}] rerank#{rank2}  {q[:26]}")
    for t in top3: print(f"      {t}")

n = len(QUERIES)
print(f"\n=== 纯向量   Hit@1={res_vec['h1']}/{n}  Hit@3={res_vec['h3']}/{n}  MRR={res_vec['rr']/n:.3f}")
print(f"=== +rerank Hit@1={res_rrk['h1']}/{n}  Hit@3={res_rrk['h3']}/{n}  MRR={res_rrk['rr']/n:.3f}")
print(f"=== 门槛参考（试点）: Hit@3>=8 且 Hit@1>=5；rerank 应显著优于纯向量 ===")
