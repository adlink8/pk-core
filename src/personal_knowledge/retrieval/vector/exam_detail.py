# -*- coding: utf-8 -*-
# 详报生成：87 题 × (题目/答案集/向量第一/Jev重排第一/源名次) → exam_report.md + exam_detail.json
import sqlite3, json, time, urllib.request, sys
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
EXAM = r"D:/ADLINK/数据分析/docs/retrieval/exam_v1.json"
OUT_MD = r"D:/ADLINK/数据分析/docs/retrieval/exam_report.md"
OUT_JSON = r"D:/ADLINK/数据分析/tmp/pilot/exam_detail.json"

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
                 "label": text[:80].replace("\n", " "), "vec": json.loads(emb)})
M = np.array([d["vec"] for d in docs], dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
print(f"语料 {len(docs)}", flush=True)

exam = [e for e in json.load(open(EXAM, encoding="utf-8")) if isinstance(e, dict) and e.get("q")]
qs = embed([e["q"] for e in exam])

sys.path.insert(0, r"D:/ADLINK/数据分析/tmp/pilot")
import query as Q
engine = Q.get_judge()

detail = []
t0 = time.time()
for i, (e, qe) in enumerate(zip(exam, qs), 1):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)[:100]
    cand = [docs[j] for j in order]
    vtop = [{"kind": d["kind"], "label": d["label"], "sid": d["sid"][:48],
             "score": round(float(scores[order[k]]), 4),
             "hit": any(d["sid"].startswith(p) for p in e["answer_set"])} for k, d in enumerate(cand[:5])]
    vr = next((k + 1 for k, d in enumerate(cand) if vtop and any(d["sid"].startswith(p) for p in e["answer_set"])), None)
    vr = next((k + 1 for k, d in enumerate(cand) if any(d["sid"].startswith(p) for p in e["answer_set"])), None)
    judged = []
    for d in cand[:30]:
        try:
            state = "用户查询：" + e["q"] + "\n\n候选内容：\n" + d.get("text", d["label"])
            r = engine.decide(state, "这段候选内容与用户查询相关吗？",
                              options={"relevant": "候选直接讨论或回答了查询所问的内容",
                                       "irrelevant": "候选与查询所问无关"},
                              category="general_relevance")
            d["jev"] = r["probabilities"]["relevant"] if r["answer"] == "relevant" else 0.0
        except Exception:
            d["jev"] = 0.0
        judged.append(d)
    jsorted = sorted(judged, key=lambda d: -d.get("jev", 0))
    jtop = [{"kind": d["kind"], "label": d["label"], "sid": d["sid"][:48],
             "jev": round(d.get("jev", 0), 3),
             "hit": any(d["sid"].startswith(p) for p in e["answer_set"])} for d in jsorted[:5]]
    jr = next((k + 1 for k, d in enumerate(jsorted) if any(d["sid"].startswith(p) for p in e["answer_set"])), None)
    detail.append({"q": e["q"], "topic": e.get("topic", ""), "agent": e.get("agent", ""),
                   "answer_set": e["answer_set"], "vec_rank": vr, "jev_rank": jr,
                   "vec_top5": vtop, "jev_top5": jtop})
    if i % 10 == 0:
        print(f"  {i}/{len(exam)}  {round(time.time()-t0)}s", flush=True)

json.dump(detail, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

h1v = sum(1 for d in detail if d["vec_rank"] == 1)
h1j = sum(1 for d in detail if d["jev_rank"] == 1)
lines = ["# 87 题考卷逐题详报", "",
         f"总览：向量第一命中 {h1v}/87，Jev 重排第一命中 {h1j}/87", ""]
for i, d in enumerate(detail, 1):
    lines.append(f"## Q{i:02d} [{d['agent']}] {d['topic']}")
    lines.append(f"**题目**：{d['q']}")
    lines.append(f"**答案集**：{'、'.join(a[:44] for a in d['answer_set'])}")
    lines.append(f"**源会话名次**：向量 #{d['vec_rank']} → Jev 重排 #{d['jev_rank']}")
    lines.append("**向量召回 Top3**：")
    for k, v in enumerate(d["vec_top5"][:3], 1):
        mark = "✓" if v["hit"] else "✗"
        lines.append(f"  {k}. [{v['kind']}] {v['label'][:60]} (score {v['score']}) {mark}")
    lines.append("**Jev 重排 Top3**：")
    for k, v in enumerate(d["jev_top5"][:3], 1):
        mark = "✓" if v["hit"] else "✗"
        lines.append(f"  {k}. [{v['kind']}] {v['label'][:60]} (相关@{v['jev']}) {mark}")
    lines.append("")
open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines))
print(f"\n=== 详报完成：{OUT_MD}（{h1v} 向量第一命中 / {h1j} Jev 第一命中）===", flush=True)
