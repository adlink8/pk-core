# -*- coding: utf-8 -*-
"""bench_serving.py —— 检索服务侧独立评测（twin-collapse × 裁判 fallback 四组对比）

与 eval_exam.py 的关系：语料加载与记分口径参考它（hit = top-k 内任一文档的会话 id 匹配
answer_set 任一前缀；裁判只看 top-30 候选、命中排名在已判名单内计算），但独立实现，
不 import 它、不改它；裁判引擎复用 query.get_judge 单例（Windows 进程内直调
jev-score.exe，同一时间只允许一个进程用 GPU，跑四组必须串行）。

参数：
  --exam PATH          考卷 JSON（默认 docs/retrieval/exam_v2.json，173 题基准）
  --collapse           开启 twin-collapse 召回聚合去重（读 var/db/twin_map.json，缺则 fail-open）
  --fallback           开启裁判 fallback（= --judge-mode fallback 的简写）
  --judge-mode M       always|fallback|never；显式指定优先于 --fallback；缺省 always（昨晚口径）
  --limit N            只跑前 N 题（抽样时须自行说明抽样方式）
  --out PATH           逐题明细 JSON（默认 var/reports/twin_bench_<tag>.json）

四组对比的标准跑法（串行，GPU 裁判同时只允许一个进程）：
  python bench_serving.py --out var/reports/twin_bench_g1.json                # 1 基线(judge always)
  python bench_serving.py --fallback --out var/reports/twin_bench_g2.json     # 2 +fallback
  python bench_serving.py --collapse --out var/reports/twin_bench_g3.json     # 3 +collapse(judge always)
  python bench_serving.py --collapse --fallback --out var/reports/twin_bench_g4.json  # 4 +both

指标：Hit@1 / Hit@3 / Hit@5 / MRR + 总耗时 + 裁判实际触发题数与判定条数。
"""
import sqlite3, json, time, urllib.request, sys, argparse
import numpy as np

DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
DEF_EXAM = r"D:/ADLINK/数据分析/docs/retrieval/exam_v2.json"
DEF_OUT_DIR = r"D:/ADLINK/数据分析/var/reports"
JUNK_SIGS = ("<system-reminder", "<user_info", "<environment_context",
             '<data-role="user-context">', "<additional_data>")

sys.path.insert(0, r"D:/ADLINK/数据分析/src/personal_knowledge/retrieval/vector")
import query as Q  # 只复用 get_judge/judge_candidates/load_twin_map/should_fallback 单例与阈值

ap = argparse.ArgumentParser()
ap.add_argument("--exam", default=DEF_EXAM)
ap.add_argument("--collapse", action="store_true")
ap.add_argument("--fallback", action="store_true", help="= --judge-mode fallback")
ap.add_argument("--judge-mode", choices=["always", "fallback", "never"], default=None)
ap.add_argument("--limit", type=int, default=0, help=">0 时只跑前 N 题")
ap.add_argument("--out", default=None)
ARGS = ap.parse_args()
JUDGE_MODE = ARGS.judge_mode or ("fallback" if ARGS.fallback else "always")
TAG = ("c" if ARGS.collapse else "-") + {"always": "a", "fallback": "f", "never": "n"}[JUDGE_MODE]
OUT = ARGS.out or "%s/twin_bench_%s.json" % (DEF_OUT_DIR, TAG)

