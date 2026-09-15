"""分层合并层构建脚本 —— 把相似事件折叠成簇,原始数据零损失。

解决三类数据质量问题:
1. L1 真重复:同一对话/文件被记录多次 → 折叠为1条
2. L2 同主题:同一项目的多次操作/同一问题反复问 → 聚成簇留代表+摘要
3. L3 保留:独立事件不动;结构性相似的超大簇(如所有 SKILL.md)保护性不合并

核心承诺:合并 = 折叠,不是删除。原始 9 张统合表一字不改,新建 2 张叠加表记录
合并关系,任何时刻 JOIN 回去都能拿到每条原始事件的完整内容。

=== 算法 ================================================================

L1 真重复检测(三重门槛,防止结构相似被误判):
  1) 余弦相似度 >= L1_COS 的边连图(连通分量)
  2) 候选簇内成员两两做 4-gram Jaccard,重合度 >= L1_JAC 才确认
  3) 内容区分度:把每个成员的语义骨架(去数字/路径/UUID)去重,
     唯一值比例 < L1_DISTINCT_RATIO 才确认 —— 这是抓"结构相似假阳性"
     的关键。例:N 个不同的 umath-*.csv 文件路径,余弦和 Jaccard 都高
     (公共前缀长),但骨架唯一值=N(文件名都是字母),判结构相似降级;
     而 N 条重复的"文档产物..."骨架唯一值=1,判真重复保留。
  → 不满足任一门槛的降级进 L2 重新判定
  代表点选时间最早的

L2 同主题聚类:
  1) 余弦 L2_COS_LO ~ L1_COS 的边连图
  2) 超大簇保护:size > L2_MAX_SIZE 判为"结构性相似",不合并,成员独立
  3) 小簇确认合并,摘要 = 成员 title 去重 + 代表 content 前 200 字

L3:不进合并表,保持原样(向量库检索/dashboards 仍按原始事件工作)

=== 产出表 =============================================================

  merge_clusters (簇主表)
    cluster_id TEXT PRIMARY KEY      -- 'L1_<8位hash>' / 'L2_<8位hash>'
    level TEXT                       -- 'L1_duplicate' / 'L2_topic'
    representative_id TEXT           -- 代表事件 event_id
    member_count INTEGER
    summary TEXT                     -- L2 摘要;L1 为空
    mean_similarity REAL
    created_at TEXT

  merge_members (成员明细,完整可追溯)
    cluster_id TEXT
    event_id TEXT
    is_representative INTEGER        -- 1=代表点,0=成员
    role TEXT                        -- 'rep'/'duplicate'/'topic_member'
    PRIMARY KEY(cluster_id, event_id)

  merge_build_meta (构建元数据,幂等校验用)
    key TEXT PRIMARY KEY, value TEXT

=== 运行 ===============================================================

  python integration\\scripts\\build_merge_layer.py
  python integration\\scripts\\build_merge_layer.py --threshold-l1 0.97  # 调阈值

幂等:重复运行先 DROP IF EXISTS 再建,结果一致。
依赖:numpy(余弦矩阵),chroma_client(读 embedding)。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"


# === 可调阈值(经验值,基于 bge-small-zh 512 维 + 实测)===
L1_COS = 0.97       # L1 余弦门槛(真重复通常 >=0.98)
L1_JAC = 0.80       # L1 4-gram Jaccard 门槛(原始文本高度重合)
L1_SEM_JAC = 0.75   # L1 语义骨架 Jaccard 门槛(去数字/路径后,防结构相似假阳性)
L1_DISTINCT_RATIO = 0.5  # L1 内容区分度门槛:语义骨架唯一值/成员数 < 此值才确认真重复
L2_COS_LO = 0.88    # L2 余弦下界
L2_MAX_SIZE = 50    # 超大簇保护:超过此 size 不合并(判为结构相似)
MIN_LEN = 10        # content_rich 最短长度(短于此跳过,无语义价值)


# === 文本工具 ===========================================================

def normalize(t: str | None) -> str:
    return " ".join((t or "").strip().lower().split())


def ngrams(text: str, n: int = 4) -> set[str]:
    """字符级 n-gram 集合(去空白后)。短文本降级到整串。"""
    s = re.sub(r"\s+", "", (text or ""))
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


# 结构噪声:数字、UUID、Windows/Unix 路径、文件扩展名 —— 这些在"结构相似"数据
# (测试数据文件、配置、路径清单)里大量重复,会污染 n-gram Jaccard。
# semantic_text() 把它们替换成占位符,只保留"语义骨架",用于 L1 二次校验。
_NUM_RE = re.compile(r"\b\d[\d.,eE+-]*\b")          # 数字(含小数/科学计数)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_WINPATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*")
_UNIXPATH_RE = re.compile(r"(?<![A-Za-z0-9])/[a-zA-Z][\w\-/.]*")


def semantic_text(text: str | None) -> str:
    """把结构噪声替换成占位符,保留语义骨架。"""
    s = text or ""
    s = _UUID_RE.sub(" UUID ", s)
    s = _WINPATH_RE.sub(" PATH ", s)
    s = _UNIXPATH_RE.sub(" PATH ", s)
    s = _NUM_RE.sub(" NUM ", s)
    return s


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def distinct_ratio(texts: list[str]) -> float:
    """内容区分度:语义骨架去重后的唯一值 / 成员数。

    用于区分"真重复"和"结构相似":
      真重复(N 条内容相同,仅 UUID/时间戳/路径不同)→ semantic_text 把
        这些噪声替换成占位符后骨架塌缩,唯一值 ≈ 1,ratio ≈ 0。
      结构相似(N 条同模板但实质不同,如不同文件名的文件路径清单)→
        骨架里的字母级差异(文件名/函数名)保留,唯一值 ≈ N,ratio ≈ 1。
    """
    if not texts:
        return 0.0
    skels = {semantic_text(t) for t in texts}
    return len(skels) / len(texts)


# === 数据加载 ===========================================================

def load_events(db: Path = UNIFIED_DB) -> list[dict]:
    """加载可参与合并的事件(有 content_rich >= MIN_LEN)。

    同时拉取向量库里的 embedding(只对在向量库里的事件做合并)。
    返回 list[dict],每条含 event_id/source/title/event_time/content_rich/emb。
    """
    from personal_knowledge.core.chroma_client import ChromaClient

    # 先从 sqlite 拉元数据 + 文本
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in con.execute(
            "SELECT ue.event_id, ue.source, ue.service, ue.event_time, ue.title, "
            "r.content_rich FROM unified_events ue "
            "JOIN unified_events_rich r ON r.event_id = ue.event_id "
            "WHERE length(r.content_rich) >= ?",
            (MIN_LEN,),
        )
    ]
    con.close()

    # 从 chroma 拉 embedding,对齐 event_id
    client = ChromaClient()
    coll = client.get_or_create_collection("personal_events")
    # 向量库里存的是 title + content,但 id 对齐 event_id
    BATCH = 2000
    emb_map: dict[str, list[float]] = {}
    offset = 0
    while True:
        batch = coll.get(limit=BATCH, offset=offset, include=["embeddings"])
        ids = batch.get("ids", [])
        if not ids:
            break
        for mid, emb in zip(ids, batch.get("embeddings", [])):
            emb_map[mid] = emb
        offset += len(ids)

    # 只保留既有文本又有 embedding 的事件
    out = []
    for r in rows:
        emb = emb_map.get(r["event_id"])
        if emb is not None:
            r["emb"] = emb
            out.append(r)
    return out


# === 连通分量(相似度阈值切图)==========================================

def connected_components(adj: np.ndarray, n: int) -> list[list[int]]:
    """BFS 找连通分量。adj 是 (n,n) bool 矩阵(adj[i,j]=True 表示 i,j 相似。"""
    visited = [False] * n
    comps = []
    for i in range(n):
        if visited[i]:
            continue
        stack = [i]
        visited[i] = True
        comp = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in np.nonzero(adj[cur])[0]:
                if not visited[nb]:
                    visited[nb] = True
                    stack.append(int(nb))
        comps.append(comp)
    return comps


# === L1: 真重复检测 =====================================================

def detect_l1(events: list[dict], sim: np.ndarray) -> tuple[list[list[int]], set[int]]:
    """L1 真重复检测:余弦 >= L1_COS 且三重校验全过。

    三重门槛防止结构相似假阳性:
      1) 原始文本 4-gram Jaccard >= L1_JAC
      2) 语义骨架(去数字/路径/UUID)Jaccard >= L1_SEM_JAC
      3) 内容区分度 distinct_ratio < L1_DISTINCT_RATIO
         (骨架唯一值少 = 成员实质内容相同;唯一值多 = 结构同质但内容各异)
    全部满足才确认 L1;否则降级到 L2 重判。

    例:15 个 umath-validation-set-*.csv 文件路径,公共前缀长 → 余弦/Jaccard
       双双过线,但骨架唯一值=15(文件名都是字母)→ 第3关拦下,降级。
       对比:26 条重复的"文档产物..."→ 骨架唯一值=1 → 三关全过,真重复。

    返回 (l1_clusters, downgraded_idx)。
    """
    n = len(events)
    adj = sim >= L1_COS
    np.fill_diagonal(adj, False)
    comps = connected_components(adj, n)

    l1_clusters = []
    downgraded = set()
    for comp in comps:
        if len(comp) < 2:
            continue
        texts = [events[i]["content_rich"] for i in comp]
        grams = [ngrams(t) for t in texts]
        sem_grams = [ngrams(semantic_text(t)) for t in texts]

        # 关卡 1+2: 两两 Jaccard 双校验
        all_pass = True
        for k in range(len(comp)):
            best_raw = max(
                jaccard(grams[k], grams[j]) for j in range(len(comp)) if j != k
            )
            best_sem = max(
                jaccard(sem_grams[k], sem_grams[j]) for j in range(len(comp)) if j != k
            )
            if best_raw < L1_JAC or best_sem < L1_SEM_JAC:
                all_pass = False
                break

        # 关卡 3: 内容区分度(抓"公共前缀长 + 字母级差异"的结构相似)
        if all_pass:
            ratio = distinct_ratio(texts)
            if ratio >= L1_DISTINCT_RATIO:
                all_pass = False  # 骨架各不相同 → 结构相似,降级

        if all_pass:
            l1_clusters.append(comp)
        else:
            downgraded.update(comp)
    return l1_clusters, downgraded


# === L2: 同主题聚类 =====================================================

def detect_l2(
    events: list[dict], sim: np.ndarray, exclude: set[int]
) -> tuple[list[list[int]], list[list[int]]]:
    """L2 同主题:余弦 L2_COS_LO~L1_COS 连通分量,应用超大簇保护。

    exclude: 已被 L1 处理的索引(跳过)。
    返回 (l2_clusters, structural_clusters)。
    structural_clusters 是 size>L2_MAX_SIZE 的"结构相似"簇(成员保持独立,不入合并表)。
    """
    n = len(events)
    # L2 的边:在 [L2_COS_LO, L1_COS) 区间(不含 L1,因为 L1 已处理)
    # 但降级的点可能两两 >=L1_COS 却没过 Jaccard,它们之间的边也要算
    # 简化:L2 边 = sim >= L2_COS_LO(包含 L1 区间,但 L1 已处理的点被 exclude)
    adj = sim >= L2_COS_LO
    np.fill_diagonal(adj, False)
    comps = connected_components(adj, n)

    l2_clusters = []
    structural = []
    for comp in comps:
        # 过滤掉被 exclude 的点
        members = [i for i in comp if i not in exclude]
        if len(members) < 2:
            continue
        if len(members) > L2_MAX_SIZE:
            structural.append(members)  # 超大簇保护
        else:
            l2_clusters.append(members)
    return l2_clusters, structural


# === 代表点选择 + 摘要 ==================================================

def pick_representative(events: list[dict], comp: list[int], sim: np.ndarray) -> int:
    """选代表点:L1 选时间最早的;L2 选簇内平均相似度最高的(最居中)。"""
    if len(comp) == 1:
        return comp[0]
    # 优先:有真实时间的事件,选最早的(去重时保留首次出现)
    # L2 用居中度
    sub = sim[np.ix_(comp, comp)]
    centrality = (sub.sum(axis=1) - 1) / (len(comp) - 1)
    rep_local = int(np.argmax(centrality))
    return comp[rep_local]


def pick_l1_representative(events: list[dict], comp: list[int]) -> int:
    """L1 代表点:时间最早的(保留首次出现)。时间相同取索引最小。"""
    best_i = comp[0]
    best_t = events[best_i].get("event_time") or "9999"
    for idx in comp[1:]:
        t = events[idx].get("event_time") or "9999"
        if t < best_t:
            best_t = t
            best_i = idx
    return best_i


def make_summary(events: list[dict], comp: list[int]) -> str:
    """L2 摘要:成员 title 去重拼接(最多5个)+ 代表 content 前 200 字。"""
    titles = []
    seen = set()
    for idx in comp:
        t = (events[idx].get("title") or "").strip()
        if t and t not in seen:
            seen.add(t)
            titles.append(t)
    title_part = " | ".join(titles[:5])
    if len(titles) > 5:
        title_part += f" ...(+{len(titles)-5})"
    return title_part


# === 落库 ===============================================================

def write_tables(
    db: Path,
    l1_clusters: list[list[int]],
    l2_clusters: list[list[int]],
    events: list[dict],
    sim: np.ndarray,
    report: dict,
) -> None:
    """幂等写入 2 张合并表 + meta。先 DROP IF EXISTS。"""
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS merge_clusters;
        DROP TABLE IF EXISTS merge_members;
        DROP TABLE IF EXISTS merge_build_meta;

        CREATE TABLE merge_clusters (
            cluster_id TEXT PRIMARY KEY,
            level TEXT NOT NULL,
            representative_id TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            summary TEXT,
            mean_similarity REAL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE merge_members (
            cluster_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            is_representative INTEGER NOT NULL,
            role TEXT NOT NULL,
            PRIMARY KEY (cluster_id, event_id)
        );
        CREATE TABLE merge_build_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX idx_merge_members_eid ON merge_members(event_id);
        CREATE INDEX idx_merge_clusters_rep ON merge_clusters(representative_id);
        """
    )

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    cluster_rows = []
    member_rows = []

    def cluster_id(level: str, idx: int) -> str:
        h = hashlib.md5(f"{level}_{idx}_{now}".encode()).hexdigest()[:8]
        return f"{level}_{h}"

    # L1
    for i, comp in enumerate(
        sorted(l1_clusters, key=len, reverse=True)
    ):
        rep = pick_l1_representative(events, comp)
        sub = sim[np.ix_(comp, comp)]
        mean_sim = float((sub.sum() - len(comp)) / (len(comp) * (len(comp) - 1))) if len(comp) > 1 else 1.0
        cid = cluster_id("L1", i)
        cluster_rows.append((cid, "L1_duplicate", events[rep]["event_id"], len(comp), None, round(mean_sim, 4), now))
        for idx in comp:
            member_rows.append((cid, events[idx]["event_id"], 1 if idx == rep else 0, "rep" if idx == rep else "duplicate"))

    # L2
    for i, comp in enumerate(
        sorted(l2_clusters, key=len, reverse=True)
    ):
        rep = pick_representative(events, comp, sim)
        sub = sim[np.ix_(comp, comp)]
        mean_sim = float((sub.sum() - len(comp)) / (len(comp) * (len(comp) - 1))) if len(comp) > 1 else 0.0
        summary = make_summary(events, comp)
        cid = cluster_id("L2", i)
        cluster_rows.append((cid, "L2_topic", events[rep]["event_id"], len(comp), summary, round(mean_sim, 4), now))
        for idx in comp:
            member_rows.append((cid, events[idx]["event_id"], 1 if idx == rep else 0, "rep" if idx == rep else "topic_member"))

    cur.executemany(
        "INSERT INTO merge_clusters VALUES (?,?,?,?,?,?,?)", cluster_rows
    )
    cur.executemany(
        "INSERT INTO merge_members VALUES (?,?,?,?)", member_rows
    )

    # meta
    for k, v in report.items():
        cur.execute(
            "INSERT INTO merge_build_meta VALUES (?,?)", (k, str(v))
        )

    con.commit()
    con.close()


