# -*- coding: utf-8 -*-
"""全库双胞胎会话映射（检索服务侧，2026-09-28）

职责：对语料全部会话按「兄弟规则（定稿）」建边，取连通分量成簇，选出确定性 canonical，
写出 var/db/twin_map.json 供 query.py 的 twin-collapse 召回去重使用。
本脚本只读语料库（file:...?mode=ro），唯一写出目标是 twin_map.json。

兄弟规则（定稿，勿改）：
  1) 跨家族：id 十六进制片段(>=10位)互含 = 镜像。
     片段 = re.findall(r'[0-9a-f]{10,}', sid.lower())；互含 = A 的某片段出现在 B 的 id 中
     且 B 的某片段出现在 A 的 id 中。
  2) 同家族：标准化引语共享 >=3 条 且 最长共享引语 >=60 字。
     quotes 取每条 summary_json 的前 6 条（多段会话取各段并集），标准化 = 去空白与标点。
  3) kimi 家族上游 uuid 碰撞（同工作区不同任务共享 session uuid）：同家族必须过内容验证
     （即规则 2）；纯 id 判据只允许跨家族。
  防误并护栏（本脚本新增，不动上面的定稿判据本身）：
  - 工作区片段护栏：一个会话在对方家族命中多个 id 候选（kimi 同工作区共享 uuid 的场景）时，
    若恰好只有一个候选与它共享 >=1 条长度>=60 的标准化引语，按内容裁决救回该边；
    否则整组丢弃并计数。宁可漏并，不可错并——错并正是当前检索第一瓶颈的反面。

canonical 选择规则（确定性，写入本注释即为契约）：
  1) 簇内「引语条数最多者」（summary_json quotes 总条数，含各段）；
  2) 平票取非 legacy 通道；
  3) 再平票取 canonical_session_id 字典序最小。

输出 var/db/twin_map.json 的 schema（JSON 无法写注释，故 schema 同时落在 "_schema" 键）：
  {
    "_schema":   说明文字,
    "meta":      {generated_at, source_db, rule, canonical_rule, source_commit},
    "stats":     {n_sessions, n_clusters, max_cluster_size, n_mapped_sessions,
                  coverage, n_edges_mirror, n_edges_deepquote, n_edges_dropped_ambiguous,
                  n_edges_dropped_noquote},
    "clusters":  [{"canonical": sid, "members": [sid...升序], "size": n,
                   "kind": "mirror|deepquote|mixed",
                   "canonical_quotes": N, "member_quotes": {sid: N}}],
    "session_to_canonical": {sid: canonical}   // 仅收录簇大小>=2 的会话；单例会话不出现在映射里
  }

用法：
  python twin_map.py [--db PATH] [--out PATH] [--sample K]
  --sample K：随机抽 K 个簇打印双方 theme/引语数供人工抽验（默认 10，只打印不改数据）。
"""
import sqlite3, json, re, sys, time, argparse, subprocess
from collections import defaultdict
from itertools import combinations

DEF_DB = r"D:/ADLINK/数据分析/var/db/conversation_vector.sqlite"
DEF_OUT = r"D:/ADLINK/数据分析/var/db/twin_map.json"
HEX_FRAG = re.compile(r"[0-9a-f]{10,}")
NONWORD = re.compile(r"[\W_]+", re.UNICODE)

def load_sessions(db):
    """读全部 status='ok' 会话：sid -> {family, quotes(标准化集合), n_quotes_raw, themes}"""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    sessions = {}
    for sid, sj in con.execute(
            "SELECT canonical_session_id, summary_json FROM summaries WHERE status='ok' "
            "ORDER BY canonical_session_id"):
        s = json.loads(sj)
        qs = s.get("quotes") or []
        ent = sessions.setdefault(sid, {
            "family": family_of(sid), "n_quotes_raw": 0,
            "quotes": set(), "themes": []})
        ent["n_quotes_raw"] += len(qs)
        for q in qs[:6]:  # 定稿规则：每条 summary 取前 6 条
            nq = NONWORD.sub("", str(q))
            if nq:
                ent["quotes"].add(nq)
        ent["themes"].append(str(s.get("theme", ""))[:60])
    con.close()
    return sessions

