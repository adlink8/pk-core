# -*- coding: utf-8 -*-
# 全量压缩跑批：2,019 会话 -> var/db/conversation_vector.sqlite（层2 会话要点）
# 纪律：权威库只读；断点续跑；失败重试3次后隔离；并发8；花费记账，预测超¥450自动暂停
import sqlite3, json, re, time, os, sys, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_AUTH = r"D:/ADLINK/数据分析/data/canonical/agent/structured/db/agent_conversations.sqlite"
DB_OUT = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
KEY = open(r"C:/Users/li/.stepfun.key").read().strip()
API = "https://api.stepfun.com/step_plan/v1/chat/completions"
MODEL = "step-3.7-flash"
PRICE_IN, PRICE_OUT = 7.0, 20.0
BUDGET_STOP = 450.0
WORKERS = 4
MAX_ATTEMPTS = 3
SEG_WINDOW = 200
ONE_SHOT_MAX_CHARS = 90000

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

SYS_ONE = (
    "你是会话摘要员。任务：把一段用户与 AI 助手的对话压缩成固定结构的 JSON 摘要。\n"
    "铁律：\n"
    "1. 只使用对话中出现的信息；某字段没有内容就输出空数组，禁止推断或补充\n"
    "2. quotes 逐字摘录用户原话，错字保留，不改写\n"
    "3. 除 artifacts/keywords 外，正文各字段合计不超过 500 字（硬上限）\n"
    "4. 只做摘要，不做评价、不给建议\n"
    "输出 JSON（只输出 JSON，不要任何其他文字）：\n"
    '{"theme":"一句话主题，≤40字",'
    '"asks":["用户的关键诉求，3-5条，尽量用原话措辞"],'
    '"did":["做了什么/查明了什么+最终结论，3-6条"],'
    '"decisions":["定了什么、否了什么+原因，没有则空数组"],'
    '"leftovers":["没解决的，没有则空数组"],'
    '"artifacts":["关键文件路径/命令/提交号，只列不解释"],'
    '"quotes":["用户最有代表性的原话2-5条，逐字摘录"],'
    '"keywords":["3-8个专有名词：模型名/工具名/项目名"]}'
)
SYS_SEG = (
    "你是会话摘要员。任务：把一段用户与 AI 助手的对话片段压缩成固定结构的 JSON 摘要。\n"
    "铁律：\n"
    "1. 只使用对话中出现的信息；某字段没有内容就输出空数组，禁止推断或补充\n"
    "2. quotes 逐字摘录用户原话，错字保留，不改写\n"
    "3. 除 artifacts/keywords 外，正文各字段合计不超过 400 字（硬上限）\n"
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
    m = re.search(r"[\[{][\s\S]*[\]}]", raw)
    if m:
        try:
            return json.loads(re.sub(r"[\r\n\t]+", " ", m.group(0)), strict=False)
        except Exception:
            pass
    return None

# ---------- 输出库 ----------
con_out = sqlite3.connect(DB_OUT, check_same_thread=False)
con_out.execute("PRAGMA journal_mode=WAL")
con_out.execute("PRAGMA busy_timeout=30000")
con_out.executescript("""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS summaries (
  summary_id TEXT PRIMARY KEY,
  canonical_session_id TEXT NOT NULL,
  agent TEXT, cwd TEXT, started_at TEXT,
  seg_no INTEGER DEFAULT 1,
  range_start INTEGER, range_end INTEGER,
  n_msgs INTEGER, chars_in INTEGER,
  model TEXT, generated_at TEXT,
  status TEXT, attempts INTEGER, finish TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  summary_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sum ON summaries(canonical_session_id, seg_no);
CREATE TABLE IF NOT EXISTS run_log (ts TEXT, event TEXT, detail TEXT);
""")
con_out.commit()
db_lock = threading.Lock()

def log(event, detail=""):
    with db_lock:
        con_out.execute("INSERT INTO run_log VALUES (datetime('now','localtime'), ?, ?)", (event, detail))
        con_out.commit()

# ---------- 选样 ----------
con = sqlite3.connect("file:%s?mode=ro" % DB_AUTH, uri=True)
MC = ("WITH m AS (SELECT canonical_session_id, COUNT(*) n FROM canonical_messages "
      "WHERE COALESCE(is_system,0)=0 AND COALESCE(is_sidechain,0)=0 "
      "AND role IN ('user','assistant') GROUP BY 1) ")
sessions = con.execute(MC + "SELECT s.canonical_session_id, s.agent, COALESCE(s.cwd,''), "
    "COALESCE(s.started_at,''), m.n FROM m JOIN canonical_sessions s USING(canonical_session_id) "
    "WHERE s.merged=0 AND m.n > 4 AND s.canonical_session_id NOT LIKE '%subagent%' "
    "ORDER BY s.started_at DESC").fetchall()
done = {r[0] for r in con_out.execute(
    "SELECT DISTINCT canonical_session_id FROM summaries WHERE status='ok'")}
todo = [s for s in sessions if s[0] not in done]
skipped_short = {r[0] for r in con_out.execute(
    "SELECT DISTINCT canonical_session_id FROM summaries WHERE status='skipped_short'")}
todo = [s for s in todo if s[0] not in skipped_short]
by_agent = {}
for s in todo:
    by_agent[s[1]] = by_agent.get(s[1], 0) + 1
print(f"合格会话 {len(sessions)}，已完成 {len(done)}，待跑 {len(todo)}", flush=True)
print(f"待跑构成: {json.dumps(by_agent, ensure_ascii=False)}", flush=True)

def transcript_parts(sid):
    con_l = sqlite3.connect("file:%s?mode=ro" % DB_AUTH, uri=True)
    try:
        rows = con_l.execute(
            "SELECT ordinal, role, content FROM canonical_messages "
            "WHERE canonical_session_id=? AND COALESCE(is_system,0)=0 "
            "AND COALESCE(is_sidechain,0)=0 AND role IN ('user','assistant') ORDER BY rowid",
            (sid,)).fetchall()
    finally:
        con_l.close()
    out = []
    for ordinal, role, content in rows:
        c = clean(content, role == 'user')
        if c:
            out.append((ordinal, 'U' if role == 'user' else 'A', c))
    return out

def call_api(sys_prompt, text):
    body = {"model": MODEL, "temperature": 0.2, "max_tokens": 16384,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": text}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(req, timeout=420) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                time.sleep(45)
                continue
            if attempt < MAX_ATTEMPTS and e.code != 429:
                time.sleep(5 * attempt)
                continue
            return None, f"HTTP {e.code}: {e.read().decode()[:150]}"
        except Exception as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(5 * attempt)
                continue
            return None, f"{type(e).__name__}: {str(e)[:150]}"
    return None, "unreachable"

stats = {"cost": 0.0, "ok": 0, "fail": 0, "short": 0, "tok_in": 0, "tok_out": 0, "calls": 0}
stat_lock = threading.Lock()
stop_flag = threading.Event()

def save_summary(sid, agent, cwd, started_at, seg_no, rng, n_msgs, chars, pt, ct, finish, parsed, attempts, status):
    summary_id = f"sv|{sid}|{seg_no}"
    with db_lock:
        con_out.execute("""INSERT OR REPLACE INTO summaries
            (summary_id, canonical_session_id, agent, cwd, started_at, seg_no, range_start, range_end,
             n_msgs, chars_in, model, generated_at, status, attempts, finish, prompt_tokens,
             completion_tokens, summary_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (summary_id, sid, agent, cwd, started_at, seg_no, rng[0], rng[1], n_msgs, chars,
             MODEL, time.strftime("%Y-%m-%d %H:%M:%S"), status, attempts, finish, pt, ct,
             json.dumps(parsed, ensure_ascii=False) if parsed is not None else None))
        con_out.commit()

def process(sess):
    if stop_flag.is_set():
        return
    sid, agent, cwd, started_at, n = sess
    msgs = transcript_parts(sid)
    if not msgs:
        save_summary(sid, agent, cwd, started_at, 1, [0, 0], 0, 0, 0, 0, "none", None, 0, "skipped_short")
        with stat_lock: stats["short"] += 1
        return
    segs = []
    if sum(len(c) for _, _, c in msgs) <= ONE_SHOT_MAX_CHARS:
        segs.append((1, [msgs[0][0], msgs[-1][0]], msgs))
    else:
        for i in range(0, len(msgs), SEG_WINDOW):
            chunk = msgs[i:i + SEG_WINDOW]
            segs.append((len(segs) + 1, [chunk[0][0], chunk[-1][0]], chunk))
    for seg_no, rng, chunk in segs:
        text = "\n\n".join("[%s%d] %s" % (tag, ordinal, c) for ordinal, tag, c in chunk)
        chars = len(text)
        sys_p = SYS_ONE if seg_no == 1 and len(segs) == 1 else SYS_SEG
        attempts = 0
        while attempts < MAX_ATTEMPTS and not stop_flag.is_set():
            attempts += 1
            resp, err = call_api(sys_p, text)
            if err:
                if attempts == MAX_ATTEMPTS:
                    save_summary(sid, agent, cwd, started_at, seg_no, rng, len(chunk), chars, 0, 0, "error", None, attempts, "failed")
                    with stat_lock: stats["fail"] += 1
                    log("fail", f"{sid[:40]} seg{seg_no} {err}")
                continue
            pt = (resp.get("usage", {}) or {}).get("prompt_tokens", 0) or 0
            ct = (resp.get("usage", {}) or {}).get("completion_tokens", 0) or 0
            finish = resp["choices"][0].get("finish_reason")
            raw = (resp["choices"][0]["message"]["content"] or "").strip()
            raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
            parsed = parse_json(raw) if raw else None
            if parsed is not None:
                save_summary(sid, agent, cwd, started_at, seg_no, rng, len(chunk), chars, pt, ct, finish, parsed, attempts, "ok")
                with stat_lock:
                    stats["ok"] += 1; stats["calls"] += 1
                    stats["tok_in"] += pt; stats["tok_out"] += ct
                    stats["cost"] += pt / 1e6 * PRICE_IN + ct / 1e6 * PRICE_OUT
                break
            if attempts == MAX_ATTEMPTS:
                save_summary(sid, agent, cwd, started_at, seg_no, rng, len(chunk), chars, pt, ct, finish, None, attempts, "failed_parse")
                with stat_lock:
                    stats["fail"] += 1; stats["calls"] += 1
                    stats["tok_in"] += pt; stats["tok_out"] += ct
                    stats["cost"] += pt / 1e6 * PRICE_IN + ct / 1e6 * PRICE_OUT
                log("fail_parse", f"{sid[:40]} seg{seg_no}")
    # 花费闸
    with stat_lock:
        if stats["calls"] and stats["calls"] % 200 == 0:
            done_n = stats["ok"] + stats["fail"] + stats["short"]
            proj = stats["cost"] / max(done_n, 1) * len(sessions)
            print(f"[checkpoint] 完成 {done_n}/{len(sessions)}  花费 ¥{stats['cost']:.2f}  全程预测 ¥{proj:.0f}", flush=True)
            log("checkpoint", f"done={done_n} cost={stats['cost']:.2f} proj={proj:.0f}")
            if proj > BUDGET_STOP:
                stop_flag.set()
                log("budget_stop", f"proj={proj:.0f}")

t0 = time.time()
err_shown = 0
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futs = {ex.submit(process, s): s for s in todo}
    for i, f in enumerate(as_completed(futs), 1):
        try:
            f.result()
        except Exception as e:
            err_shown += 1
            if err_shown <= 5:
                print(f"[worker异常] {futs[f][0][:40]}: {type(e).__name__}: {str(e)[:120]}", flush=True)
        if i % 50 == 0:
            print(f"进度 {i}/{len(todo)}  已用 {round((time.time()-t0)/60,1)} 分钟  花费 ¥{stats['cost']:.2f}", flush=True)
if err_shown:
    print(f"[worker异常总数] {err_shown}", flush=True)

status = "budget_stopped" if stop_flag.is_set() else "done"
print(f"\n=== 全量压缩结束({status}): ok={stats['ok']} fail={stats['fail']} short={stats['short']} "
      f"耗时 {round((time.time()-t0)/60,1)} 分钟 花费 ¥{stats['cost']:.2f} ===", flush=True)
log("run_end", json.dumps({**stats, "status": status}, ensure_ascii=False))
