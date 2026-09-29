# -*- coding: utf-8 -*-
# 夜间轰炸·题目生成（2026-09-28 夜任务，用户放行）
# 产物：docs/retrieval/exam_v4_draft_night_0928.json（草稿卷，不碰现役 exam_v3.json——
#       基线 161/173 不可污染；晋升需人工审核）。
# 题型：改写552(184×3) + 合成覆盖~400 + 无答案负样本30(手写) + 时间限定30 +
#       孪生辨析20(twin_map簇) + 聚合扩量(5→~45) + 边角50(手写)。
# 红线：全程 mode=ro 只读；KEY/API 从 full_run.py 提取后绝不打印；每类独立容错。
import json, os, re, sys, time, sqlite3, random, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")
random.seed(20260928)
ROOT = r"D:/ADLINK/数据分析"
EXAM_V3 = os.path.join(ROOT, "docs", "retrieval", "exam_v3.json")
VECTOR_DB = os.path.join(ROOT, "var", "db", "conversation_vector.sqlite")
TWIN_MAP = os.path.join(ROOT, "var", "db", "twin_map.json")
OUT = os.path.join(ROOT, "docs", "retrieval", "exam_v4_draft_night_0928.json")
ORIGIN = "v4draft-night-20260928"

# --- flash 通道：从 full_run.py 提取字面量常量（不打印值） ---
_fr = open(os.path.join(ROOT, "src", "personal_knowledge", "retrieval", "vector",
                        "full_run.py"), encoding="utf-8").read()
_ns = {}
for _line in _fr.splitlines():
    m = re.match(r"^(KEY|API|MODEL)\s*=\s*(.+?)\s*$", _line)
    if m:
        try:
            _ns[m.group(1)] = eval(m.group(2))
        except Exception:
            pass
KEY, API = _ns["KEY"], _ns["API"]
MODEL = _ns.get("MODEL", "step-3.7-flash")
print(f"[通道] model={MODEL} api={'已配置' if API else '缺失'}", flush=True)


def flash(prompt, max_tokens=2000, temperature=0.6, retries=5):
    body = {"model": MODEL, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode("utf-8"))
            content = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if content.strip():
                return content
            last = RuntimeError("空响应(200 但 content 空)")  # 实测 ~3%：空串不重试会漏题
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(45 if e.code == 429 else 5 * (i + 1))  # 429 伺候法与 full_run 一致
            continue
        except Exception as e:
            last = e
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"flash 调用失败: {last!r}")


def parse_json(text):
    """宽松提取 LLM 输出里的 JSON（容忍前后缀话与截断）。"""
    lo = [i for i in (text.find("["), text.find("{")) if i >= 0]
    hi = max(text.rfind("]"), text.rfind("}"))
    if lo and hi >= 0:
        try:
            return json.loads(text[min(lo):hi + 1])
        except Exception:
            pass
    # 截断容错：补闭合符再试；仍失败则正则提取字符串字面量
    if lo:
        frag = text[min(lo):].rstrip().rstrip(",")
        for tail in ('"]', '"}', "]"):
            try:
                return json.loads(frag + tail)
            except Exception:
                continue
    strs = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if strs:
        return [s.encode().decode("unicode_escape") if "\\u" in s else s for s in strs]
    raise ValueError(f"无 JSON: {text[:120]!r}")


def batch_map(items, fn, workers=4, label=""):
    """并发执行 fn(item)->result，单项失败返回 None 不拖垮全批。"""
    out = [None] * len(items)
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                out[i] = f.result()
            except Exception as e:
                print(f"  [{label}] #{i} 失败: {e!r}"[:150], flush=True)
            done += 1
            if done % 20 == 0 or done == len(items):
                print(f"  [{label}] {done}/{len(items)}  {round(time.time()-t0)}s", flush=True)
    return out


# ============ 数据载入 ============
exam_raw = json.load(open(EXAM_V3, encoding="utf-8"))
exam = [e for e in exam_raw if isinstance(e, dict) and e.get("q")]
singles = [e for e in exam if e.get("type") != "aggregate"]
print(f"[考卷] v3 共 {len(exam)} 题（single {len(singles)}）", flush=True)

con = sqlite3.connect(f"file:{VECTOR_DB}?mode=ro", uri=True)
summaries = []
for sid, agent, started, theme, sj in con.execute(
        "SELECT canonical_session_id, agent, started_at, "
        "json_extract(summary_json,'$.theme'), summary_json "
        "FROM summaries WHERE status='ok'"):
    summaries.append({"sid": sid, "agent": agent, "started": started or "",
                      "theme": theme or "", "sj": sj})
con.close()
print(f"[语料] summaries {len(summaries)}", flush=True)


