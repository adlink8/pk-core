"""Auto-split from unified_search.py — see facade for the public API."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import csv
import io
import time
from pathlib import Path
from typing import Any, Optional

import sys  # noqa: E402
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR

# search_vectors is used by semantic_search only, but harmless elsewhere.
from personal_knowledge.retrieval.search_vectors import search as _semantic_search, search_all as _semantic_search_all  # noqa: E402
import personal_knowledge.retrieval._constants as _C  # noqa: E402
from personal_knowledge.retrieval._constants import (  # noqa: E402
    DEFAULT_MEMORY_GRAPH_LIMIT, MAX_MEMORY_GRAPH_LIMIT,
    DEFAULT_RELATION_REVIEW_LIMIT, MAX_RELATION_REVIEW_LIMIT,
    RELATION_REVIEW_STATUSES, DEFAULT_DATA_LIMIT, MAX_DATA_LIMIT, MAX_EXPORT_LIMIT,
    DEFAULT_EVENT_FIELDS, EVENT_FIELD_SQL, AGGREGATE_GROUP_SQL,
    _KU_SLOTS, _RAW_SLOTS_DEFAULT, _KU_PORT,
    FALLBACK_POLICIES, DEFAULT_FALLBACK_POLICY,
    CONVERSATION_TURNS_COLLECTION, CANONICAL_MESSAGES_COLLECTION,
    _NON_DIALOGUE_PREFERRED_SOURCE,
)

from personal_knowledge.retrieval._db_utils import (  # noqa: E402
    _bounded_int, _split_csv, _normalize_event_fields, _event_from_clause, _event_filter_sql, _memory_layer_ready, _table_exists, _parse_metadata, _parse_json_list, _memory_row_to_dict,
)

def _merge_layer_ready() -> bool:
    """合并层是否已构建(merge_clusters 表存在且非空)。"""
    con = sqlite3.connect(_C.UNIFIED_DB)
    try:
        n = con.execute("SELECT COUNT(*) FROM merge_clusters").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    con.close()
    return n > 0


def _dedup_event_ids(event_ids: list[str]) -> tuple[list[str], dict[str, str]]:
    """按合并层折叠一批 event_id。

    返回 (kept_ids, dup_map):
      kept_ids: 去重后保留的代表 id 列表(保持输入顺序)
      dup_map:  {被折叠的成员id → 代表id}(仅含实际被折叠的;代表/独立点不入表)

    规则:若 event_id 是某簇的成员,用该簇代表点替换它;
          代表点或非合并表成员保持原样。多个成员属同一簇只留一个代表。
    """
    if not event_ids:
        return [], {}
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(event_ids))
    # 每个 event_id → 其所属簇的代表(若有)
    rows = con.execute(
        f"SELECT mm.event_id AS eid, mc.representative_id AS rep_id "
        f"FROM merge_members mm JOIN merge_clusters mc "
        f"ON mc.cluster_id = mm.cluster_id "
        f"WHERE mm.event_id IN ({placeholders})",
        event_ids,
    ).fetchall()
    con.close()
    eid_to_rep = {r["eid"]: r["rep_id"] for r in rows}

    kept: list[str] = []
    seen_reps: set[str] = set()
    dup_map: dict[str, str] = {}
    for eid in event_ids:
        rep = eid_to_rep.get(eid, eid)  # 不在合并表 → 自身即代表
        if rep in seen_reps:
            dup_map[eid] = rep
            continue
        seen_reps.add(rep)
        kept.append(eid if eid == rep else rep)
        if eid != rep:
            dup_map[eid] = rep
    return kept, dup_map


def merge_stats() -> dict:
    """返回合并层构建报告(merge_build_meta + 簇分布)。

    若合并层未构建,返回 {"available": False, "hint": ...}。
    """
    if not _merge_layer_ready():
        return {
            "available": False,
            "hint": "合并层未构建。运行: python -m personal_knowledge.application.graph.build_merge_layer",
        }
    con = sqlite3.connect(_C.UNIFIED_DB)
    con.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in con.execute(
        "SELECT key, value FROM merge_build_meta"
    )}
    by_level = {
        r["level"]: r["n"] for r in con.execute(
            "SELECT level, COUNT(*) AS n FROM merge_clusters GROUP BY level"
        )
    }
    top_l1 = [dict(r) for r in con.execute(
        "SELECT mc.cluster_id, mc.member_count, ue.title "
        "FROM merge_clusters mc JOIN unified_events ue "
        "ON ue.event_id = mc.representative_id "
        "WHERE mc.level='L1_duplicate' "
        "ORDER BY mc.member_count DESC LIMIT 5"
    )]
    top_l2 = [dict(r) for r in con.execute(
        "SELECT mc.cluster_id, mc.member_count, substr(mc.summary,1,60) AS summary "
        "FROM merge_clusters mc WHERE mc.level='L2_topic' "
        "ORDER BY mc.member_count DESC LIMIT 5"
    )]
    con.close()

    def num(k):
        try:
            return int(float(meta.get(k, 0)))
        except (ValueError, TypeError):
            return meta.get(k)

    return {
        "available": True,
        "n_input": num("n_input"),
        "l1_clusters": by_level.get("L1_duplicate", 0),
        "l1_events": num("l1_events"),
        "l2_clusters": by_level.get("L2_topic", 0),
        "l2_events": num("l2_events"),
        "structural_clusters": num("structural_clusters"),
        "structural_events": num("structural_events"),
        "effective_events": num("effective_events"),
        "compression": float(meta.get("compression", 0)),
        "thresholds": {
            "l1_cos": float(meta.get("threshold_l1_cos", 0)),
            "l1_jac": float(meta.get("threshold_l1_jac", 0)),
            "l1_sem_jac": float(meta.get("threshold_l1_sem_jac", 0)),
            "l1_distinct": float(meta.get("threshold_l1_distinct", 0)),
            "l2_cos": float(meta.get("threshold_l2_cos", 0)),
        },
        "top_l1_clusters": top_l1,
        "top_l2_clusters": top_l2,
    }


# === 聚类 / 去重(对向量库二次加工)======================================

def cluster(
    source: Optional[str] = None,
    threshold: float = 0.92,
    min_cluster_size: int = 2,
    limit: Optional[int] = None,
) -> dict:
    """对向量库做相似度聚类,把高度相似的事件归成簇。

    本质是"向量库的二次加工":从 chroma 拉出全部 embedding,算两两余弦相似度,
    相似度 >= threshold 的连成一张图,连通分量即一个簇。

    source:           过滤数据源(None=全库)
    threshold:        相似度阈值(0-1,越大越严格;0.92 经验上能抓"几乎重复")
    min_cluster_size: 只保留 size >= N 的簇(size=1 的孤立点单独统计,不展开)
    limit:            最多处理多少条(默认全部,调试时可设小值)

    返回 dict:
        n_input:        输入事件数
        n_kept:         保留(代表)事件数 = 簇数 + 孤立点数
        n_merged:       被合并掉的事件数 = n_input - n_kept
        n_clusters:     簇数(size>=min_cluster_size)
        n_singletons:   孤立点数(自成一类)
        compression:    压缩率 = n_merged / n_input
        threshold/min_cluster_size: 回显参数
        clusters:       [{id, size, representative_id, representative_title,
                         member_ids: [...], mean_similarity}, ...](按 size 降序)

    依赖:numpy(余弦相似度矩阵)。7700 条全量约 230MB 内存,可接受。
    """
    import numpy as np
    from personal_knowledge.core.chroma_client import ChromaClient

    client = ChromaClient()
    coll = client.get_or_create_collection("personal_events")

    # 分批拉全部 embedding + 元数据(chroma 单次 get 有上限,分批稳妥)
    BATCH = 2000
    ids: list[str] = []
    embs: list[list[float]] = []
    titles: dict[str, str] = {}
    offset = 0
    where = {"source": source} if source else None
    while True:
        batch = coll.get(
            where=where, limit=BATCH, offset=offset,
            include=["embeddings", "documents", "metadatas"],
        )
        b_ids = batch.get("ids", [])
        if not b_ids:
            break
        ids.extend(b_ids)
        embs.extend(batch.get("embeddings", []))
        for i, mid in enumerate(b_ids):
            meta = (batch.get("metadatas") or [None] * len(b_ids))[i] or {}
            titles[mid] = meta.get("title") or (batch.get("documents") or [""])[i][:60]
        offset += len(b_ids)
        if limit and len(ids) >= limit:
            ids = ids[:limit]
            embs = embs[:limit]
            break

    n = len(ids)
    if n == 0:
        return {
            "n_input": 0, "n_kept": 0, "n_merged": 0,
            "n_clusters": 0, "n_singletons": 0, "compression": 0.0,
            "threshold": threshold, "min_cluster_size": min_cluster_size,
            "clusters": [],
        }

    # 余弦相似度矩阵(embedding 已是 bge-m3 归一化的,但保险起见再归一)
    mat = np.asarray(embs, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    sim = mat @ mat.T  # (n, n) 余弦相似度

    # 连通分量:相似度 >= threshold 的点互相连通 → 同一簇
    adj = sim >= threshold
    visited = [False] * n
    groups: list[list[int]] = []
    for i in range(n):
        if visited[i]:
            continue
        # BFS 找连通分量
        stack = [i]
        visited[i] = True
        comp = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            neighbors = np.nonzero(adj[cur])[0]
            for nb in neighbors:
                if not visited[nb]:
                    visited[nb] = True
                    stack.append(int(nb))
        groups.append(comp)

    # 拆成 clusters(>=min) 和 singletons
    clusters_raw = [g for g in groups if len(g) >= min_cluster_size]
    singletons = [g for g in groups if len(g) < min_cluster_size]

    out_clusters = []
    for ci, comp in enumerate(
        sorted(clusters_raw, key=len, reverse=True)
    ):
        sub = sim[np.ix_(comp, comp)]
        mean_sim = float((sub.sum() - len(comp)) / (len(comp) * (len(comp) - 1)))
        # 代表点选簇内平均相似度最高的(最"居中")
        centrality = (sub.sum(axis=1) - 1) / (len(comp) - 1)
        rep_idx = int(np.argmax(centrality))
        rep_id = ids[comp[rep_idx]]
        out_clusters.append({
            "id": ci,
            "size": len(comp),
            "representative_id": rep_id,
            "representative_title": titles.get(rep_id, "")[:60],
            "mean_similarity": round(mean_sim, 4),
            "member_ids": [ids[j] for j in comp],
        })

    n_kept = len(clusters_raw) + len(singletons)
    return {
        "n_input": n,
        "n_kept": n_kept,
        "n_merged": n - n_kept,
        "n_clusters": len(clusters_raw),
        "n_singletons": len(singletons),
        "compression": round((n - n_kept) / n, 4),
        "threshold": threshold,
        "min_cluster_size": min_cluster_size,
        "clusters": out_clusters,
    }


# === CLI 入口 ===

