# -*- coding: utf-8 -*-
# 终极评测：87 题答案集考卷 × 两档（纯向量 / 向量+Jev裁判）
# 记分：命中 = top-k 内任一文档的会话匹配 answer_set 任一前缀
import sqlite3, json, time, urllib.request, sys
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
EXAM = r"D:/ADLINK/数据分析/docs/retrieval/exam_v1.json"

def embed(texts):
    body = json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8")
    req = urllib.request.Request("http://localhost:11434/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["embeddings"]

def doc_text(sj):
    s = json.loads(sj)
    def L(k):
        v = s.get(k)
        if isinstance(v, list):
            return v
        return [str(v)] if v else []
    parts = [str(s.get("theme", ""))]
    for k in ("asks", "quotes"):
        parts += [str(x) for x in L(k)[:3]]
    return " ".join(p for p in parts if p)[:600]

con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
docs = []
for sid, sj, seg, emb in con.execute(
        "SELECT s.canonical_session_id, s.summary_json, s.seg_no, v.embedding "
        "FROM summaries s JOIN summary_vectors v USING(summary_id) WHERE s.status='ok'"):
    docs.append({"sid": sid, "seg": seg, "kind": "要点", "text": doc_text(sj),
                 "label": json.loads(sj).get("theme", ""), "vec": json.loads(emb)})
for cid, sid, text, emb in con.execute(
        "SELECT c.chunk_id, c.canonical_session_id, c.text, c.embedding "
        "FROM quote_chunks c WHERE EXISTS (SELECT 1 FROM summaries s WHERE "
        "s.canonical_session_id=c.canonical_session_id AND s.status='ok')"):
    docs.append({"sid": sid, "seg": 0, "kind": "原话", "text": text[:800],
                 "label": text[:70].replace("\n", " "), "vec": json.loads(emb)})
M = np.array([d["vec"] for d in docs], dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
print(f"语料 {len(docs)}（层2 {sum(1 for d in docs if d['kind']=='要点')} + 层1 {sum(1 for d in docs if d['kind']=='原话')}）", flush=True)

exam = json.load(open(EXAM, encoding="utf-8"))
exam = [e for e in exam if isinstance(e, dict) and e.get("q")]
print(f"考卷 {len(exam)} 题", flush=True)
qs = embed([e["q"] for e in exam])

def hit_rank(cand, answer_set, k_limit=None):
    seq = cand if k_limit is None else cand[:k_limit]
    return next((i + 1 for i, d in enumerate(seq)
                 if any(d["sid"].startswith(p) for p in answer_set)), None)

def judge_pool(query, cand):
    engine = Q.get_judge()
    out = []
    for d in cand:
        try:
            state = "用户查询：" + query + "\n\n候选内容：\n" + d.get("text", d["label"])
            r = engine.decide(state, "这段候选内容与用户查询相关吗？",
                              options={"relevant": "候选直接讨论或回答了查询所问的内容",
                                       "irrelevant": "候选与查询所问无关"},
                              category="general_relevance")
            d["jev"] = r["probabilities"]["relevant"] if r["answer"] == "relevant" else 0.0
        except Exception:
            d["jev"] = 0.0
        out.append(d)
    return sorted(out, key=lambda d: -d.get("jev", 0))

sys.path.insert(0, r"D:/ADLINK/数据分析/tmp/pilot")
import query as Q  # 复用 get_judge 单例
res = {"vec": {"h1": 0, "h3": 0, "h5": 0, "rr": 0.0},
       "jev": {"h1": 0, "h3": 0, "h5": 0, "rr": 0.0}}
misses = []
t0 = time.time()
for i, (e, qe) in enumerate(zip(exam, qs), 1):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)[:100]
    cand = [docs[j] for j in order]
    vr = hit_rank(cand, e["answer_set"])
    jcand = judge_pool(e["q"], cand[:30])
    jr = hit_rank(jcand, e["answer_set"])
    for key, rk in (("vec", vr), ("jev", jr)):
        if rk == 1: res[key]["h1"] += 1
        if rk and rk <= 3: res[key]["h3"] += 1
        if rk and rk <= 5: res[key]["h5"] += 1
        res[key]["rr"] += (1.0 / rk) if rk else 0.0
    if vr is None or vr > 3:
        misses.append((e["q"], e["answer_set"], vr, jr))
    if i % 10 == 0:
        print(f"  进度 {i}/{len(exam)}  已用 {round(time.time()-t0)}s", flush=True)

n = len(exam)
print(f"\n=== 纯向量     Hit@1={res['vec']['h1']}/{n}  Hit@3={res['vec']['h3']}/{n}  Hit@5={res['vec']['h5']}/{n}  MRR={res['vec']['rr']/n:.3f}")
print(f"=== 向量+Jev  Hit@1={res['jev']['h1']}/{n}  Hit@3={res['jev']['h3']}/{n}  Hit@5={res['jev']['h5']}/{n}  MRR={res['jev']['rr']/n:.3f}")
print(f"\n未进前三的 {len(misses)} 题：")
for q, aset, vr, jr in misses[:10]:
    print(f"  [向量#{vr}|jev#{jr}] {q[:40]}")