def asks_quotes(sj):
    try:
        s = json.loads(sj)
        asks = [str(a) for a in (s.get("asks") or [])][:3]
        quotes = [str(q) for q in (s.get("quotes") or [])][:2]
        return asks, quotes
    except Exception:
        return [], []


sections = {}

# ============ A. 改写压力（184 single × 3） ============
def gen_rewrites(e):
    prompt = ("你帮我把下面这条检索测试题改写成 3 个不同的问法，用于测试会话检索系统对换措辞的鲁棒性。\n"
              "原题：" + e["q"] + "\n"
              "要求：\n"
              "1. 三种风格：①口语随意版 ②句式结构大改（倒装/换成问结论）③关键词同义替换\n"
              "2. 每个改写必须保留原题全部关键信息点（项目名、限定条件、数量要求一个不少）\n"
              "3. 不许添加原题没有的新要求\n"
              '只输出 JSON 数组：["改写1","改写2","改写3"]')
    arr = parse_json(flash(prompt, max_tokens=3000, temperature=0.7))
    assert isinstance(arr, list) and len(arr) >= 3
    return [str(x).strip() for x in arr[:3]]


print("[A] 改写生成…", flush=True)
rw = batch_map(singles, gen_rewrites, label="改写")
sections["rewrite"] = [
    {"q": w, "answer_set": e["answer_set"], "type": "single",
     "topic": e.get("topic", ""), "agent": e.get("agent", ""),
     "origin": ORIGIN, "kind": "rewrite", "src_q": e["q"], "style": i + 1}
    for e, ws in zip(singles, rw) if ws for i, w in enumerate(ws)]
print(f"[A] 改写产题 {len(sections['rewrite'])}", flush=True)

# ============ B. 合成覆盖题（~400，分层抽样） ============
N_SYNTH = 400
by_agent = {}
for s in summaries:
    by_agent.setdefault(s["agent"], []).append(s)
agents = sorted(by_agent, key=lambda a: -len(by_agent[a]))
picks, take = [], N_SYNTH
for a in agents:
    n = max(8, round(len(by_agent[a]) / len(summaries) * N_SYNTH))
    n = min(n, len(by_agent[a]), take)
    take -= n
    picks += random.sample(by_agent[a], n)
    if take <= 0:
        break
print(f"[B] 合成覆盖题抽样 {len(picks)}（家族 {len(agents)} 个）", flush=True)


def gen_synth(s):
    asks, quotes = asks_quotes(s["sj"])
    prompt = ("以下是用户一段工作会话的摘要素材。\n主题：" + s["theme"] +
              "\n要点：" + json.dumps(asks, ensure_ascii=False) +
              "\n原话片段：" + json.dumps(quotes, ensure_ascii=False) +
              "\n请以用户本人事后回忆的口吻，生成 1 条用来搜回这段会话的自然问题。要求：\n"
              "1. 15-50 字，口语化，像随手在 AI 助手里敲的话\n"
              "2. 覆盖主题核心，但不得连续照抄摘要原句超过 10 字\n"
              "3. 不要出现会话 id、文件路径、精确日期\n"
              '只输出 JSON：{"q":"问题"}')
    d = parse_json(flash(prompt, temperature=0.8))
    return str(d["q"]).strip()


print("[B] 合成覆盖题生成…", flush=True)
sy = batch_map(picks, gen_synth, label="合成")
sections["synth"] = [
    {"q": q, "answer_set": [s["sid"]], "type": "single",
     "topic": s["theme"][:40], "agent": s["agent"], "origin": ORIGIN,
     "kind": "synth_cover", "src_sid": s["sid"]}
    for s, q in zip(picks, sy) if q]
print(f"[B] 合成覆盖题产题 {len(sections['synth'])}", flush=True)

# ============ C. 无答案负样本（30，手写） ============
NEG_TOPICS = [
    "小提琴手工制琴师傅怎么挑选", "马术场地障碍赛的基础训练方法", "波尔多左岸红酒分级制度",
    "冰岛语名词变格规律", "帆船龙骨养护", "家庭养蜂取蜜流程", "净土宗早晚课仪轨顺序",
    "量子化学的高斯计算入门", "希腊神话十二主神谱系考据", "爵士乐即兴和弦替换套路",
    "手工陶艺拉坯技巧", "城市公园观鸟入门", "日本茶道表千家点前流程", "古钱币真伪鉴定",
    "勃艮第葡萄酒产区划分", "芭蕾舞基础把杆动作", "法棍面包的配比与烘烤", "野外攀岩保护站搭建",
    "水草缸造景布局", "深空天文摄影后期堆栈", "胶片相机冲扫流程", "手工皮具植鞣鞣制",
    "围棋定式大雪崩变化", "威士忌泥煤度分区", "潜水 OW 证考核内容", "多肉植物度夏养护",
    "黑胶唱片机唱针更换", "观星入门望远镜选配", "面包板电路入门", "手工羊毛毡戳戳乐",
]
sections["negative"] = [
    {"q": q, "answer_set": [], "type": "negative", "topic": "无答案探针",
     "agent": "", "origin": ORIGIN, "kind": "negative"}
    for q in NEG_TOPICS]