def family_of(sid):
    """有效家族：legacy 聚合通道拆到原始通道（cs|legacy|cs/grok/x -> legacy/grok），
    避免把 legacy 下不同来源会话误判为同家族。"""
    parts = sid.split("|")
    if len(parts) >= 3 and parts[0] == "cs":
        if parts[1] == "legacy" and parts[2].startswith("cs/"):
            seg = parts[2].split("/")
            if len(seg) >= 2:
                return "legacy/" + seg[1]
        return parts[1]
    return sid.rsplit("|", 1)[0] if "|" in sid else sid

def mirror_edges(sessions):
    """跨家族 id 十六进制片段互含 -> 镜像边。返回 (edges, n_dropped, n_rescued)。
    edges: set of (sidA, sidB) with sidA < sidB。
    歧义处理（kimi 同工作区共享 uuid 场景）：一个会话在同一对方家族里命中多个候选时，
    若恰好只有一个候选与它共享 >=1 条长度>=60 的标准化引语，则内容裁决唯一胜者，救回该边；
    否则整组丢弃（宁漏勿错）。"""
    frag_index = defaultdict(set)  # 片段 -> {sid}
    for sid, ent in sessions.items():
        for f in set(HEX_FRAG.findall(sid.lower())):
            frag_index[f].add(sid)
    # 候选对：共享至少一个片段、且跨家族、且互含
    cand = {}
    for f, sids in frag_index.items():
        if len(sids) < 2:
            continue
        for a, b in combinations(sorted(sids), 2):
            if sessions[a]["family"] == sessions[b]["family"] or (a, b) in cand:
                continue
            fa, fb = frag_sets(a, sessions), frag_sets(b, sessions)
            ma = {x for x in fa if x in b}   # A 的片段落在 B 的 id 里
            mb = {x for x in fb if x in a}
            if ma and mb:
                cand[(a, b)] = (ma, mb)
    # 歧义分组：a 在 b 家族内的全部候选；b 在 a 家族内的全部候选
    grp_a = defaultdict(set)  # (sid, 对方家族) -> {候选 sid}
    for a, b in cand:
        grp_a[(a, sessions[b]["family"])].add(b)
        grp_a[(b, sessions[a]["family"])].add(a)
    edges, dropped, rescued = set(), 0, 0
    for (a, b) in sorted(cand):
        if grp_a[(a, sessions[b]["family"])] == {b} and grp_a[(b, sessions[a]["family"])] == {a}:
            edges.add((a, b))  # 双向都唯一，纯 id 即可定
            continue
        # 有歧义：内容裁决——双方共享引语最长>=60 字才可信
        shared = sessions[a]["quotes"] & sessions[b]["quotes"]
        if shared and max(len(q) for q in shared) >= 60:
            winner = True
            for c in grp_a[(a, sessions[b]["family"])] - {b}:
                sc = sessions[a]["quotes"] & sessions[c]["quotes"]
                if sc and max(len(q) for q in sc) >= 60:
                    winner = False  # 超过一个内容可信候选，仍歧义
                    break
            if winner:
                edges.add((a, b))
                rescued += 1
                continue
        dropped += 1
    return edges, dropped, rescued

def frag_sets(sid, sessions):
    return set(HEX_FRAG.findall(sid.lower()))

def deepquote_edges(sessions):
    """同家族标准化引语共享 >=3 条且最长 >=60 字 -> 深重合边。"""
    q_index = defaultdict(set)  # 标准化引语 -> {sid}
    for sid, ent in sessions.items():
        for q in ent["quotes"]:
            q_index[q].add(sid)
    co = defaultdict(int)  # (sidA, sidB) -> 共享引语条数
    longest = {}
    for q, sids in q_index.items():
        if len(sids) < 2 or len(q) < 20:  # 过短引语谁都有，不进共现统计
            continue
        for a, b in combinations(sorted(sids), 2):
            if sessions[a]["family"] == sessions[b]["family"]:
                co[(a, b)] += 1
                if len(q) > longest.get((a, b), 0):
                    longest[(a, b)] = len(q)
    return {(a, b) for (a, b), n in co.items()
            if n >= 3 and longest[(a, b)] >= 60}

