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
from personal_knowledge.retrieval.google_assertions import get_google_structure_status  # noqa: E402
from personal_knowledge.retrieval.merge_cluster import _merge_layer_ready, _dedup_event_ids  # noqa: E402
from personal_knowledge.retrieval.serving import ServingSnapshotResolver, member_version  # noqa: E402
from personal_knowledge.retrieval.relevance import annotate_candidate_support  # noqa: E402
from personal_knowledge.retrieval.layers.base import SearchState  # noqa: E402
from personal_knowledge.core.canonical_visibility import (  # noqa: E402
    canonical_projection_predicate,
)

def search_semantic(
    query: str,
    top_k: int = 5,
    source: Optional[str] = None,
    dedup: bool = False,
    include_turns: bool = True,
) -> list[dict]:
    """语义检索:自然语言召回用户历史事件。

    Wave 7 起默认跨 collection 检索:personal_events(单条事件) +
    conversation_turns(turn 叙述,含因果链)。适合"用户做过什么/怎么做的"类查询。

    query: 自然语言(如"PPT 排版怎么做")
    top_k: 返回条数
    source: 过滤数据源("Google"/"GPT"/"Agent"),None=全源
    dedup:  True=按合并层折叠重复命中(L1 真重复/L2 同主题只留代表),
            返回结果里多一个 merged_count 字段表示该代表背后折叠了几条。
            折叠后实际条数可能少于 top_k。
            注意:dedup 只作用于 personal_events(conversation_turns 不参与合并层折叠)。
    include_turns: True=同时搜 conversation_turns turn 叙述(Wave 7 默认);
                   False=只搜 personal_events(旧行为)。collection 不存在时自动降级。
    返回: list[dict],按相似度降序,字段:
        event_id, source, category_v2, event_type, service,
        event_time, month, title, content, score[, merged_count],
        collection, retrieval_unit, rank_reason
        turn 叙述额外带: session_id, turn_id, turn_no, main_topic
    """
    if not query or not query.strip():
        return []
    # dedup 模式多召回一些,折叠后仍有足够结果
    fetch_k = top_k * 3 if dedup else top_k
    # Wave 7: 跨 collection 检索(include_turns=True 时合并 turn 叙述)
    if include_turns:
        results = _semantic_search_all(query, top_k=fetch_k, source=source)
    else:
        results = _semantic_search(query, top_k=fetch_k, source=source)
    if not dedup or not results:
        return results[:top_k]
    if not _merge_layer_ready():
        return results[:top_k]

    # 按合并层折叠:同簇只留首个(分数最高)命中,附 merged_count
    kept_ids, dup_map = _dedup_event_ids([r["event_id"] for r in results])
    rep_first_idx: dict[str, int] = {}
    for i, r in enumerate(results):
        rep = dup_map.get(r["event_id"], r["event_id"])
        if rep not in rep_first_idx:
            rep_first_idx[rep] = i

    # 统计每个代表折叠了多少条命中
    con = sqlite3.connect(_C.UNIFIED_DB)
    rep_counts: dict[str, int] = {}
    for rep in rep_first_idx:
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM merge_members WHERE cluster_id IN "
                "(SELECT cluster_id FROM merge_members WHERE event_id=?)",
                (rep,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        # 独立事件(不在任何簇)代表它自己,计 1
        rep_counts[rep] = n if n > 0 else 1
    con.close()

    out = []
    for rep, idx in sorted(rep_first_idx.items(), key=lambda x: x[1]):
        r = dict(results[idx])
        r["merged_count"] = rep_counts.get(rep, 1)
        out.append(r)
        if len(out) >= top_k:
            break
    return out


# --- Knowledge unit hybrid retrieval (Phase 14 Wave 4 / Phase 15 Wave 2) ---

# 混合策略：知识层贡献结构化语义结果；raw 层可配置:
#   legacy  — KU 后补全量 personal_events（旧行为）
#   layered — KU → canonical message 片段/词检索(dialogue) → conversation_turns
#             → personal_events(Google) → 可选 legacy_pad
# Phase 15 W4: message 级 dialogue 补洞（frozen gold snippet R@8=1.0 on canonical）。
_KU_SLOTS = 3
_RAW_SLOTS_DEFAULT = 4
_KU_PORT = 8001

FALLBACK_POLICIES = ("legacy", "layered")
DEFAULT_FALLBACK_POLICY = "layered"
CONVERSATION_TURNS_COLLECTION = "conversation_turns"
CANONICAL_MESSAGES_COLLECTION = "canonical_messages"
_NON_DIALOGUE_PREFERRED_SOURCE = "Google"


def _resolve_fallback_policy(fallback_policy: str | None = None) -> str:
    """Resolve hybrid fallback policy from arg or env PERSONAL_DATA_FALLBACK_POLICY."""
    if fallback_policy is None:
        raw = os.environ.get("PERSONAL_DATA_FALLBACK_POLICY", DEFAULT_FALLBACK_POLICY)
    else:
        raw = fallback_policy
    policy = (raw or DEFAULT_FALLBACK_POLICY).strip().lower()
    if policy not in FALLBACK_POLICIES:
        return DEFAULT_FALLBACK_POLICY
    return policy


def _resolve_allow_legacy_pad(allow_legacy_pad: bool | None = None) -> bool:
    """Whether layered mode may pad with non-Google personal_events when still short.

    Default True (transition safety; see retrieval-ssot.md § legacy_pad rollout).
    Env: PERSONAL_DATA_ALLOW_LEGACY_PAD=0|1|true|false.
    """
    if allow_legacy_pad is not None:
        return bool(allow_legacy_pad)
    env = os.environ.get("PERSONAL_DATA_ALLOW_LEGACY_PAD")
    if env is None or not str(env).strip():
        return True
    return str(env).strip().lower() in ("1", "true", "yes", "on")


def _empty_layer_telemetry(name: str) -> dict[str, Any]:
    return {"name": name, "attempted": False, "hits": 0, "latency_ms": 0.0}


def _finalize_search_telemetry(
    layers: list[dict[str, Any]],
    *,
    pad_allowed: bool,
    t0: float,
) -> dict[str, Any]:
    """Build response telemetry: per-layer hit/attempt/latency + pad usage."""
    first = None
    pad_used = False
    for layer in layers:
        if first is None and int(layer.get("hits") or 0) > 0:
            first = layer.get("name")
        if layer.get("name") == "legacy_pad" and int(layer.get("hits") or 0) > 0:
            pad_used = True
    return {
        "layers": layers,
        "first_contributing_layer": first,
        "pad_allowed": bool(pad_allowed),
        "pad_used": pad_used,
        "total_latency_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


def _raw_event_item(
    ev: dict,
    *,
    retrieval_unit: str,
    collection: str,
    rank_reason: str,
) -> dict:
    """Normalize a vector search hit into the hybrid result schema."""
    title = ev.get("title") or ev.get("main_topic") or ""
    return {
        "unit_id": ev.get("event_id", ""),
        "subject": str(title)[:80],
        "answer": (ev.get("content") or "")[:300],
        "score": ev.get("score", 0),
        "lifecycle": "current",
        "confidence": 0,
        "source_message_ref": "",
        "collection": collection,
        "retrieval_unit": retrieval_unit,
        "rank_reason": rank_reason,
        "event_id": ev.get("event_id", ""),
        "source": ev.get("source", ""),
        "event_time": ev.get("event_time", ""),
    }


def _search_personal_events_filtered(
    query: str,
    top_k: int,
    source: Optional[str],
) -> list[dict]:
    """Query personal_events; prefer server-side where, else client-side source filter."""
    if top_k <= 0:
        return []
    try:
        return _semantic_search(query, top_k=top_k, source=source)
    except Exception:
        pass
    # where may be unsupported / broken — fetch wider and filter client-side
    try:
        events = _semantic_search(query, top_k=max(top_k * 3, top_k), source=None)
    except Exception:
        return []
    if not source:
        return events[:top_k]
    filtered = [e for e in events if (e.get("source") or "") == source]
    return filtered[:top_k] if filtered else []


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_tokens(query: str, max_toks: int = 4) -> list[str]:
    """Extract distinctive CJK/latin tokens for AND-style LIKE search."""
    toks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z_][A-Za-z0-9_\-]{2,}", query or "")
    # longer first (more distinctive for code / paths)
    ordered = sorted(set(toks), key=len, reverse=True)
    return ordered[:max_toks]


def _search_dialogue_canonical_messages(
    query: str,
    top_k: int = 5,
    db_path: Path | None = None,
) -> list[dict]:
    """Message-level dialogue fallback on canonical_messages (read-only).

    Strategy (Phase 15 W4):
    1) For long pastes: sliding snippet LIKE (handles code-literal gold)
    2) Else: multi-token AND LIKE on distinctive terms

    Returns hybrid-schema items with unit_id = canonical_message_id (cm|…).
    """
    if top_k <= 0 or not (query or "").strip():
        return []
    path = Path(db_path) if db_path else _C.AGENT_CONVERSATIONS_DB
    if not path.exists():
        return []

    q = query.strip()
    rows: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
        except Exception:
            pass
        # Prefer non-system messages when column exists
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(canonical_messages)")}
        except Exception:
            cols = set()
        if "canonical_message_id" not in cols or "content" not in cols:
            con.close()
            return []

        base_where, projection_params = canonical_projection_predicate(
            con, "canonical_message_id"
        )
        if "is_system" in cols:
            base_where += " AND COALESCE(is_system,0)=0"

        if len(q) >= 40:
            for start in (0, 20, 50, 100, min(200, max(0, len(q) - 40))):
                snip = q[start : start + 40]
                if len(snip) < 20:
                    continue
                esc = _like_escape(snip)
                sql = (
                    "SELECT canonical_message_id, role, substr(content,1,300), "
                    "COALESCE(timestamp,''), COALESCE(source,'') "
                    f"FROM canonical_messages WHERE {base_where} "
                    "AND content LIKE ? ESCAPE '\\' LIMIT ?"
                )
                try:
                    found = con.execute(
                        sql, (*projection_params, f"%{esc}%", top_k * 2)
                    ).fetchall()
                except Exception:
                    found = []
                if found:
                    rows = found
                    break

        if not rows:
            toks = _query_tokens(q, max_toks=4)
            if toks:
                sql = (
                    "SELECT canonical_message_id, role, substr(content,1,300), "
                    "COALESCE(timestamp,''), COALESCE(source,'') "
                    f"FROM canonical_messages WHERE {base_where}"
                )
                params: list[Any] = list(projection_params)
                for t in toks:
                    sql += " AND content LIKE ? ESCAPE '\\'"
                    params.append(f"%{_like_escape(t)}%")
                sql += " LIMIT ?"
                params.append(top_k * 2)
                try:
                    rows = con.execute(sql, params).fetchall()
                except Exception:
                    rows = []
        con.close()
    except Exception:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for mid, role, content, ts, src in rows:
        mid = str(mid or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        # Prefer higher score for earlier ranks; user messages slight boost
        base = 0.92 - 0.02 * len(out)
        if (role or "") == "user":
            base = min(0.99, base + 0.03)
        out.append(
            {
                "unit_id": mid,
                "subject": f"dialogue:{(role or 'msg')}"[:80],
                "answer": (content or "")[:300],
                "score": round(base, 4),
                "lifecycle": "current",
                "confidence": 0,
                "source_message_ref": mid,
                "collection": CANONICAL_MESSAGES_COLLECTION,
                "retrieval_unit": "dialogue",
                "rank_reason": "dialogue_fallback canonical_messages",
                "event_id": mid,
                "source": src or "Agent",
                "event_time": ts or "",
                "role": role or "",
            }
        )
        if len(out) >= top_k:
            break
    return out


def _read_knowledge_active_collection() -> str:
    """Resolve active knowledge index from SQLite authority, then legacy pointer."""
    pointer = _C.DB_DIR / "knowledge_index_active.txt"
    state = ServingSnapshotResolver(_C.UNIFIED_DB, pointer).resolve()
    return str((state.member("knowledge_retrieval") or {}).get("location_ref") or "")


def get_knowledge_status(*, probe_chroma: bool = True) -> dict:
    """知识索引只读状态（CLI / REST / MCP 共用）。

    返回 active collection、pointer、版本行、fallback_policy/ssot 与（可选）Chroma 实际条数。
    不暴露 promote/rollback；运维脚本仍走 knowledge/* 入口。
    """
    from personal_knowledge.core.project_paths import DB_DIR  # noqa: E402

    pointer = _C.DB_DIR / "knowledge_index_active.txt"
    active = _read_knowledge_active_collection()
    serving = ServingSnapshotResolver(_C.UNIFIED_DB, pointer).resolve()
    policy = _resolve_fallback_policy(None)
    if policy == "layered":
        route_policy = "wiki-first + layered fallback (cards→KU→dialogue→non_dialogue_raw)"
    else:
        route_policy = "knowledge-first + raw fallback"
    out: dict[str, Any] = {
        "available": bool(active),
        "active_collection": active or None,
        "serving_snapshot_id": serving.snapshot_id,
        "snapshot_hash": serving.manifest_hash,
        "snapshot_drift": serving.drift,
        "pointer_path": str(pointer),
        "pointer_exists": pointer.exists(),
        "search_backend": "search_knowledge_units",
        "route_policy": route_policy,
        # Phase 15: 三层 SSOT 与 fallback 策略（见 docs/architecture/retrieval-ssot.md）
        # fallback_policy: legacy = 全量 personal_events 补洞；layered = wiki→cards→KU→dialogue→non_dialogue
        "ssot": {
            "dialogue": "agentsview_canonical",
            "knowledge": "canonical_knowledge_units",
            "non_dialogue_raw": "personal_events",
            "google_structure": "google_data.normalized_events+light_assertions",
        },
        "fallback_policy": policy,
        "allow_legacy_pad": _resolve_allow_legacy_pad(None),
        "semantic_routes": {
            "cli": "unified_search.py semantic",
            "rest": "POST /search/semantic",
            "mcp": "search_semantic",
        },
        "unit_count": None,
        "db_unit_count": None,
        "version": {},
        "chroma_available": False,
        "google_structure": get_google_structure_status(),
    }

    if active and _C.UNIFIED_DB.exists():
        try:
            con = sqlite3.connect(f"file:{_C.UNIFIED_DB.as_posix()}?mode=ro", uri=True)
            row = con.execute(
                "SELECT version_id, build_id, collection_name, unit_count, status, "
                "created_at, activated_at, checksum "
                "FROM knowledge_index_versions WHERE collection_name=? "
                "ORDER BY created_at DESC LIMIT 1",
                (active,),
            ).fetchone()
            if row:
                out["version"] = {
                    "version_id": row[0],
                    "build_id": row[1],
                    "collection_name": row[2],
                    "unit_count": row[3],
                    "status": row[4],
                    "created_at": row[5],
                    "activated_at": row[6],
                    "checksum": (row[7] or "")[:16] + ("…" if row[7] and len(row[7]) > 16 else ""),
                }
                out["db_unit_count"] = row[3]
                if out["unit_count"] is None:
                    out["unit_count"] = row[3]
            # canonical current count (may span multiple runs for merged index)
            try:
                cur = con.execute(
                    "SELECT COUNT(*) FROM canonical_knowledge_units WHERE status='current'"
                ).fetchone()
                out["canonical_current_count"] = cur[0] if cur else None
            except sqlite3.Error:
                out["canonical_current_count"] = None
            con.close()
        except Exception as e:
            out["db_error"] = str(e)[:120]

    if probe_chroma and active:
        try:
            from personal_knowledge.core.chroma_client import ChromaClient  # noqa: E402

            client = ChromaClient(port=_KU_PORT)
            coll = client.get_or_create_collection(active)
            count = coll.count()
            out["unit_count"] = count
            out["chroma_available"] = True
            out["chroma_port"] = _KU_PORT
        except Exception as e:
            out["chroma_error"] = str(e)[:120]

    return out


# --- Fallback layer orchestration (OC-4 split) --------------------------------
# Layer names in response-telemetry order. wiki_page / semantic_card /
# knowledge_unit are primary; the rest form the fallback chain.
_LAYER_NAMES = (
    "wiki_page",
    "semantic_card",
    "knowledge_unit",
    "canonical_messages",
    "conversation_turns",
    "non_dialogue_raw",
    "legacy_pad",
    "legacy_personal_events",
)

# Serving-snapshot member role per versionable layer (response telemetry.version).
_LAYER_ROLES = {
    "knowledge_unit": "knowledge_retrieval",
    "canonical_messages": "canonical_message",
    "conversation_turns": "turn_retrieval",
    "non_dialogue_raw": "google_normalized",
}


def _append_unique(query: str, item: dict, state: SearchState) -> bool:
    """Dedup + evidence-gate a fallback candidate and append when accepted."""
    decision = annotate_candidate_support(
        query,
        item,
        resolve=lambda ref: state.resolve_support_ref(item, ref),
    )
    if decision.state == "unsupported":
        return False
    uid = str(item.get("unit_id") or item.get("event_id") or "")
    if uid and uid in state.seen_ids:
        return False
    if uid:
        state.seen_ids.add(uid)
    state.fallback_results.append(item)
    return True


def _run_compressed_chain(query: str, state: SearchState, layers: list[dict[str, Any]]) -> None:
    """Run wiki_page then semantic_card before KU. SQLite-only; never writes."""
    from personal_knowledge.retrieval.layers.semantic_card import SemanticCardLayer  # noqa: E402
    from personal_knowledge.retrieval.layers.wiki_page import WikiPageLayer  # noqa: E402

    factories = {"wiki_page": WikiPageLayer, "semantic_card": SemanticCardLayer}
    layer_by_name = {item["name"]: item for item in layers}
    for name in _C.COMPRESSED_LAYER_NAMES:
        telemetry = layer_by_name[name]
        if state.remaining() <= 0:
            break
        telemetry["attempted"] = True
        t_layer = time.perf_counter()
        n_before = len(state.compressed_results)
        try:
            hits = factories[name](telemetry).retrieve(query, state)
            for item in hits:
                decision = annotate_candidate_support(query, item, resolve=None)
                if decision.state == "unsupported":
                    continue
                uid = str(item.get("unit_id") or "")
                if uid and uid in state.seen_ids:
                    continue
                if uid:
                    state.seen_ids.add(uid)
                state.compressed_results.append(item)
                if state.remaining() <= 0:
                    break
        except Exception:
            pass
        finally:
            telemetry["hits"] = max(0, len(state.compressed_results) - n_before)
            telemetry["latency_ms"] = round((time.perf_counter() - t_layer) * 1000, 2)


def _run_fallback_chain(query: str, state: SearchState, layers: list[dict[str, Any]]) -> None:
    """Execute the policy fallback chain with shared quota/dedup/telemetry.

    Layer order, per-layer fetch-ahead quotas and skip conditions (snapshot
    role gating, unbound-only layers, legacy-pad opt-out) live in
    fallback_policy. Each layer reports attempted/hits/latency on its shared
    telemetry dict; collection/chroma errors are soft-skipped per layer.
    """
    from personal_knowledge.retrieval import fallback_policy as _fp  # noqa: E402

    _fp.mark_snapshot_skipped_layers(
        state.policy, {x["name"]: x for x in layers},
        snapshot_enforced=state.snapshot_enforced,
    )
    chain = _fp.build_fallback_chain(
        state.policy, {x["name"]: x for x in layers}, snapshot_enforced=state.snapshot_enforced,
    )
    for layer in chain:
        name = layer.layer_name
        if state.remaining() <= 0:
            break
        reason = _fp.layer_skip_reason(name, state)
        if reason is not None:
            layer.telemetry["skipped_reason"] = reason
            continue
        if _fp.layer_is_gated(name, pad_allowed=state.pad_allowed):
            continue
        layer.telemetry["attempted"] = True
        t_layer = time.perf_counter()
        n_before = len(state.fallback_results)
        try:
            hits = layer.retrieve(query, state)
            for item in hits:
                _append_unique(query, item, state)
                if state.remaining() <= 0:
                    break
        except Exception:
            # collection missing / chroma error — soft skip per layer
            pass
        finally:
            layer.telemetry["hits"] = max(0, len(state.fallback_results) - n_before)
            layer.telemetry["latency_ms"] = round((time.perf_counter() - t_layer) * 1000, 2)


def _apply_evidence_resolution(merged: list[dict], serving: Any) -> None:
    """Attach typed evidence resolution to each merged result (include_evidence)."""
    from personal_knowledge.retrieval.evidence import EvidenceResolver  # noqa: E402

    resolver = EvidenceResolver(
        unified_db=_C.UNIFIED_DB,
        conversation_db=_C.AGENT_CONVERSATIONS_DB,
        google_db=_C.GOOGLE_DB,
    )
    for item in merged:
        ref = str(item.get("source_message_ref") or item.get("unit_id") or item.get("event_id") or "")
        if not ref:
            continue
        if ref.startswith("cm|") or ref.startswith("v2|cm|"):
            # v2|cm| 是 v2 兼容投影的 canonical_message_id（与旧 cm| 并存），
            # 无论来自卡片/KU/dialogue 都按 canonical_message 解析。
            artifact_type = "canonical_message"
            role = "canonical_message"
        elif str(item.get("source") or "").lower() == "google" or ref.startswith("g|"):
            artifact_type = "google_signal"
            role = "google_normalized"
        elif item.get("retrieval_unit") == "dialogue":
            artifact_type = "turn"
            role = "turn_retrieval"
        else:
            artifact_type = "knowledge_unit"
            role = "canonical_knowledge"
        item["evidence"] = resolver.resolve(
            ref,
            artifact_type=artifact_type,
            include_content=True,
            source_version=member_version(serving.member(role)),
        )


def search_knowledge_units(
    query: str,
    top_k: int = 5,
    source: Optional[str] = None,
    include_evidence: bool = False,
    collection_override: Optional[str] = None,
    fallback_policy: str | None = None,
    allow_legacy_pad: bool | None = None,
    current_only: bool = True,
) -> dict:
    """知识单元混合检索 backend。

    knowledge-first + fallback：layered 先查 wiki 主题页与会话卡，再查 active
    knowledge unit collection，再按 fallback_policy 补洞:
      - legacy:  全量 personal_events（旧行为，不含 wiki/cards 前缀）
      - layered: wiki → cards → KU → dialogue → personal_events(Google) → 可选 legacy_pad

    返回 route/versions/results/telemetry，CLI/REST/MCP 共用此唯一 backend。

    query: 自然语言查询
    top_k: 返回条数（默认 5，有界 [1,20]）
    source: 过滤 raw 事件数据源（不影响知识层；layered 下 non_dialogue 默认 Google）
    include_evidence: True=附带 evidence quote（默认只给 refs）
    collection_override: 指定 knowledge collection（canary 用，不改变 active pointer）
    fallback_policy: "legacy" | "layered"；None=读 env PERSONAL_DATA_FALLBACK_POLICY，默认 layered
    allow_legacy_pad: layered 仍不足时是否用非 Google personal_events 填充；默认 True
    current_only: 显式声明当前检索契约；当前索引与知识层过滤均只放行 lifecycle=current。
      传 False 暂不改变结果，因为索引不包含非 current 单元。

    返回: {
        "route": "knowledge" | "fallback_raw" | "abstain",
        "reason": str (fallback/abstain 时),
        "fallback_policy": "legacy" | "layered",
        "allow_legacy_pad": bool,
        "telemetry": {layers, first_contributing_layer, pad_allowed, pad_used, total_latency_ms},
        "results": [{"rank","unit_id","subject","answer","score","lifecycle",
                      "confidence","source_message_ref","collection","retrieval_unit"}, ...],
        "versions": {"index_version","build_id","canonical_build_id","unit_count","status"},
    }
    """
    # The active index and knowledge-layer guard are current-only today. Keep
    # the parameter explicit without widening the result set until a future
    # historical-value index is designed and evaluated.
    _ = current_only
    top_k = max(1, min(20, top_k))
    policy = _resolve_fallback_policy(fallback_policy)
    pad_allowed = _resolve_allow_legacy_pad(allow_legacy_pad)
    t0 = time.perf_counter()
    layers: list[dict[str, Any]] = [
        _empty_layer_telemetry(name) for name in _LAYER_NAMES
    ]
    layer_by_name = {x["name"]: x for x in layers}
    pointer = _C.DB_DIR / "knowledge_index_active.txt"
    serving = ServingSnapshotResolver(_C.UNIFIED_DB, pointer).resolve()
    # Resolve through the public compatibility hook as well. In production it
    # reads the same SQLite authority; tests and embedded callers can inject an
    # isolated authority without inheriting this machine's live snapshot.
    resolved_active = _read_knowledge_active_collection()
    snapshot_collection = str((serving.member("knowledge_retrieval") or {}).get("location_ref") or "")
    snapshot_enforced = bool(
        serving.snapshot_id
        and collection_override is None
        and resolved_active
        and resolved_active == snapshot_collection
    )
    for layer_name, role in _LAYER_ROLES.items():
        layer_by_name[layer_name]["version"] = member_version(serving.member(role))

    def _pack(
        *,
        route: str,
        results: list[dict],
        versions: dict | None = None,
        reason: str | None = None,
        ku_collection: str | None = None,
    ) -> dict:
        out: dict[str, Any] = {
            "route": route,
            "results": results,
            "versions": versions or {},
            "fallback_policy": policy,
            "allow_legacy_pad": pad_allowed,
            "telemetry": _finalize_search_telemetry(
                layers, pad_allowed=pad_allowed, t0=t0
            ),
            "serving_snapshot_id": serving.snapshot_id,
            "snapshot_hash": serving.manifest_hash,
            "snapshot_drift": serving.drift,
            "snapshot_consistency": (
                "override_unbound" if collection_override else
                "enforced" if snapshot_enforced else "legacy"
            ),
        }
        if reason is not None:
            out["reason"] = reason
        if ku_collection is not None:
            out["collection"] = ku_collection
        return out

    if not query or not query.strip():
        return _pack(route="abstain", results=[], reason="empty query")

    # Optional vector stack. Wiki/cards are SQLite and must still run when
    # Chroma/embedding are missing (offline tests and empty active KU index).
    ChromaClient = None
    ChromaError = Exception
    local_embed = None
    try:
        from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
        import personal_knowledge.core.local_embed as local_embed  # noqa: E402
    except Exception:
        pass

    # 解析 knowledge collection
    if collection_override:
        ku_collection = collection_override
    elif snapshot_enforced:
        ku_collection = snapshot_collection
    else:
        # Compatibility hook retained for tests and pre-snapshot installations.
        ku_collection = resolved_active

    client = None
    embedding = None
    if ChromaClient is not None:
        client = ChromaClient(port=_KU_PORT)
        # Probe the optional vector service before loading the optional local
        # embedding stack. Offline/CI environments must still reach the layered
        # SQLite fallback instead of failing on a missing model or port 8001.
        if ku_collection:
            try:
                collections = client.list_collections()
                names = {item if isinstance(item, str) else item.get("name", "") for item in collections}
                if ku_collection in names:
                    embedding = local_embed.embed(query)
                else:
                    ku_collection = ""
            except Exception as exc:
                # 向量路径失效必须留痕（embedding 模型缺失/Chroma 不可达等），
                # 静默降级会让检索质量塌方且无从排查。
                print(f"[search_semantic] KU vector path unavailable, keyword fallback: {exc}", file=sys.stderr)
                ku_collection = ""
            if ku_collection and embedding is None:
                print("[search_semantic] query embedding unavailable, keyword fallback", file=sys.stderr)
                ku_collection = ""
    else:
        ku_collection = ""

    from personal_knowledge.retrieval.evidence import EvidenceResolver  # noqa: E402
    support_resolver = EvidenceResolver(
        unified_db=_C.UNIFIED_DB,
        conversation_db=_C.AGENT_CONVERSATIONS_DB,
        google_db=_C.GOOGLE_DB,
    )

    def _resolve_support_ref(item: dict, ref: str) -> dict:
        unit = str(item.get("retrieval_unit") or "")
        source_name = str(item.get("source") or "").lower()
        if ref.startswith("cm|") or ref.startswith("v2|cm|"):
            artifact_type = "canonical_message"
        elif source_name == "google" or ref.startswith("g|"):
            artifact_type = "google_signal"
        elif unit in {"dialogue", "turn"}:
            artifact_type = "turn"
        else:
            artifact_type = "knowledge_unit"
        return support_resolver.resolve(ref, artifact_type=artifact_type, include_content=True)

    state = SearchState(
        top_k=top_k,
        source=source,
        include_evidence=include_evidence,
        policy=policy,
        pad_allowed=pad_allowed,
        snapshot_enforced=snapshot_enforced,
        serving=serving,
    )
    state.ku_collection = ku_collection
    state.embedding = embedding
    state.client = client
    state.vector_available = bool(ku_collection and embedding is not None)
    state.resolve_support_ref = _resolve_support_ref

    # --- Phase 0: wiki + session cards (layered only; SQLite projections) ---
    if policy == "layered":
        _run_compressed_chain(query, state, layers)

    # --- Phase 1: 知识层检索（Chroma active，空代则 sqlite current） ---
    route = "knowledge" if state.compressed_results else "fallback_raw"
    if state.remaining() > 0:
        layer = layer_by_name["knowledge_unit"]
        layer["attempted"] = True
        t_layer = time.perf_counter()
        try:
            from personal_knowledge.retrieval.layers.knowledge_unit import KnowledgeUnitLayer  # noqa: E402
            ku_results = KnowledgeUnitLayer(layer).retrieve(query, state)
            state.ku_results = ku_results
            for item in ku_results:
                uid = str(item.get("unit_id") or "")
                if uid:
                    state.seen_ids.add(uid)
            if state.ku_results:
                route = "knowledge"
        except ChromaError:
            pass
        finally:
            layer["hits"] = len(state.ku_results)
            layer["latency_ms"] = round((time.perf_counter() - t_layer) * 1000, 2)

    # --- Phase 2: fallback 补洞（层顺序/配额/跳过条件集中在 fallback_policy） ---
    _run_fallback_chain(query, state, layers)

    # --- 合并 + 排名 ---
    merged = state.compressed_results + state.ku_results + state.fallback_results
    versions = state.versions
    if not merged:
        return _pack(
            route="abstain",
            results=[],
            versions=versions,
            reason="no results from either source",
            ku_collection=ku_collection or "personal_events",
        )

    # Compressed wiki/card hits are knowledge-route results, not raw fallback.
    if route == "fallback_raw" and (state.compressed_results or state.ku_results):
        route = "knowledge"

    # 编号
    if include_evidence:
        _apply_evidence_resolution(merged, serving)

    for i, item in enumerate(merged, 1):
        item["rank"] = i

    return _pack(
        route=route,
        results=merged[:top_k],
        versions=versions,
        ku_collection=ku_collection or "personal_events",
    )

