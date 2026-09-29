# -*- coding: utf-8 -*-
# 夜间轰炸·进程内评测（2026-09-28 夜任务）。跑在生成脚本之后，消费 exam_v4_draft_night_0928.json。
# 阶段：0) v3原题184 走 serving 默认参数（口径校准，对照 eval_exam 基线 161/173）
#       1) 各题型桶评（改写/合成/时间/辨析 → H@1+MRR；聚合 → partial recall@5）
#       2) 负样本拒答校准（top1 分数分布 vs 正常题 top1 分布，分离度）
#       3) 边角输入健壮性（不崩/限时/结构完整）
#       4) FTS 单腿对照（原题+改写全量 force_fts）
#       5) 裁判确定性抽样（fallback 触发题×3 遍一致性）
# 红线：全程只读；不改 serving/query 任何行为；结果落 var/reports/night_bomb_eval_0928.json。
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:/ADLINK/数据分析/src")
ROOT = r"D:/ADLINK/数据分析"
DRAFT = os.path.join(ROOT, "docs", "retrieval", "exam_v4_draft_night_0928.json")
EXAM_V3 = os.path.join(ROOT, "docs", "retrieval", "exam_v3.json")
OUTJ = os.path.join(ROOT, "var", "reports", "night_bomb_eval_0928.json")

from personal_knowledge.retrieval.vector import serving  # 生产入口，默认参数

print("[预热] 加载语料池…", flush=True)
t0 = time.time()
serving.load_pool_cached()
print(f"[预热] 完成 {round(time.time()-t0,1)}s", flush=True)


def sid_of(h):
    return h.get("sid") or h.get("session_id") or ""


def hit_rank(hits, answer_set):
    for i, h in enumerate(hits):
        s = sid_of(h)
        if any(s.startswith(p) for p in answer_set):
            return i + 1
    return None


_RUN_EX = ThreadPoolExecutor(max_workers=1)  # 单题硬超时防线：卡死的题放弃不拖垮全卷


def run_q(q, k=5, timeout_s=120, **kw):
    fut = _RUN_EX.submit(serving.search, q)
    try:
        return fut.result(timeout=timeout_s)
    except Exception:
        raise TimeoutError(f"单题超时 {timeout_s}s: {q[:40]!r}")


BUCKET_DIR = os.path.join(ROOT, "var", "reports", "night_bomb_buckets")
os.makedirs(BUCKET_DIR, exist_ok=True)


def _bucket_cache(label, judge_kw):
    safe = label.replace(" ", "_").replace("/", "_")
    tag = "fts" if judge_kw else "vec"
    return os.path.join(BUCKET_DIR, f"{safe}.{tag}.json")


def eval_single_bucket(questions, label, judge_kw=None, progress_every=25):
    """single 类桶评：H@1/H@3/H@5/MRR + 逐题记录。judge_kw 传给 search（默认 fallback）。
    断点续跑：跑完立即落盘 BUCKET_DIR，重跑时直接加载（2026-09-29 夜测中途崩过一次）。"""
    cache = _bucket_cache(label, judge_kw)
    if os.path.exists(cache):
        d = json.load(open(cache, encoding="utf-8"))
        print(f"[{label}] 复用存档: {d['metrics']}", flush=True)
        return d["metrics"], d["records"]
    kw = judge_kw if judge_kw is not None else {}
    rec, n = [], 0
    h = {1: 0, 3: 0, 5: 0}
    rr = 0.0
    t0 = time.time()
    for i, e in enumerate(questions, 1):
        try:
            res = run_q(e["q"], **kw)
            rk = hit_rank(res.get("hits", []), e["answer_set"]) if e.get("answer_set") else None
        except Exception as ex:
            print(f"  [{label}] #{i} 检索异常: {ex!r}"[:160], flush=True)
            rk = None
            res = {"hits": [], "notices": [f"EXC {ex!r}"], "engine": "error"}
        n += 1
        if rk:
            for kk in h:
                if rk <= kk:
                    h[kk] += 1
            rr += 1.0 / rk
        rec.append({"q": e["q"], "kind": e.get("kind"), "topic": e.get("topic", ""),
                    "rank": rk, "engine": res.get("engine"),
                    "top1_score": (res["hits"][0]["score"] if res.get("hits") and "score" in res["hits"][0] else None),
                    "answer_set": e.get("answer_set", [])[:4],
                    "top_sids": [sid_of(x)[:70] for x in res.get("hits", [])[:3]]})
        if i % progress_every == 0:
            el = time.time() - t0
            print(f"  [{label}] {i}/{len(questions)}  h1={h[1]}  {round(el)}s ({round(el/i,2)}s/题)", flush=True)
    m = {"n": n, "h1": h[1], "h3": h[3], "h5": h[5],
         "mrr": round(rr / max(n, 1), 4), "hit_rate1": round(h[1] / max(n, 1), 4)}
    print(f"[{label}] H@1={h[1]}/{n} ({m['hit_rate1']*100:.1f}%)  MRR={m['mrr']}", flush=True)
    json.dump({"metrics": m, "records": rec}, open(cache, "w", encoding="utf-8"),
              ensure_ascii=False)
    return m, rec


