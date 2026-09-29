# -*- coding: utf-8 -*-
# 夜间轰炸·预跑阶段0校准（2026-09-29 凌晨）：v3 184 题走 serving 默认参数。
# 产物 var/reports/night_bomb_calib_0928.json，正式 eval 直接复用，不重跑。
# 提前暴露：评测口径与 eval_exam 基线(161/173)的差值 + 评测代码 bug。
import json, os, sys, time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:/ADLINK/数据分析/src")
ROOT = r"D:/ADLINK/数据分析"
CALIB = os.path.join(ROOT, "var", "reports", "night_bomb_calib_0928.json")

from personal_knowledge.retrieval.vector import serving
serving.load_pool_cached()
print("[预热] 池已加载", flush=True)


def sid_of(h):
    return h.get("sid") or h.get("session_id") or ""


exam = [e for e in json.load(open(os.path.join(ROOT, "docs", "retrieval", "exam_v3.json"),
                                 encoding="utf-8")) if isinstance(e, dict) and e.get("q")]
singles = [e for e in exam if e.get("type") != "aggregate"]
aggs = [e for e in exam if e.get("type") == "aggregate"]

h = {1: 0, 3: 0, 5: 0}
rr, n = 0.0, 0
records = []
t0 = time.time()
for i, e in enumerate(singles, 1):
    try:
        res = serving.search(e["q"], k=5)
        hits = res.get("hits", [])
        rk = next((j + 1 for j, x in enumerate(hits)
                   if any(sid_of(x).startswith(p) for p in e["answer_set"])), None)
    except Exception as ex:
        print(f"  #{i} 异常 {ex!r}", flush=True)
        hits, rk = [], None
    n += 1
    if rk:
        for kk in h:
            if rk <= kk:
                h[kk] += 1
        rr += 1.0 / rk
    records.append({"q": e["q"], "kind": "v3", "topic": e.get("topic", ""), "rank": rk,
                    "engine": res.get("engine"),
                    "top1_score": (hits[0].get("score") if hits and "score" in hits[0] else None),
                    "answer_set": e.get("answer_set", [])[:4],
                    "top_sids": [sid_of(x)[:70] for x in hits[:3]]})
    if i % 25 == 0:
        print(f"  {i}/{len(singles)} h1={h[1]} {round(time.time()-t0)}s", flush=True)

m = {"n": n, "h1": h[1], "h3": h[3], "h5": h[5], "mrr": round(rr / n, 4),
     "hit_rate1": round(h[1] / n, 4)}

agg_partial, agg_n, agg_perfect = 0.0, 0, 0.0
for e in aggs:
    res = serving.search(e["q"], k=5)
    covered = sorted({p for p in e["answer_set"]
                      if any(sid_of(x).startswith(p) for x in res.get("hits", [])[:5])})
    partial = len(covered) / max(len(e["answer_set"]), 1)
    agg_partial += partial
    agg_perfect += (partial == 1.0)
    agg_n += 1
agg_m = {"n": agg_n, "partial_recall5_mean": round(agg_partial / max(agg_n, 1), 4),
         "perfect": agg_perfect} if agg_n else None

out = {"metrics": m, "records": records, "aggregate": agg_m,
       "baseline_eval_exam": {"h1": 161, "n": 173,
                              "note": "eval_exam 裸排+judge 全量；口径差异=collapse窗口/召回50 vs 100"},
       "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
json.dump(out, open(CALIB, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\n=== 校准完成 H@1={h[1]}/{n} MRR={m['mrr']}（基线 161/173=0.931）", flush=True)
if agg_m:
    print(f"=== 聚合 partial@5 均值 {agg_m['partial_recall5_mean']}", flush=True)
print(f"=== 落盘 {CALIB}", flush=True)
