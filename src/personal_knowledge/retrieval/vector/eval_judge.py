# -*- coding: utf-8 -*-
# 判断腿评测：10 题 × 合并检索 top-30 → Jev 裁判 → 严格单靶命中率
# 对比三档：纯向量 / +rerank / +rerank+Jev裁判
import sqlite3, json, subprocess, urllib.request
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
JUDGE_WSL_CMD = ["wsl", "-d", "Ubuntu-D", "--", "python3",
                 "/mnt/d/Ollama/dl/jev-style-v3/jev_judge.py"]

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
    for k in ("did", "asks", "quotes"):
        parts += [str(x) for x in L(k)[:3]]
    return " ".join(p for p in parts if p)[:600]

def _patch_jina_reranker():
    import torch
    import transformers.models.xlm_roberta.modeling_xlm_roberta as _m
    if not hasattr(_m, "create_position_ids_from_input_ids"):
        def create_position_ids_from_input_ids(input_ids, padding_idx, past_key_values_length=0):
            mask = input_ids.ne(padding_idx).int()
            incremental_indices = (torch.cumsum(mask, dim=1).type_as(mask) + past_key_values_length) * mask
            return incremental_indices.long() + padding_idx
        _m.create_position_ids_from_input_ids = create_position_ids_from_input_ids

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
print(f"语料 {len(docs)}（层2 {sum(1 for d in docs if d['kind']=='要点')} + 层1 {sum(1 for d in docs if d['kind']=='原话')}）", flush=True)

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
qs = embed([q for q, _, _ in QUERIES])

_patch_jina_reranker()
from sentence_transformers import CrossEncoder
reranker = CrossEncoder("jinaai/jina-reranker-v2-base-multilingual", trust_remote_code=True)

# 每题：向量 top-30 → rerank 排序 → Jev 裁判
judge_requests = []
plans = []
for (q, prefix, segs), qe in zip(QUERIES, qs):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)[:100]
    cand = [docs[i] for i in order]
    rs = reranker.predict([(q, d["label"]) for d in cand])
    r_order = np.argsort(-np.array(rs))
    cand = [cand[i] for i in r_order]
    plans.append({"q": q, "prefix": prefix, "segs": segs, "cand": cand})
    judge_requests.append({"query": q, "candidates": [{"idx": i, "text": d["text"]}
                                                       for i, d in enumerate(cand)]})

print("Jev 裁判中（0.8B，WSL CPU）...", flush=True)
p = subprocess.run(JUDGE_WSL_CMD, input=json.dumps({"requests": judge_requests}, ensure_ascii=False),
                   capture_output=True, text=True, encoding="utf-8", timeout=1800)
if p.returncode != 0:
    print("裁判失败:", p.stderr[-600:]); sys_exit = p.returncode; raise SystemExit(p.returncode)
judged = json.loads(p.stdout)["results"]

def rank_of(cand, prefix, segs):
    return next((i + 1 for i, d in enumerate(cand)
                 if d["sid"].startswith(prefix) and (segs is None or d["seg"] in segs)), None)

res = {"vec": {"h1": 0, "h3": 0}, "rrk": {"h1": 0, "h3": 0}, "jev": {"h1": 0, "h3": 0}}
json.dump(judged, open(r"D:/ADLINK/数据分析/tmp/pilot/judged_dump.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

for i, (plan, jl) in enumerate(zip(plans, judged)):
    q, prefix, segs, cand = plan["q"], plan["prefix"], plan["segs"], plan["cand"]
    qv = np.array(embed([q])[0], dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    vorder = list(np.argsort(-scores)[:100])
    vrank = next((pos + 1 for pos, di in enumerate(vorder)
                  if docs[di]["sid"].startswith(prefix) and (segs is None or docs[di]["seg"] in segs)), None)
    rrank = rank_of(cand, prefix, segs)
    jsorted = sorted(zip(jl, cand), key=lambda x: (x[0].get("p_relevant", 0) if x[0].get("answer") == "relevant" else x[0].get("p_relevant", 0) - 1), reverse=True)
    jrank = rank_of([c for _, c in jsorted], prefix, segs)
    for key, rk in (("vec", vrank), ("rrk", rrank), ("jev", jrank)):
        if rk == 1: res[key]["h1"] += 1
        if rk and rk <= 3: res[key]["h3"] += 1
    print(f"[向量#{vrank}|rerank#{rrank}|jev#{jrank}] {q[:24]}", flush=True)
    for j, d in list(jsorted)[:3]:
        print(f"      jev {j.get('answer','?')[:6]} {j.get('p_relevant',0):.2f}  {d['kind']} seg{d['seg']} {d['label'][:34]}")

n = len(QUERIES)
print(f"\n=== 纯向量(合并top30)  Hit@1={res['vec']['h1']}/{n}  Hit@3={res['vec']['h3']}/{n}")
print(f"=== +rerank           Hit@1={res['rrk']['h1']}/{n}  Hit@3={res['rrk']['h3']}/{n}")
print(f"=== +rerank+Jev裁判   Hit@1={res['jev']['h1']}/{n}  Hit@3={res['jev']['h3']}/{n}")
