# -*- coding: utf-8 -*-
# 终极评测：答案集考卷 × 两档（纯向量 / 向量+Jev裁判）
# 记分：命中 = top-k 内任一文档的会话匹配 answer_set 任一前缀
# 2026-09-28: 加 --exam/--out 参数与垃圾块检测（默认值不变，v1 行为可复现）
# 2026-09-28(v3): 加 --no-judge（跳过 judge 相关 import 与 GPU 调用，纯向量档）
#   与 aggregate 题型（type=aggregate 按 partial recall@5 单独记分，
#   绝不混入 single 指标；无 type 字段默认 single，行为不变）
import sqlite3, json, time, urllib.request, sys, argparse
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
DEF_EXAM = r"D:/ADLINK/数据分析/docs/retrieval/exam_v1.json"
JUNK_SIGS = ("<system-reminder", "<user_info", "<environment_context",
             '<data-role="user-context">', "<additional_data>")

ap = argparse.ArgumentParser()
ap.add_argument("--exam", default=DEF_EXAM, help="考卷 JSON 路径")
ap.add_argument("--out", default=None, help="逐题明细输出 JSON 路径")
# --no-judge: 为何加——Jev 裁判吃本机 GPU，并行工作流需要独占；纯向量档对比口径统一用它。
# 怎么跳——不 import query（其模块级无副作用但 get_judge 会加载 GGUF 到 GPU）、
#   不调 judge_pool，jev 指标整体不出。默认不加此开关 = 与 2026-09-27 晚基线完全一致。
ap.add_argument("--no-judge", dest="no_judge", action="store_true",
                help="跳过 Jev 重排（纯向量档，不占 GPU）")
ARGS = ap.parse_args()
EXAM = ARGS.exam

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
                 "label": json.loads(sj).get("theme", ""), "vec": json.loads(emb),
                 "junk": False})
for cid, sid, text, emb in con.execute(
        "SELECT c.chunk_id, c.canonical_session_id, c.text, c.embedding "
        "FROM quote_chunks c WHERE EXISTS (SELECT 1 FROM summaries s WHERE "
        "s.canonical_session_id=c.canonical_session_id AND s.status='ok')"):
    junk = text.lstrip().lower().startswith(JUNK_SIGS)
    docs.append({"sid": sid, "seg": 0, "kind": "原话", "text": text[:800],
                 "label": text[:70].replace("\n", " "), "vec": json.loads(emb),
                 "junk": junk})
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

if ARGS.no_judge:
    # --no-judge：judge 相关 import 与 GPU 调用全部跳过，jr 恒为 None
    def judge_pool(query, cand):
        return cand
else:
    sys.path.insert(0, r"D:/ADLINK/数据分析/src/personal_knowledge/retrieval/vector")
    import query as Q  # 复用 get_judge 单例（惰性加载，首次调用才进 GPU）
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

res = {"vec": {"h1": 0, "h3": 0, "h5": 0, "rr": 0.0},
       "jev": {"h1": 0, "h3": 0, "h5": 0, "rr": 0.0}}
