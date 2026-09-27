# -*- coding: utf-8 -*-
# 第一关：巨型会话机械分段重压——range 由代码生成，模型只管压缩
import sqlite3, json, re, time, os, urllib.request

DB = r"D:/ADLINK/数据分析/data/canonical/agent/structured/db/agent_conversations.sqlite"
KEY = open(r"C:/Users/li/.stepfun.key").read().strip()
API = "https://api.stepfun.com/v1/chat/completions"
OUT = r"D:/ADLINK/数据分析/tmp/pilot"
WINDOW = 200  # 每段有效消息数

NOISE = [re.compile(r"^The TodoWrite tool hasn.?t been used", re.I),
         re.compile(r"<task-notification", re.I), re.compile(r"^Caveat:")]

def clean(t, is_user):
    t = (t or "").strip()
    if not t or any(p.search(t) for p in NOISE):
        return ""
    if is_user:
        t = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", t).strip()
        if len(t) > 1500:
            t = t[:1500] + " …[单条截断]"
    else:
        if len(t) > 500:
            t = t[:500] + " …[截断]"
    return t

def load_messages(sid):
    rows = con.execute(
        "SELECT ordinal, role, content FROM canonical_messages "
        "WHERE canonical_session_id=? AND COALESCE(is_system,0)=0 "
        "AND COALESCE(is_sidechain,0)=0 AND role IN ('user','assistant') ORDER BY rowid",
        (sid,)).fetchall()
    out = []
    for ordinal, role, content in rows:
        c = clean(content, role == 'user')
        if c:
            out.append((ordinal, 'U' if role == 'user' else 'A', c))
    return out

SYS_SEG = (
    "你是会话摘要员。任务：把一段用户与 AI 助手的对话片段压缩成固定结构的 JSON 摘要。\n"
    "铁律：\n"
    "1. 只使用对话中出现的信息；某字段没有内容就输出空数组，禁止推断或补充\n"
    "2. quotes 逐字摘录用户原话，错字保留，不改写\n"
    "3. 除 artifacts/keywords 外，正文各字段合计不超过 400 字\n"
    "4. 只做摘要，不做评价、不给建议\n"
    "输出 JSON（只输出 JSON，不要任何其他文字）：\n"
    '{"theme":"这一段在做什么，≤30字",'
    '"asks":["这段里用户的关键诉求，尽量用原话措辞"],'
    '"did":["这段做了什么+结论，2-4条"],'
    '"decisions":["定了什么、否了什么+原因，没有则空数组"],'
    '"leftovers":["没解决的，没有则空数组"],'
    '"artifacts":["关键文件路径/命令/提交号，只列不解释"],'
    '"quotes":["用户最有代表性的原话1-3条，逐字摘录"],'
    '"keywords":["3-6个专有名词"]}'
)

def parse_json(raw):
    for fn in (lambda s: json.loads(s),
               lambda s: json.loads(s, strict=False),
               lambda s: json.loads(re.sub(r"[\r\n\t]+", " ", s))):
        try:
            return fn(raw)
        except Exception:
            continue
    return {"_parse_failed": True, "_raw_head": raw[:200]}

con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
sid = [r["session_id"] for r in json.load(open(os.path.join(OUT, "all_results.json"), encoding="utf-8"))
       if r["session_id"].startswith("cs|legacy|cs/codex/019fae6a")][0]
msgs = load_messages(sid)
print(f"巨型会话 {sid[:36]}… 有效消息 {len(msgs)} 条，按 {WINDOW} 条/段切分", flush=True)

segments = []
for i in range(0, len(msgs), WINDOW):
    chunk = msgs[i:i + WINDOW]
    text = "\n\n".join("[%s%d] %s" % (tag, ordinal, c) for ordinal, tag, c in chunk)
    segments.append({"seg_no": len(segments) + 1,
                     "range": [chunk[0][0], chunk[-1][0]],
                     "n": len(chunk), "chars": len(text), "text": text})
for s in segments:
    print(f"  段{s['seg_no']}: range={s['range']} {s['n']}条 {s['chars']:,}字", flush=True)

results = []
tin = tout = 0
for s in segments:
    body = {"model": "step-3.7-flash", "temperature": 0.2, "max_tokens": 16384,
            "messages": [{"role": "system", "content": SYS_SEG},
                         {"role": "user", "content": s["text"]}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  段{s['seg_no']} 调用失败: {str(e)[:120]}", flush=True)
        results.append({"seg_no": s["seg_no"], "range": s["range"], "error": str(e)[:200]})
        continue
    u = resp.get("usage", {}) or {}
    tin += u.get("prompt_tokens", 0) or 0
    tout += u.get("completion_tokens", 0) or 0
    raw = (resp["choices"][0]["message"]["content"] or "").strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    parsed = parse_json(raw) if raw else {"_empty": True}
    ok = not (isinstance(parsed, dict) and (parsed.get("_parse_failed") or parsed.get("_empty")))
    rec = {"seg_no": s["seg_no"], "range": s["range"], "n_msgs": s["n"],
           "session_id": sid, "model": "step-3.7-flash",
           "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
           "seconds": round(time.time() - t0, 1), "ok": ok, "summary": parsed}
    results.append(rec)
    print(f"  段{s['seg_no']} {'OK ' if ok else 'BAD'} {u.get('prompt_tokens')}+{u.get('completion_tokens')} tok {rec['seconds']}s  {parsed.get('theme','') if ok else ''}", flush=True)

json.dump(results, open(os.path.join(OUT, "giant_mech_segments.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"\n=== 机械分段完成 {sum(1 for r in results if r.get('ok'))}/{len(segments)} 段  输入{tin:,}+输出{tout:,} tok ≈ ¥{tin/1e6*7 + tout/1e6*20:.2f} ===", flush=True)