def embed(texts):
    body = json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8")
    req = urllib.request.Request("http://localhost:11434/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["embeddings"]

def doc_text(sj):
    """与昨晚 eval_exam 口径一致：theme + 前3条asks/quotes，截600字（裁判 state 用）"""
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

def load_corpus():
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
    con.close()
    return docs

def recall_candidates(order, scores, s2c, depth=100):
    """构造候选（按分数降序）。collapse 时只有 twin_map 里成簇的会话参与归并（同簇只留
    最高分文档，名额让给不同簇），未映射会话一律不动；否则取纯 top-depth。
    文档就地写 score 与 twin_canonical（供 fallback 判定用）。"""
    cand, seen = [], set()
    for pos, i in enumerate(order):
        d = docs[i]
        if s2c:
            rep = s2c.get(d["sid"])
            if rep is not None:
                if rep in seen:
                    continue
                seen.add(rep)
                d["twin_canonical"] = rep
        elif pos >= depth:
            break
        d["score"] = float(scores[i])
        cand.append(d)
        if len(cand) >= depth:
            break
    return cand

def hit_rank(cand, answer_set):
    return next((i + 1 for i, d in enumerate(cand)
                 if any(d["sid"].startswith(p) for p in answer_set)), None)

# ============ 主流程 ============
docs = load_corpus()
M = np.array([d["vec"] for d in docs], dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
n_vec = sum(1 for d in docs if d["kind"] == "要点")
print("语料 %d（层2 %d + 层1 %d）  collapse=%s judge=%s" % (
    len(docs), n_vec, len(docs) - n_vec, ARGS.collapse, JUDGE_MODE), flush=True)

s2c = Q.load_twin_map() if ARGS.collapse else None
if ARGS.collapse:
    print("twin_map: %s" % ("映射 %d 会话" % len(s2c) if s2c else "不可用，降级为不归并"))

exam = json.load(open(ARGS.exam, encoding="utf-8"))
exam = [e for e in exam if isinstance(e, dict) and e.get("q")]
if ARGS.limit > 0:
    exam = exam[:ARGS.limit]
print("考卷 %d 题" % len(exam), flush=True)
qs = embed([e["q"] for e in exam])

res = {"h1": 0, "h3": 0, "h5": 0, "rr": 0.0}
res_vec = dict(res)  # 本跑法下的纯向量参考（collapse 会改变它）
n_judged_q = n_judged_pairs = 0
records = []
t0 = time.time()
for i, (e, qe) in enumerate(zip(exam, qs), 1):
    qv = np.array(qe, dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = M @ qv
    order = np.argsort(-scores)
    cand = recall_candidates(order, scores, s2c, depth=100)       # 服务最终候选（可能已 collapse）
    cand_raw = recall_candidates(order, scores, None, depth=100)  # 纯向量参考（不 collapse）
    vr_final = hit_rank(cand, e["answer_set"])
    vr_vec = hit_rank(cand_raw, e["answer_set"])
    if JUDGE_MODE == "always":
        trigger, why = True, "always"
    elif JUDGE_MODE == "fallback":
        trigger, why = Q.should_fallback(cand)
    else:
        trigger, why = False, ""
    if trigger:
        # 与昨晚口径一致：裁判只判 top-30，命中排名在已判名单内计算
        jcand = Q.judge_candidates(e["q"], cand[:30], topn=30)
        n_judged_q += 1
        n_judged_pairs += len(jcand)
        final_rank = hit_rank(jcand, e["answer_set"])
        final_top = jcand[:3]
    else:
        final_rank = vr_final
        final_top = cand[:3]
    for key, rk, base in (("final", final_rank, res), ("vec", vr_vec, res_vec)):
        if rk == 1: base["h1"] += 1
        if rk and rk <= 3: base["h3"] += 1
        if rk and rk <= 5: base["h5"] += 1
        base["rr"] += (1.0 / rk) if rk else 0.0
    records.append({"q": e["q"], "topic": e.get("topic", ""), "triggered": trigger,
                    "trigger_why": why,
                    "final_rank": final_rank, "vec_rank_serving": vr_final,
                    "vec_rank_raw": vr_vec,
                    "final_top3": [{"sid": d["sid"], "kind": d["kind"],
                                    "score": round(d["score"], 4)} for d in final_top]})
    if i % 20 == 0:
        print("  进度 %d/%d  已用 %ds  裁判已触发 %d 题" % (
            i, len(exam), time.time() - t0, n_judged_q), flush=True)

n = len(exam)
wall = time.time() - t0
def line(tag, r):
    return "%s  Hit@1=%d/%d  Hit@3=%d/%d  Hit@5=%d/%d  MRR=%.3f" % (
        tag, r["h1"], n, r["h3"], n, r["h5"], n, r["rr"] / n)
print("\n=== 最终口径   %s" % line("FINAL", res))
print("=== 纯向量参考 %s" % line("VEC  ", res_vec))
print("=== 耗时 %.1fs（裁判触发 %d 题、判定 %d 条候选）" % (wall, n_judged_q, n_judged_pairs))

with open(OUT, "w", encoding="utf-8") as f:
    json.dump({"exam": ARGS.exam, "n": n, "collapse": ARGS.collapse, "judge_mode": JUDGE_MODE,
               "h1h3h5_final": {k: res[k] for k in ("h1", "h3", "h5")},
               "h1h3h5_vec": {k: res_vec[k] for k in ("h1", "h3", "h5")},
               "mrr_final": round(res["rr"] / n, 4), "mrr_vec": round(res_vec["rr"] / n, 4),
               "wall_sec": round(wall, 1), "judge_questions": n_judged_q,
               "judge_pairs": n_judged_pairs, "records": records},
              f, ensure_ascii=False, indent=1)
print("明细已写 %s" % OUT)
