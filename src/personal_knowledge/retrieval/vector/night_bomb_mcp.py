# -*- coding: utf-8 -*-
# 夜间轰炸·MCP 真链路（2026-09-28 夜任务）。跑在进程内评测之后。
# 干什么：
#   1) 手写 MCP streamable-HTTP 客户端连 127.0.0.1:8789，clientInfo=night-bombard
#      （call_log 的 client 列即此标签，统计真实使用时 WHERE client != 'night-bombard' 可分离）
#   2) v3 原题 184 道走 conversation_search_semantic，与进程内 serving 结果对照：
#      不一致 = MCP 渲染或隐私闸误伤（客户端拿不到会话 id 是真实使用事故）
#   3) FTS 工具/负样本/边角/改写抽样 + 10 线程并发压测
#   4) 校验 call_log：条数对账、ok 率、result_text 完整率、耗时分布
# 红线：只读轰炸；不写任何业务库；KEY 无关。
import json, os, sys, time, sqlite3, threading, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:/ADLINK/数据分析"
EXAM_V3 = os.path.join(ROOT, "docs", "retrieval", "exam_v3.json")
DRAFT = os.path.join(ROOT, "docs", "retrieval", "exam_v4_draft_night_0928.json")
INPROC = os.path.join(ROOT, "var", "reports", "night_bomb_eval_0928.json")
OUTJ = os.path.join(ROOT, "var", "reports", "night_bomb_mcp_0928.json")
CALLLOG = os.path.join(ROOT, "var", "db", "mcp_call_log.sqlite")
BASE = "http://127.0.0.1:8789/mcp"
CLIENT_TAG = "night-bombard"

import httpx

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}


