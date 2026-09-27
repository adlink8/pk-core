# -*- coding: utf-8 -*-
# 向量化：层2 摘要 + 层1 用户原话切块 -> conversation_vector.sqlite（BLOB float32, bge-m3@Ollama）
# 断点续跑：已嵌入的 id 跳过
import sqlite3, json, re, time, os, urllib.request

DB_AUTH = r"D:/ADLINK/数据分析/data/canonical/agent/structured/db/agent_conversations.sqlite"
DB_OUT = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
OLLAMA = "http://localhost:11434/api/embed"
BATCH = 32
CHUNK_TARGET = 1000

NOISE = [re.compile(r"^The TodoWrite tool hasn.?t been used", re.I),
         re.compile(r"<task-notification", re.I), re.compile(r"^Caveat:")]

def clean_user(t):
    t = (t or "").strip()
    if not t or any(p.search(t) for p in NOISE):
        return ""
    t = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", t).strip()
    if len(t) > 1500:
        t = t[:1500] + " …[单条截断]"
    return t

def summary_text(sj):
    s = json.loads(sj)
    L = lambda k: s.get(k) or []
    parts = [s.get("theme", "")]
    for k in ("asks", "did", "decisions", "leftovers", "quotes", "keywords", "artifacts"):
        parts += [str(x) for x in L(k)]
    return " ".join(p for p in parts if p)

def embed_batch(texts):
    body = json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))["embeddings"]
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))

con_out = sqlite3.connect(DB_OUT)
con_out.execute("PRAGMA journal_mode=WAL")
con_out.execute("PRAGMA busy_timeout=30000")
con_out.executescript("""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS summaries (
  summary_id TEXT PRIMARY KEY, canonical_session_id TEXT NOT NULL, agent TEXT, cwd TEXT,
  started_at TEXT, seg_no INTEGER DEFAULT 1, range_start INTEGER, range_end INTEGER,
  n_msgs INTEGER, chars_in INTEGER, model TEXT, generated_at TEXT, status TEXT,
  attempts INTEGER, finish TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, summary_json TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sum ON summaries(canonical_session_id, seg_no);
CREATE TABLE IF NOT EXISTS run_log (ts TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS summary_vectors (
  summary_id TEXT PRIMARY KEY, embedding BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS quote_chunks (
  chunk_id TEXT PRIMARY KEY, canonical_session_id TEXT NOT NULL,
  ord_start INTEGER, ord_end INTEGER, text TEXT NOT NULL, embedding BLOB);
""")
con_out.commit()

# ---- 层2 ----
rows = con_out.execute("SELECT summary_id, summary_json FROM summaries WHERE status='ok'").fetchall()
done_vec = {r[0] for r in con_out.execute("SELECT summary_id FROM summary_vectors")}
todo = [(sid, sj) for sid, sj in rows if sid not in done_vec and sj]
print(f"层2: 摘要 {len(rows)} 份，待嵌入 {len(todo)}", flush=True)
t0 = time.time()
for i in range(0, len(todo), BATCH):
    batch = todo[i:i + BATCH]
    embs = embed_batch([summary_text(sj) for _, sj in batch])
    with con_out:
        for (sid, _), emb in zip(batch, embs):
            con_out.execute("INSERT OR REPLACE INTO summary_vectors VALUES (?,?)",
                            (sid, json.dumps(emb)))  # json 存储便于跨语言读取
    if (i // BATCH) % 20 == 0:
        print(f"  层2 {i + len(batch)}/{len(todo)}  {round(time.time()-t0,1)}s", flush=True)

# ---- 层1 ----
eligible = {r[0] for r in con_out.execute(
    "SELECT DISTINCT canonical_session_id FROM summaries WHERE status='ok'")}
done_chunks = {r[0] for r in con_out.execute("SELECT chunk_id FROM quote_chunks")}
con_auth = sqlite3.connect("file:%s?mode=ro" % DB_AUTH, uri=True)
chunks = []
for sid in eligible:
    rows = con_auth.execute(
        "SELECT ordinal, content FROM canonical_messages WHERE canonical_session_id=? "
        "AND role='user' AND COALESCE(is_system,0)=0 AND COALESCE(is_sidechain,0)=0 ORDER BY rowid",
        (sid,)).fetchall()
    cur_texts, cur_start, cur_end, cur_len = [], None, None, 0
    for ordinal, content in rows:
        c = clean_user(content)
        if not c:
            continue
        if cur_len and cur_len + len(c) > CHUNK_TARGET:
            chunks.append((sid, cur_start, cur_end, cur_texts))
            cur_texts, cur_len = [], 0
            cur_start = None
        if cur_start is None:
            cur_start = ordinal
        cur_end = ordinal
        cur_texts.append(c)
        cur_len += len(c)
    if cur_texts:
        chunks.append((sid, cur_start, cur_end, cur_texts))
recs = []
for sid, ostart, oend, texts in chunks:
    text = "\n".join(texts)
    if len(text) < 20:
        continue
    cid = "qc|%s|%d-%d" % (sid, ostart, oend)
    if cid not in done_chunks:
        recs.append((cid, sid, ostart, oend, text))
print(f"层1: 原话块 {len(recs)} 个待嵌入（会话 {len(eligible)} 个中产出）", flush=True)
for i in range(0, len(recs), BATCH):
    batch = recs[i:i + BATCH]
    embs = embed_batch([t for _, _, _, _, t in batch])
    with con_out:
        for (cid, sid, ostart, oend, text), emb in zip(batch, embs):
            con_out.execute("INSERT OR REPLACE INTO quote_chunks VALUES (?,?,?,?,?,?)",
                            (cid, sid, ostart, oend, text, json.dumps(emb)))
    if (i // BATCH) % 20 == 0:
        print(f"  层1 {i + len(batch)}/{len(recs)}  {round(time.time()-t0,1)}s", flush=True)

nv = con_out.execute("SELECT COUNT(*) FROM summary_vectors").fetchone()[0]
nq = con_out.execute("SELECT COUNT(*) FROM quote_chunks").fetchone()[0]
with con_out:
    con_out.execute("INSERT OR REPLACE INTO meta VALUES ('embedding_model','bge-m3-ollama-1024')")
    con_out.execute("INSERT OR REPLACE INTO meta VALUES ('vector_built_at', datetime('now','localtime'))")
print(f"\n=== 向量化完成: 层2 {nv} 份 + 层1 {nq} 块  耗时 {round((time.time()-t0)/60,1)} 分钟 ===", flush=True)