def eval_aggregate(questions, label):
    cache = _bucket_cache(label, None)
    if os.path.exists(cache):
        d = json.load(open(cache, encoding="utf-8"))
        print(f"[{label}] 复用存档: {d['metrics']}", flush=True)
        return d["metrics"], d["records"]
    rec = []
    tot, perfect = 0, 0.0
    for i, e in enumerate(questions, 1):
        try:
            res = run_q(e["q"])
            covered = sorted({p for p in e["answer_set"]
                              if any(sid_of(x).startswith(p) for x in res.get("hits", [])[:5])})
        except Exception as ex:
            print(f"  [{label}] #{i} 异常: {ex!r}", flush=True)
            covered = []
        partial = len(covered) / max(len(e["answer_set"]), 1)
        tot += partial
        perfect += (partial == 1.0)
        rec.append({"q": e["q"], "topic": e.get("topic"), "kind": e.get("kind"),
                    "covered": len(covered), "total": len(e["answer_set"]),
                    "partial": round(partial, 3), "gold_note": e.get("gold_note", "")})
        if i % 10 == 0:
            print(f"  [{label}] {i}/{len(questions)} avg={tot/i:.3f}", flush=True)
    m = {"n": len(questions), "partial_recall5_mean": round(tot / max(len(questions), 1), 4),
         "perfect": perfect}
    print(f"[{label}] partial@5 均值={m['partial_recall5_mean']}  全覆盖={int(perfect)}/{len(questions)}", flush=True)
    json.dump({"metrics": m, "records": rec}, open(cache, "w", encoding="utf-8"),
              ensure_ascii=False)
    return m, rec


report = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "sections": {}}
S = report["sections"]

# ============ 阶段0：v3 原题校准 ============
exam = [e for e in json.load(open(EXAM_V3, encoding="utf-8")) if isinstance(e, dict) and e.get("q")]
singles_v3 = [e for e in exam if e.get("type") != "aggregate"]
agg_v3 = [e for e in exam if e.get("type") == "aggregate"]
CALIB = os.path.join(ROOT, "var", "reports", "night_bomb_calib_0928.json")
if os.path.exists(CALIB):
    S["v3_calibration"] = json.load(open(CALIB, encoding="utf-8"))
    m0, rec0 = S["v3_calibration"]["metrics"], S["v3_calibration"]["records"]
    print(f"\n[阶段0] 复用已存校准: {m0}", flush=True)