class McpClient:
    def __init__(self, base=BASE):
        self.base = base
        self.session = None
        self.next_id = 0
        self.lock = threading.Lock()
        self.http = httpx.Client(timeout=90)

    def _post(self, payload, expect_body=True):
        h = dict(HEADERS)
        if self.session:
            h["mcp-session-id"] = self.session
        r = self.http.post(self.base, json=payload, headers=h)
        if r.status_code not in (200, 202):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session = sid
        if not expect_body or r.status_code == 202 or not r.content:
            return None
        ct = r.headers.get("content-type", "")
        if "text/event-stream" in ct:
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    blob = line[5:].strip()
                    if blob and blob != "[DONE]":
                        return json.loads(blob)
            return None
        return r.json()

    def initialize(self):
        d = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": CLIENT_TAG, "version": "1.0"}}})
        if not d or "result" not in d:
            raise RuntimeError(f"initialize 失败: {str(d)[:200]}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_body=False)
        info = d["result"].get("serverInfo", {})
        tools = self._post({"jsonrpc": "2.0", "id": -1, "method": "tools/list"})
        n_tools = len(tools["result"]["tools"]) if tools and "result" in tools else -1
        return info, n_tools

    def call_tool(self, name, arguments):
        with self.lock:
            self.next_id += 1
            i = self.next_id
        d = self._post({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
        if not d:
            raise RuntimeError("tools/call 无响应")
        if "error" in d:
            raise RuntimeError(f"rpc error: {str(d['error'])[:150]}")
        content = d["result"].get("content", [])
        return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


def parse_sids(text):
    """从 MCP 文本输出提取「命中会话:」后的 sid（渲染格式见 handler）。"""
    import re
    return re.findall(r"命中会话:\s*(\S+)", text)


print("[MCP] 初始化…", flush=True)
mc = McpClient()
server_info, n_tools = mc.initialize()
print(f"[MCP] server={server_info} tools={n_tools}", flush=True)

report = {"server_info": server_info, "tools": n_tools, "sections": {}}
S = report["sections"]

# ============ 题源 ============
exam = [e for e in json.load(open(EXAM_V3, encoding="utf-8")) if isinstance(e, dict) and e.get("q")]
singles = [e for e in exam if e.get("type") != "aggregate"]
draft = [e for e in json.load(open(DRAFT, encoding="utf-8")) if isinstance(e, dict) and e.get("kind")]
negs = [e for e in draft if e["kind"] == "negative"][:20]
corners = [e for e in draft if e["kind"] == "corner"][:15]
rewrites = [e for e in draft if e["kind"] == "rewrite"][::18][:30]  # 隔 18 抽 1 ≈ 30 道
# 进程内对照基线：同题 serving 直跑的命中名次（eval 脚本产出）
inproc_rank = {}
try:
    ip = json.load(open(INPROC, encoding="utf-8"))
    for r in ip["sections"]["v3_calibration"]["records"]:
        inproc_rank[r["q"]] = r["rank"]
except Exception as ex:
    print(f"[对照] 进程内基线读取失败（将只报 MCP 侧）: {ex!r}", flush=True)

# ============ 阶段1：真链路 184 原题 + 进程内对照 ============
print("\n[M1] 语义工具真链路 184 原题…", flush=True)
rows = []
t0 = time.time()
for i, e in enumerate(singles, 1):
    t = time.time()
    try:
        text = mc.call_tool("conversation_search_semantic", {"query": e["q"], "top_k": 5})
        sids = parse_sids(text)
        hit = next((j + 1 for j, s in enumerate(sids)
                    if any(s.startswith(p) for p in e["answer_set"])), None)
        err = ""
    except Exception as ex:
        text, sids, hit, err = "", [], None, repr(ex)[:120]
    rows.append({"q": e["q"], "mcp_hit": hit, "n_sids": len(sids), "err": err,
                 "ms": round((time.time() - t) * 1000), "text_head": text[:150]})
    if i % 25 == 0:
        el = time.time() - t0
        h1 = sum(1 for r in rows if r["mcp_hit"] == 1)
        print(f"  [M1] {i}/{len(singles)} h1={h1} {round(el)}s ({round(el/i,2)}s/题)", flush=True)

n = len(rows)
h1 = sum(1 for r in rows if r["mcp_hit"] == 1)
h5 = sum(1 for r in rows if r["mcp_hit"])
errored = [r for r in rows if r["err"]]
# 隐私闸/渲染误伤嫌疑：无错误但一条 sid 都没解析出来
sealed_suspect = [r for r in rows if not r["err"] and r["n_sids"] == 0]
# 进程内 vs MCP 真链路 diff：名次不一致 = 渲染/闸门改变了客户端所见
diff_rows = []
for r in rows:
    ipr = inproc_rank.get(r["q"])
    if ipr is not None and ipr != r["mcp_hit"] and not r["err"]:
        diff_rows.append({"q": r["q"][:50], "inproc_rank": ipr, "mcp_rank": r["mcp_hit"]})
lat = sorted(r["ms"] for r in rows)
S["semantic_184"] = {
    "n": n, "hit1": h1, "hit5": h5,
    "mrr": round(sum(1.0 / r["mcp_hit"] for r in rows if r["mcp_hit"]) / max(n, 1), 4),
    "no_sid_rows": len(sealed_suspect), "errors": len(errored),
    "inproc_diff_count": len(diff_rows), "inproc_diff": diff_rows[:20],
    "p50_ms": lat[len(lat)//2], "p95_ms": lat[int(len(lat)*0.95)],
    "rows_head": rows[:5], "err_detail": errored[:5],
    "suspect_detail": sealed_suspect[:10],
}
print(f"[M1] H@1={h1}/{n}  全命中={h5}/{n}  无sid行={len(sealed_suspect)}  错误={len(errored)}  "
      f"P50={S['semantic_184']['p50_ms']}ms P95={S['semantic_184']['p95_ms']}ms", flush=True)

# ============ 阶段2：FTS 工具 + 负样本 + 边角 + 改写抽样 ============
print("\n[M2] FTS 工具 30 题…", flush=True)
fts_rows = []
for e in singles[:30]:
    t = time.time()
    try:
        text = mc.call_tool("conversation_search", {"query": e["q"], "top_k": 5})
        n_rows = text.count("命中会话:")
    except Exception as ex:
        text, n_rows = repr(ex)[:120], -1
    fts_rows.append({"q": e["q"][:40], "hits_rows": n_rows, "ms": round((time.time()-t)*1000)})
S["fts_tool_30"] = {"rows": fts_rows,
                    "avg_ms": round(sum(r["ms"] for r in fts_rows) / max(len(fts_rows), 1))}
print(f"[M2] FTS 工具均值 {S['fts_tool_30']['avg_ms']}ms/题", flush=True)

print("[M2] 负样本 20 题（真链路）…", flush=True)
neg_rows = []
for e in negs:
    try:
        text = mc.call_tool("conversation_search_semantic", {"query": e["q"], "top_k": 5})
        neg_rows.append({"q": e["q"], "output_head": text[:120]})
    except Exception as ex:
        neg_rows.append({"q": e["q"], "output_head": repr(ex)[:120]})
S["negative_20"] = neg_rows

print("[M2] 边角 15 题（真链路）…", flush=True)
corner_rows = []
for e in corners:
    t = time.time()
    try:
        text = mc.call_tool("conversation_search_semantic", {"query": e["q"], "top_k": 5})
        ok = isinstance(text, str) and len(text) > 0
        note = f"len={len(text)} head={text[:60]!r}"
    except Exception as ex:
        ok, note = False, repr(ex)[:120]
    corner_rows.append({"tag": e["topic"], "ok": ok, "ms": round((time.time()-t)*1000), "note": note})
    print(f"  [{e['topic']}] ok={ok} {corner_rows[-1]['ms']}ms", flush=True)
S["corner_15"] = {"failed": sum(1 for r in corner_rows if not r["ok"]), "rows": corner_rows}

print("[M2] 改写抽样 30 题…", flush=True)
rw_rows = []
for e in rewrites:
    t = time.time()
    try:
        text = mc.call_tool("conversation_search_semantic", {"query": e["q"], "top_k": 5})
        sids = parse_sids(text)
        hit = next((j + 1 for j, s in enumerate(sids)
                    if any(s.startswith(p) for p in e["answer_set"])), None)
    except Exception:
        hit = None
    rw_rows.append(hit)
S["rewrite_30"] = {"n": len(rw_rows),
                   "hit1": sum(1 for x in rw_rows if x == 1),
                   "hit5": sum(1 for x in rw_rows)}

# ============ 阶段3：并发压测 ============
print("\n[M3] 并发压测：10 线程 × 30 题…", flush=True)
conc_queries = [e["q"] for e in rewrites] * 1
conc_rows = []


def _conc_one(q):
    t = time.time()
    try:
        text = mc.call_tool("conversation_search_semantic", {"query": q, "top_k": 5})
        return {"ok": True, "ms": round((time.time() - t) * 1000)}
    except Exception as ex:
        return {"ok": False, "ms": round((time.time() - t) * 1000), "err": repr(ex)[:100]}


t0 = time.time()
with ThreadPoolExecutor(max_workers=10) as ex:
    futs = [ex.submit(_conc_one, q) for q in conc_queries]
    for f in as_completed(futs):
        conc_rows.append(f.result())
el = time.time() - t0
oks = [r for r in conc_rows if r["ok"]]
lat = sorted(r["ms"] for r in conc_rows)
S["concurrency_10x30"] = {
    "total": len(conc_rows), "ok": len(oks), "wall_s": round(el, 1),
    "qps": round(len(conc_rows) / el, 2),
    "p50_ms": lat[len(lat)//2], "p95_ms": lat[int(len(lat)*0.95)],
    "err_sample": [r.get("err") for r in conc_rows if not r["ok"]][:5]}
print(f"[M3] 并发完成 {len(oks)}/{len(conc_rows)} ok  QPS={S['concurrency_10x30']['qps']}  "
      f"P95={S['concurrency_10x30']['p95_ms']}ms", flush=True)

# ============ 阶段4：call_log 对账 ============
print("\n[M4] call_log 对账…", flush=True)
try:
    con = sqlite3.connect(f"file:{CALLLOG}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(calls)")]
    tot = con.execute("SELECT COUNT(*) FROM calls WHERE client=?", (CLIENT_TAG,)).fetchone()[0]
    ok_n = con.execute("SELECT COUNT(*) FROM calls WHERE client=? AND ok=1", (CLIENT_TAG,)).fetchone()[0]
    nonempty = con.execute("SELECT COUNT(*) FROM calls WHERE client=? AND "
                           "result_text IS NOT NULL AND length(result_text)>0", (CLIENT_TAG,)).fetchone()[0]
    dur = [r[0] for r in con.execute("SELECT duration_ms FROM calls WHERE client=? AND duration_ms IS NOT NULL", (CLIENT_TAG,))]
    dur.sort()
    qn = dict(con.execute("SELECT quality_note, COUNT(*) FROM calls WHERE client=? GROUP BY 1", (CLIENT_TAG,)).fetchall())
    sent = (len(rows) + len(fts_rows) + len(neg_rows) + len(corner_rows) + len(rw_rows) + len(conc_rows))
    con.close()
    S["call_log_audit"] = {"cols": cols, "client_rows": tot, "sent_estimate": sent,
                           "ok": ok_n, "result_text_nonempty": nonempty,
                           "duration_p50": dur[len(dur)//2] if dur else None,
                           "duration_max": dur[-1] if dur else None,
                           "quality_note_dist": qn,
                           "reconcile": "吻合" if abs(tot - sent) <= max(2, int(sent*0.01)) else "缺口"}
    print(f"[M4] 落库 {tot} vs 发送≈{sent} → {S['call_log_audit']['reconcile']}；"
          f"ok={ok_n} 正文非空={nonempty}", flush=True)
except Exception as ex:
    S["call_log_audit"] = {"error": repr(ex)[:200]}
    print(f"[M4] call_log 校验失败: {ex!r}", flush=True)

json.dump(report, open(OUTJ, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\n=== MCP 真链路完成，结果落 {OUTJ}", flush=True)