print(f"[C] 负样本题 {len(sections['negative'])}（手写）", flush=True)

# ============ D. 时间限定题（30，flash 自然嵌入日期） ============
t_picks = [s for s in summaries if len(s["started"]) >= 10 and s["theme"]]
t_picks = random.sample(t_picks, min(30, len(t_picks)))


def gen_time(s):
    d = s["started"][:10]
    y, m, day = d[:4], int(d[5:7]), int(d[8:10])
    prompt = (f"用户的某段会话发生在 {y} 年 {m} 月 {day} 日，主题：「{s['theme']}」。\n"
              "请生成 1 条检索该会话的自然问题，要求：\n"
              "1. 问题里自然嵌入时间线索（如\"X月X号那次\"）+ 主题内容\n"
              "2. 15-50 字口语化\n"
              '只输出 JSON：{"q":"问题"}')
    r = parse_json(flash(prompt, temperature=0.7))
    return str(r["q"]).strip()


print("[D] 时间限定题生成…", flush=True)
tm = batch_map(t_picks, gen_time, label="时间")
sections["time"] = [
    {"q": q, "answer_set": [s["sid"]], "type": "single",
     "topic": s["theme"][:40], "agent": s["agent"], "origin": ORIGIN,
     "kind": "time_anchored", "src_sid": s["sid"], "src_date": s["started"][:10]}
    for s, q in zip(t_picks, tm) if q]
print(f"[D] 时间题产题 {len(sections['time'])}", flush=True)

# ============ E. 孪生辨析题（20，twin_map 簇内两两会话） ============
disc_pairs = []
try:
    tmap = json.load(open(TWIN_MAP, encoding="utf-8"))
    canon = {}
    for sid, c in (tmap.get("session_to_canonical") or tmap).items() if isinstance(tmap, dict) else []:
        canon.setdefault(c, []).append(sid)
    clusters = [v for v in canon.values() if len(v) >= 2]
    sid2s = {s["sid"]: s for s in summaries}
    for cl in clusters:
        cl = [x for x in cl if x in sid2s]
        if len(cl) >= 2:
            disc_pairs.append(random.sample(cl, 2))
    random.shuffle(disc_pairs)
    disc_pairs = disc_pairs[:20]
except Exception as e:
    print(f"[E] twin_map 读取失败: {e!r}", flush=True)
print(f"[E] 辨析对 {len(disc_pairs)}", flush=True)


def gen_disc(pair):
    a, b = pair
    prompt = ("用户有两段主题相近的会话：\n"
              f"会话A主题：「{a['theme']}」\n会话B主题：「{b['theme']}」\n"
              "请生成 1 条明确指向会话A、并排除会话B的检索问题（类似\"是关于…的那次，不是…那次\"）。"
              "要求自然口语 15-50 字，两个主题都要出现在问句里。\n"
              '只输出 JSON：{"q":"问题"}')
    r = parse_json(flash(prompt, temperature=0.7))
    return str(r["q"]).strip()


print("[E] 辨析题生成…", flush=True)
dc = batch_map(disc_pairs, gen_disc, label="辨析") if disc_pairs else []
sections["discriminate"] = [
    {"q": q, "answer_set": [a["sid"]], "type": "single",
     "topic": f"辨析:{a['theme'][:25]}vs{b['theme'][:25]}", "agent": a["agent"],
     "origin": ORIGIN, "kind": "twin_discriminate", "distractor_sid": b["sid"]}
    for (a, b), q in zip(disc_pairs, dc) if q]
print(f"[E] 辨析题产题 {len(sections['discriminate'])}", flush=True)

# ============ F. 聚合扩量（5 → ~45，程序化挖词 + flash 问法） ============
from collections import Counter
word_sessions = {}
for s in summaries:
    seen = set()
    for w in re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z0-9_.\-]{2,}", s["theme"]):
        w = w.strip("的了呢吗")
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            word_sessions.setdefault(w, set()).add(s["sid"])
STOP = {"会话", "问题", "讨论", "关于", "怎么", "如何", "什么", "一个", "自己",
        "项目", "分析", "优化", "修复", "测试", "总结", "记录", "相关", "进行"}
cands = [(w, len(v)) for w, v in word_sessions.items()
         if 5 <= len(v) <= 12 and w not in STOP]