class UF:
    def __init__(self):
        self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)

def pick_canonical(members, sessions):
    """确定性 canonical：引语条数最多 -> 非 legacy 优先 -> id 字典序最小。"""
    return sorted(members, key=lambda s: (
        -sessions[s]["n_quotes_raw"],
        1 if s.split("|")[1:2] == ["legacy"] else 0,
        s))[0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--sample", type=int, default=10, help="人工抽验打印簇数")
    args = ap.parse_args()

    t0 = time.time()
    sessions = load_sessions(args.db)
    print("会话 %d 个，加载 %.1fs" % (len(sessions), time.time() - t0), flush=True)

    mirror, drop_amb, rescued = mirror_edges(sessions)
    print("跨家族镜像边 %d（歧义丢弃 %d，引语裁决救回 %d）" % (len(mirror), drop_amb, rescued), flush=True)
    deep = deepquote_edges(sessions)
    print("同家族深重合边 %d" % len(deep), flush=True)

    uf = UF()
    for s in sessions:
        uf.find(s)
    kinds = {}
    for a, b in mirror | deep:
        uf.union(a, b)
        kinds[(a, b)] = "mirror" if (a, b) in mirror else "deepquote"
    comps = defaultdict(list)
    for s in sorted(sessions):
        comps[uf.find(s)].append(s)
    clusters = [m for m in comps.values() if len(m) >= 2]
    clusters.sort(key=lambda m: (-len(m), m[0]))

    out_clusters, s2c = [], {}
    for members in clusters:
        can = pick_canonical(members, sessions)
        for s in members:
            s2c[s] = can
        ks = {kinds.get((a, b)) for a, b in combinations(members, 2) if (a, b) in kinds}
        kind = "mixed" if len(ks) > 1 else (ks.pop() if ks else "mirror")
        out_clusters.append({"canonical": can, "members": members, "size": len(members),
                             "kind": kind,
                             "canonical_quotes": sessions[can]["n_quotes_raw"],
                             "member_quotes": {s: sessions[s]["n_quotes_raw"] for s in members}})

    n_mapped = len(s2c)
    stats = {
        "n_sessions": len(sessions),
        "n_clusters": len(clusters),
        "max_cluster_size": max((len(m) for m in clusters), default=0),
        "n_mapped_sessions": n_mapped,
        "coverage": round(n_mapped / len(sessions), 4) if sessions else 0.0,
        "n_edges_mirror": len(mirror),
        "n_edges_deepquote": len(deep),
        "n_edges_dropped_ambiguous": drop_amb,
        "n_edges_rescued_quote": rescued,
    }
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=args.db.rsplit("var/db", 1)[0],
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        commit = ""
    payload = {
        "_schema": "clusters[{canonical,members[],size,kind,canonical_quotes,member_quotes}]; "
                   "session_to_canonical 仅含簇>=2的会话; 判据与 canonical 规则见 twin_map.py 文件头",
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_db": args.db,
            "source_commit": commit,
            "rule": "跨家族: id hex片段(>=10)互含且片段在对方家族唯一; 同家族: 标准化引语共享>=3且最长>=60",
            "canonical_rule": "引语最多 -> 非legacy优先 -> sid字典序最小",
        },
        "stats": stats,
        "clusters": out_clusters,
        "session_to_canonical": dict(sorted(s2c.items())),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("写出 %s（%.1fs）" % (args.out, time.time() - t0))
    print("统计:", json.dumps(stats, ensure_ascii=False))

    # 人工抽验：打印前 K 个簇（按大小排）双方 theme
    if args.sample:
        print("\n=== 人工抽验 %d 个簇（theme 对比，人工判断是否同内容）===" % args.sample)
        for c in out_clusters[:args.sample]:
            print("-" * 70)
            print("簇 size=%d kind=%s canonical_quotes=%d" % (c["size"], c["kind"], c["canonical_quotes"]))
            for s in c["members"]:
                print("  [%s] %s" % (s, " || ".join(sessions[s]["themes"])))

if __name__ == "__main__":
    main()