# aggregate 题独立记分桶：partial recall@5（top5 覆盖 answer_set 的不同会话数 / answer_set 大小）
res_agg = {"n": 0, "partial_sum": 0.0, "perfect": 0, "scores": []}
misses, records, junk_hits = [], [], 0
t0 = time.time()
for i, (e, qe) in enumerate(zip(exam, qs), 1):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)[:100]
    cand = [docs[j] for j in order]
    vr = hit_rank(cand, e["answer_set"])
    # aggregate 题型分支：为何单独记分——聚合题有多个正确会话，Hit@1 口径会低估且污染 single 指标；
    # 怎么记——partial recall@5 = top5 内匹配 answer_set 的不同会话数 / answer_set 大小，单独汇报。
    if e.get("type") == "aggregate":
        covered = sorted({p for p in e["answer_set"]
                          if any(d["sid"].startswith(p) for d in cand[:5])})
        partial = len(covered) / len(e["answer_set"])
        res_agg["n"] += 1
        res_agg["partial_sum"] += partial
        res_agg["perfect"] += (partial == 1.0)
        res_agg["scores"].append(partial)
        records.append({"q": e["q"], "topic": e.get("topic", ""),
                        "agent": e.get("agent", ""), "origin": e.get("origin", "v1"),
                        "type": "aggregate", "agg_covered": len(covered),
                        "agg_total": len(e["answer_set"]), "agg_partial": round(partial, 4),
                        "vec_rank": None, "jev_rank": None,
                        "top5": [{"kind": d["kind"], "sid": d["sid"], "score": round(float(scores[j]), 4),
                                  "junk": bool(d.get("junk")), "label": d["label"][:60]}
                                 for j, d in zip(order[:5], cand[:5])]})
        if i % 10 == 0:
            print(f"  进度 {i}/{len(exam)}  已用 {round(time.time()-t0)}s", flush=True)
        continue  # aggregate 绝不混入 single 指标
    if ARGS.no_judge:
        jr = None
    else:
        jcand = judge_pool(e["q"], cand[:30])
        jr = hit_rank(jcand, e["answer_set"])
    top5junk = sum(1 for d in cand[:5] if d.get("junk"))
    if top5junk:
        junk_hits += 1
    records.append({"q": e["q"], "topic": e.get("topic", ""),
                    "agent": e.get("agent", ""), "origin": e.get("origin", "v1"),
                    "vec_rank": vr, "jev_rank": jr,
                    "top5": [{"kind": d["kind"], "sid": d["sid"], "score": round(float(scores[j]), 4),
                              "junk": bool(d.get("junk")), "label": d["label"][:60]}
                             for j, d in zip(order[:5], cand[:5])]})
    # --no-judge 时 jev 桶不参与累计，输出里也不出现
    for key, rk in ((("vec", vr),) if ARGS.no_judge else (("vec", vr), ("jev", jr))):
        if rk == 1: res[key]["h1"] += 1
        if rk and rk <= 3: res[key]["h3"] += 1
        if rk and rk <= 5: res[key]["h5"] += 1
        res[key]["rr"] += (1.0 / rk) if rk else 0.0
    if vr is None or vr > 3:
        misses.append((e["q"], e["answer_set"], vr, jr))
    if i % 10 == 0:
        print(f"  进度 {i}/{len(exam)}  已用 {round(time.time()-t0)}s", flush=True)

# single 指标分母只算 single 题（无 aggregate 题时 = len(exam)，与历史完全一致）
n = sum(1 for e in exam if e.get("type") != "aggregate")
print(f"\n=== 纯向量     Hit@1={res['vec']['h1']}/{n}  Hit@3={res['vec']['h3']}/{n}  Hit@5={res['vec']['h5']}/{n}  MRR={res['vec']['rr']/n:.3f}")
if not ARGS.no_judge:
    print(f"=== 向量+Jev  Hit@1={res['jev']['h1']}/{n}  Hit@3={res['jev']['h3']}/{n}  Hit@5={res['jev']['h5']}/{n}  MRR={res['jev']['rr']/n:.3f}")
if res_agg["n"]:
    avg = res_agg["partial_sum"] / res_agg["n"]
    print(f"=== aggregate  n={res_agg['n']}  partial-recall@5 均值={avg:.3f}  全覆盖题={res_agg['perfect']}/{res_agg['n']}")
print(f"=== 垃圾块检测: {junk_hits}/{n} 题 top5 内出现系统上下文垃圾块 ({junk_hits*100//max(n,1)}%)")
print(f"\n未进前三的 {len(misses)} 题：")
for q, aset, vr, jr in misses[:10]:
    print(f"  [向量#{vr}|jev#{jr}] {q[:40]}")
if ARGS.out:
    if ARGS.no_judge:
        res.pop("jev", None)
    outj = {"exam": EXAM, "n": n, "metrics": res, "junk_top5_questions": junk_hits,
            "misses": misses, "records": records}
    if res_agg["n"]:  # aggregate 题单独汇报，不并入 metrics 的 single 桶
        outj["aggregate"] = {"n": res_agg["n"],
                             "partial_recall_at5_mean": round(res_agg["partial_sum"] / res_agg["n"], 4),
                             "perfect_at5": res_agg["perfect"], "scores": res_agg["scores"]}
    with open(ARGS.out, "w", encoding="utf-8") as f:
        json.dump(outj, f, ensure_ascii=False, indent=1)
    print(f"逐题明细已写 {ARGS.out}")