cands.sort(key=lambda x: -x[1])
agg_words = [w for w, _ in cands[:60]]


def filter_words(words):
    prompt = ("下面是从用户会话主题里挖出的高频词，请挑出「像真实项目名/工具名/明确业务主题」的词"
              "（排除过于宽泛的日常词），最多 45 个：\n" + json.dumps(words, ensure_ascii=False) +
              '\n只输出 JSON 数组：["词1","词2",…]')
    return [str(x) for x in parse_json(flash(prompt, temperature=0.2))][:45]


try:
    agg_words = filter_words(agg_words)
except Exception as e:
    print(f"[F] 词过滤失败退回程序化截断: {e!r}", flush=True)
print(f"[F] 聚合词 {len(agg_words)}: {agg_words[:10]}…", flush=True)


def gen_agg(word):
    sids = sorted(word_sessions[word])[:8]
    prompt = (f"用户在多个会话里都聊过「{word}」相关内容。生成 1 条聚合式检索问题，"
              "把相关的主要会话都找出来（类似\"关于X的会话都有哪些，列主要的\"），"
              "15-50 字口语化。\n"
              '只输出 JSON：{"q":"问题"}')
    r = parse_json(flash(prompt, temperature=0.6))
    return str(r["q"]).strip(), sids


def _agg_job(word):
    q, sids = gen_agg(word)
    return q, sids


print("[F] 聚合题生成…", flush=True)
ag = batch_map(agg_words, _agg_job, label="聚合") if agg_words else []
sections["aggregate"] = [
    {"q": q, "answer_set": sids, "type": "aggregate", "topic": w,
     "agent": "", "origin": ORIGIN, "kind": "aggregate_word", "gold_note": "程序化gold(含词会话cap8)可能不完整"}
    for w, (q, sids) in zip(agg_words, ag) if q and len(sids) >= 3]
print(f"[F] 聚合题产题 {len(sections['aggregate'])}", flush=True)

# ============ G. 边角输入（50，手写） ============
long_text = ("这是一段超长无意义测试文本。" * 400)[:10000]
sections["corner"] = [
    {"q": q, "type": "corner", "kind": "corner", "origin": ORIGIN,
     "answer_set": [], "topic": tag}
    for tag, q in [
        ("空串", ""), ("纯空白", "   \t\n  "), ("单字符", "a"), ("单汉字", "搜"),
        ("纯数字", "1234567890"), ("纯标点", "？！。，、；：""''"), ("纯emoji", "🚀🔥🎯💡😂👍"),
        ("SQL注入", "'; DROP TABLE summaries; --"), ("路径串", "C:\\Users\\li\\Desktop\\数据分析"),
        ("格式串", "%s%d%n%p%x" * 8), ("日文", "先週の会話で機械学習について話しましたか"),
        ("繁体", "關於爬蟲代理池的那次討論"), ("韩文", "지난주 대화에서 크롤링 이야기 했나요"),
        ("俄文", "обсуждение парсинга на прошлой неделе"), ("阿拉伯文", "بحث حول تجريف الويب"),
        ("超长1万字", long_text), ("中英混拼", "那个 jwt token 过期的bug咋处理的"),
        ("错别字", "帮找下关于分布式爬虫的 дискуссию"), ("极短问句", "嗯?"),
        ("重复字符", "爬" * 500),
        ("密钥样探针", "那个 api_key 配置该怎么写"),
        ("会话id直查", "cs|legacy|cs/zcode"),
        ("markdown注入", "```python\nimport os\nos.system('dir')\n``` 找这个会话"),
        ("超宽字符", "ｗｉｄｅ字符测试ｱｲｳｴｵ"),
        ("零宽字符", "爬\u200b虫\u200b代\u200b理"),
    ]] * 2
print(f"[G] 边角题 {len(sections['corner'])}（手写模板×2）", flush=True)

# ============ 汇总落盘 ============
meta = {
    "generated_at": "2026-09-28夜", "version": "v4draft-night",
    "origin": ORIGIN,
    "purpose": "夜间轰炸草稿卷：改写鲁棒性/合成覆盖上限/负样本拒答校准/时间盲区/孪生辨析/聚合扩量/边角健壮。"
               "不碰现役 exam_v3.json；晋升需人工审核（合成/聚合为程序化gold，辨析/时间为flash生成需抽验）。",
    "counts": {k: len(v) for k, v in sections.items()},
    "db_readonly": "全程 SQLite mode=ro 只读",
}
draft = [meta] + [q for v in sections.values() for q in v]
json.dump(draft, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
total = sum(len(v) for v in sections.values())
print(f"\n=== 草稿卷落盘 {OUT}")
print(f"=== 总题数 {total}  分布 {meta['counts']}", flush=True)