# === 主流程 ==============================================================

def build(
    threshold_l1_cos: float = L1_COS,
    threshold_l1_jac: float = L1_JAC,
    threshold_l1_sem_jac: float = L1_SEM_JAC,
    threshold_l1_distinct: float = L1_DISTINCT_RATIO,
    threshold_l2_cos: float = L2_COS_LO,
    l2_max_size: int = L2_MAX_SIZE,
    db: Path = UNIFIED_DB,
) -> dict:
    global L1_COS, L1_JAC, L1_SEM_JAC, L1_DISTINCT_RATIO, L2_COS_LO, L2_MAX_SIZE
    L1_COS = threshold_l1_cos
    L1_JAC = threshold_l1_jac
    L1_SEM_JAC = threshold_l1_sem_jac
    L1_DISTINCT_RATIO = threshold_l1_distinct
    L2_COS_LO = threshold_l2_cos
    L2_MAX_SIZE = l2_max_size

    t0 = time.time()
    print("[1/4] 加载事件 + embedding...")
    events = load_events(db)
    n = len(events)
    print(f"    可合并事件: {n} (有 content_rich>= {MIN_LEN} 且在向量库中)")

    print("[2/4] 计算余弦相似度矩阵...")
    mat = np.asarray([e["emb"] for e in events], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    sim = mat @ mat.T
    print(f"    矩阵: {sim.shape}, 内存约 {sim.nbytes / 1024 / 1024:.0f}MB")

    print("[3/4] 分层检测...")
    l1_clusters, downgraded = detect_l1(events, sim)
    l1_event_count = sum(len(c) for c in l1_clusters)
    print(f"    L1 真重复: {len(l1_clusters)} 簇, 涉及 {l1_event_count} 条")

    # L1 处理过的索引都 exclude
    l1_idx = set()
    for c in l1_clusters:
        l1_idx.update(c)
    l2_clusters, structural = detect_l2(events, sim, exclude=l1_idx)
    l2_event_count = sum(len(c) for c in l2_clusters)
    struct_event_count = sum(len(c) for c in structural)
    print(f"    L2 同主题: {len(l2_clusters)} 簇, 涉及 {l2_event_count} 条")
    print(f"    L3 超大簇保护: {len(structural)} 个结构相似簇, {struct_event_count} 条保持独立")

    print("[4/4] 写入叠加表...")
    merged_events = l1_event_count + l2_event_count
    kept_l1 = len(l1_clusters)  # L1 每簇留1
    kept_l2 = len(l2_clusters)  # L2 每簇留1代表
    # L1 每簇省 (size-1),L2 每簇省 (size-1)
    saved = merged_events - kept_l1 - kept_l2
    # 加上结构保护没动的 + 完全独立的
    solo = n - merged_events - struct_event_count

    report = {
        "n_input": n,
        "l1_clusters": len(l1_clusters),
        "l1_events": l1_event_count,
        "l1_representatives": kept_l1,
        "l2_clusters": len(l2_clusters),
        "l2_events": l2_event_count,
        "l2_representatives": kept_l2,
        "structural_clusters": len(structural),
        "structural_events": struct_event_count,
        "solo_events": solo,
        "merged_events": merged_events,
        "saved_events": saved,
        "effective_events": n - saved,  # 去重后等效事件数
        "compression": round(saved / n, 4) if n else 0.0,
        "threshold_l1_cos": L1_COS,
        "threshold_l1_jac": L1_JAC,
        "threshold_l1_sem_jac": L1_SEM_JAC,
        "threshold_l1_distinct": L1_DISTINCT_RATIO,
        "threshold_l2_cos": L2_COS_LO,
        "l2_max_size": L2_MAX_SIZE,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    write_tables(db, l1_clusters, l2_clusters, events, sim, report)
    print(f"    写入 merge_clusters ({len(l1_clusters)+len(l2_clusters)} 行) + "
          f"merge_members ({merged_events} 行)")

    print()
    print("=" * 56)
    print("=== 合并层构建报告 ===")
    print(f"输入事件: {n:,}")
    print(f"L1 真重复:  {l1_event_count:>6,} 条 → {kept_l1} 代表  (省 {l1_event_count - kept_l1:,})")
    print(f"L2 同主题:  {l2_event_count:>6,} 条 → {kept_l2} 簇    (省 {l2_event_count - kept_l2:,})")
    print(f"L3 结构保护:{struct_event_count:>6,} 条({len(structural)} 超大簇保持独立)")
    print(f"L3 保留原样:{solo:>6,} 条")
    print("-" * 40)
    print(f"净压缩率: {report['compression']:.1%} ({n:,} → {report['effective_events']:,})")
    print(f"耗时: {report['elapsed_sec']}s | 阈值: L1={L1_COS}/J{L1_JAC}/SJ{L1_SEM_JAC}/DR{L1_DISTINCT_RATIO} L2={L2_COS_LO} max{L2_MAX_SIZE}")
    print("成员 100% 可追溯 (JOIN merge_members)")
    print("=" * 56)
    return report


def main() -> None:
    p = argparse.ArgumentParser(description="构建分层合并层")
    p.add_argument("--threshold-l1", type=float, default=L1_COS, help=f"L1 余弦门槛(默认 {L1_COS})")
    p.add_argument("--l1-jac", type=float, default=L1_JAC, help=f"L1 原始 Jaccard 门槛(默认 {L1_JAC})")
    p.add_argument("--l1-sem-jac", type=float, default=L1_SEM_JAC, help=f"L1 语义骨架 Jaccard 门槛(默认 {L1_SEM_JAC})")
    p.add_argument("--l1-distinct", type=float, default=L1_DISTINCT_RATIO, help=f"L1 内容区分度门槛(默认 {L1_DISTINCT_RATIO},骨架唯一值/成员数<此值才确认真重复)")
    p.add_argument("--threshold-l2", type=float, default=L2_COS_LO, help=f"L2 余弦下界(默认 {L2_COS_LO})")
    p.add_argument("--l2-max-size", type=int, default=L2_MAX_SIZE, help=f"L2 超大簇保护上限(默认 {L2_MAX_SIZE})")
    args = p.parse_args()

    print("=" * 56)
    print("分层合并层构建 build_merge_layer.py")
    print("  原则: 合并=折叠, 原始数据零损失, 可回滚")
    print("=" * 56)
    build(
        threshold_l1_cos=args.threshold_l1,
        threshold_l1_jac=args.l1_jac,
        threshold_l1_sem_jac=args.l1_sem_jac,
        threshold_l1_distinct=args.l1_distinct,
        threshold_l2_cos=args.threshold_l2,
        l2_max_size=args.l2_max_size,
    )


if __name__ == "__main__":
    main()