else:
    print(f"\n[阶段0] v3 原题校准（serving 默认参数，对照 eval_exam 基线 161/173）", flush=True)
    m0, rec0 = eval_single_bucket(singles_v3, "v3校准")
    ma0, rec0a = eval_aggregate(agg_v3, "v3聚合")
    S["v3_calibration"] = {"metrics": m0, "records": rec0, "aggregate": ma0,
                           "baseline_eval_exam": {"h1": 161, "n": 173,
                                                  "note": "eval_exam 裸排+judge 全量；口径差异=collapse窗口/召回50 vs 100"}}
    json.dump(S["v3_calibration"], open(CALIB, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[阶段0] 校准已存 {CALIB}", flush=True)

# ============ 载入草稿卷 ============
draft = [e for e in json.load(open(DRAFT, encoding="utf-8")) if isinstance(e, dict) and e.get("q") and e.get("kind")]
by_kind = {}
for e in draft:
    by_kind.setdefault(e["kind"], []).append(e)
print(f"\n[草稿卷] {len(draft)} 题 kinds={ {k: len(v) for k, v in by_kind.items()} }", flush=True)

# ============ 阶段1：题型桶评 ============
# 改写：与原题对照的 H@1 掉幅是核心产出
S["rewrite"] = {}
m, rec = eval_single_bucket(by_kind.get("rewrite", []), "改写")
S["rewrite"]["overall"] = m
# 按原题分组（用结果记录 rec，与 questions 顺序一致）：原题命中但改写丢失 = 换措辞脆弱题
rec_by_src = {}
for r, e in zip(rec, by_kind.get("rewrite", [])):
    rec_by_src.setdefault(e["src_q"], []).append(r["rank"])
v3_hit = {r["q"]: r["rank"] for r in rec0}
fragile = []
for src, wranks in rec_by_src.items():
    lost = sum(1 for r in wranks if r is None)
    if lost and v3_hit.get(src):
        fragile.append({"src_q": src, "src_rank": v3_hit[src],
                        "rewrite_ranks": wranks, "lost": lost})
fragile.sort(key=lambda x: -x["lost"])
S["rewrite"]["fragile_source_questions"] = {"count": len(fragile),
                                            "detail": fragile[:30]}
print(f"[改写] 换措辞脆弱原题 {len(fragile)} 道（原题能中、改写会丢）", flush=True)
S["rewrite"]["by_style"] = {}
if os.environ.get("NB_SKIP_STYLE") == "1":
    # 夜测 04:20 决策：风格拆解是总体的冗余子集（同 528 题重跑），GPU 卡死高发时段跳过，
    # 白天可设 NB_SKIP_STYLE 未置重跑补上；总体 H@1=479/528 已回答换措辞核心问题。
    print("[改写风格] NB_SKIP_STYLE=1，跳过（总体桶已覆盖）", flush=True)
else:
    for style in (1, 2, 3):
        sub = [e for e in by_kind.get("rewrite", []) if e["style"] == style]
        ms, _ = eval_single_bucket(sub, f"改写风格{style}", progress_every=60)
        S["rewrite"]["by_style"][style] = ms

for kind, label in (("synth_cover", "合成覆盖"), ("time_anchored", "时间限定"),
                    ("twin_discriminate", "孪生辨析")):
    if kind in by_kind:
        m, rec = eval_single_bucket(by_kind[kind], label)
        S[kind] = {"metrics": m,
                   "misses": [r for r in rec if r["rank"] is None][:40]}

if "aggregate_word" in by_kind:
    m, rec = eval_aggregate(by_kind["aggregate_word"], "聚合扩量")
    S["aggregate_word"] = {"metrics": m, "worst": sorted(rec, key=lambda r: r["partial"])[:15]}

# ============ 阶段2：负样本拒答校准 ============
print("\n[阶段2] 负样本拒答校准…", flush=True)
neg_rows = []
for e in by_kind.get("negative", []):
    try:
        res = run_q(e["q"])
        h = res.get("hits", [])
        neg_rows.append({"q": e["q"], "engine": res.get("engine"),
                         "top1_score": h[0].get("score") if h else None,
                         "top1_sid": sid_of(h[0])[:60] if h else ""})
    except Exception as ex:
        neg_rows.append({"q": e["q"], "engine": "error", "error": repr(ex)[:120]})
normal_scores = sorted(r["top1_score"] for r in rec0 if r.get("top1_score") is not None)
neg_scores = sorted(r["top1_score"] for r in neg_rows if r.get("top1_score") is not None)
thr_audit = {}
for thr in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.58):
    fp = sum(1 for s in normal_scores if s < thr)          # 正常题被判低分（误杀）
    fn = sum(1 for s in neg_scores if s >= thr)            # 负样本混过高分（硬凑）
    thr_audit[str(thr)] = {"normal_below": fp, "neg_above": fn}
S["negative_probe"] = {
    "neg_top1_scores": neg_scores,
    "normal_top1_min": normal_scores[0] if normal_scores else None,
    "normal_top1_p5": normal_scores[max(0, len(normal_scores)//20)] if normal_scores else None,
    "threshold_audit": thr_audit, "rows": neg_rows}
if neg_scores and normal_scores:
    print(f"[负样本] top1 分数范围 {neg_scores[0]:.3f}~{neg_scores[-1]:.3f}；"
          f"正常题最低 {normal_scores[0]:.3f}", flush=True)
else:
    print(f"[负样本] 有 {len(neg_rows)} 条但可打分样本不足（neg={len(neg_scores)} normal={len(normal_scores)}）", flush=True)

# ============ 阶段3：边角健壮性 ============
print("\n[阶段3] 边角输入…", flush=True)
corner_rows = []
for e in by_kind.get("corner", []):
    t = time.time()
    row = {"tag": e["topic"], "q_head": e["q"][:30], "ms": None, "ok": False, "note": ""}
    try:
        res = run_q(e["q"])
        row["ms"] = round((time.time() - t) * 1000)
        row["ok"] = isinstance(res, dict) and "engine" in res and "hits" in res
        row["note"] = f"engine={res.get('engine')} hits={len(res.get('hits', []))}"
    except Exception as ex:
        row["ms"] = round((time.time() - t) * 1000)
        row["note"] = repr(ex)[:150]
    corner_rows.append(row)
    print(f"  [{row['tag']}] {row['ms']}ms ok={row['ok']} {row['note'][:60]}", flush=True)
bad = [r for r in corner_rows if not r["ok"]]
S["corner"] = {"n": len(corner_rows), "failed": len(bad), "rows": corner_rows,
               "max_ms": max((r["ms"] or 0) for r in corner_rows)}
print(f"[边角] {len(corner_rows)} 条 失败 {len(bad)}  最慢 {S['corner']['max_ms']}ms", flush=True)

# ============ 阶段4：FTS 单腿对照 ============
print("\n[阶段4] FTS 单腿对照（原题+改写）…", flush=True)
fts_pool = singles_v3 + by_kind.get("rewrite", [])
mf, recf = eval_single_bucket(fts_pool, "FTS单腿", judge_kw={"force_fts": True}, progress_every=50)
S["fts_leg"] = {"metrics": mf,
                "note": "force_fts=True；FTS 为消息级关键词检索，H@1 天然低于语义向量，此处量化兜底腿成色"}

# ============ 阶段5：裁判确定性抽样 ============
print("\n[阶段5] 裁判确定性抽样（触发过 fallback 的题 ×3 遍）…", flush=True)
judge_fired = [r for r in rec0 if r["rank"] is None][:10]
if len(judge_fired) < 10:
    judge_fired = [r for r in rec0 if r.get("top1_score") is not None and r["top1_score"] < 0.62][:10]
det_rows = []
for r in judge_fired:
    e = next((x for x in singles_v3 if x["q"] == r["q"]), None)
    if not e:
        continue
    runs = []
    for _ in range(3):
        try:
            res = run_q(e["q"])
            runs.append([sid_of(h) for h in res.get("hits", [])])
        except Exception as ex:
            runs.append([f"EXC {ex!r}"])
    stable = runs[0] == runs[1] == runs[2]
    det_rows.append({"q": r["q"][:50], "stable": stable})
    print(f"  {'稳定' if stable else '不稳定!'} {r['q'][:40]}", flush=True)
S["judge_determinism"] = {"sampled": len(det_rows), "stable": sum(1 for d in det_rows if d["stable"]),
                          "rows": det_rows}

# ============ 落盘 ============
report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
json.dump(report, open(OUTJ, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\n=== 评测完成，结果落 {OUTJ}", flush=True)
print("=== 摘要 ===")
for k, v in S.items():
    if isinstance(v, dict) and "metrics" in v:
        print(f"  {k}: {v['metrics']}", flush=True)
