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
#
# 改动四（2026-09-29 MCP 接入）：main() 编排逻辑抽到 serving.py，本文件只剩
#         参数解析+打印薄壳。行为保持重构（同一查询输出逐位一致，有回归基线）；
#         serving.load_pool_cached() 供 MCP 常驻进程复用语料（mtime 失效）。
#         回退：git checkout 本文件 + serving.py。
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
# 改动五（2026-09-29 夜测）：裁判并发锁 + 判定超时自愈。
#   为何（锁）：MCP HTTP 常驻服务下多请求并发（to_thread 工作线程）会同时调同一 GGUF
#         引擎实例，引擎管线非线程安全，并发即崩；夜测压测(10线程)必须先堵住。
#   为何（超时）：夜测 02:25 实测引擎子进程 jev-score.exe 偶发死循环（单次判定烧 CPU
#         2000s+、GPU 满载），decide() 无超时会永久挂起——常驻服务下锁永不释放，
#         后续所有低置信查询全部瘫痪；夜评测卷也因此卡死一次。
#   怎么：judge_candidates 全程持锁；每次 decide 在单线程池里跑，90s 超时记 0 分，
#         连续 2 次超时即判引擎子进程卡死 → close()(含 kill) → 下次 get_judge 重建。
#   回退：删掉 with _JUDGE_LOCK / pool_exec 两段即回到旧行为。
import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _TPEx
_JUDGE_LOCK = _threading.Lock()
_JUDGE_TIMEOUT_S = 40  # 正常单判定 ~0.35s；夜测 GPU 恶劣竞争下 90s 确认太慢，40s 仍极宽容

def get_judge():
    global _JUDGE_ENGINE
    if _JUDGE_ENGINE is None:
        sys.path.insert(0, r"D:/Ollama/dl/jev-style-v3")
        from jev_style_decision_gguf import JevStyleDecisionGGUF
        _JUDGE_ENGINE = JevStyleDecisionGGUF(r"D:/Ollama/dl/jev-style-v3", quant="Q4_K_M",
                                             binary=r"D:/Ollama/dl/jev-style-v3/build/jev-score.exe")
    return _JUDGE_ENGINE

def judge_candidates(query, pool, topn=20):
    """Jev-Style-0.8B 判定腿（Windows 原生进程内直调，去 WSL）——判定语义零改动，
    只包了超时与卡死自愈（见改动五注释）。"""
    engine = get_judge()
    judged = []
    with _JUDGE_LOCK:
        pool_exec = _TPEx(max_workers=1)
        n_timeout = 0
        try:
            for i, d in enumerate(pool[:topn]):
                try:
                    state = "用户查询：" + query + "\n\n候选内容：\n" + d.get("text", d["label"])
                    fut = pool_exec.submit(
                        engine.decide, state, "这段候选内容与用户查询相关吗？",
                        options={"relevant": "候选直接讨论或回答了查询所问的内容",
                                 "irrelevant": "候选与查询所问无关"},
                        category="general_relevance")
                    r = fut.result(timeout=_JUDGE_TIMEOUT_S)
                    n_timeout = 0
                    d["jev"] = r["probabilities"]["relevant"] if r["answer"] == "relevant" else 0.0
                except Exception:
                    d["jev"] = 0.0
                    n_timeout += 1
                    if n_timeout >= 2:  # 连续超时=子进程卡死，杀掉重建，fail-open 继续判余下
                        try:
                            print("[judge] 引擎疑似卡死，杀进程重建", file=sys.stderr, flush=True)
                            engine.close()
                        except Exception:
                            pass
                        globals()["_JUDGE_ENGINE"] = None
                        engine = get_judge()
                        n_timeout = 0
                judged.append(d)
        finally:
            pool_exec.shutdown(wait=False)
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
    import serving  # 检索编排单一来源（改动四），lazy import 避免顶层循环
    res = serving.search(q, k=k, judge_mode=judge_mode, collapse=collapse,
                         force_fts=force_fts, rerank=rerank)
    for notice in res["notices"]:
        print(notice)
    if res.get("failed"):
        sys.exit(1)
    if res["engine"] == "fts":
        for rank, r in enumerate(res["hits"], 1):
            extra = f"  命中词{r['terms']}个" if "terms" in r else ""
            print(f"{rank}. [FTS] hits={r['hits']}{extra}  {r['snippet'][:90].replace(chr(10), ' ')}")
            print(f"   session={r['session_id'][:52]}  agent={r['agent'] or '?'}  "
                  f"started={(r['started_at'] or '?')[:16]}  bm25={r['bm25']:.3f}")
        return
    for rank, d in enumerate(res["hits"], 1):
        print(f"{rank}. {d['kind']}  {d['label'][:90]}")
        print(f"   session={d['sid'][:52]}  seg={d['seg']} range={d['range']}  score={d['score']:.4f}"
              + (f"  [twin簇代表 {d['twin_canonical'][:40]}]" if d.get("twin_canonical") else ""))

if __name__ == "__main__":
    main()
