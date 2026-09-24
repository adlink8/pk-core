"""MCP handlers 共享的格式化 helper（从 services/mcp_server.py 原样拆出）。

对外契约保持：_format_* / _json_contract / _event_contract_args 与拆分前
逐字节等价，services/mcp_server.py 会原样 re-export 以兼容旧 import。
"""

from __future__ import annotations

import json


def _format_semantic(ku_result: dict) -> str:
    """把混合检索结果格式化成 AI 友好的文本。

    ku_result: {"route": str, "results": list[dict], "versions": dict}
    """
    results = ku_result.get("results", [])
    route = ku_result.get("route", "")
    versions = ku_result.get("versions") or {}
    if not results:
        return (
            f"无匹配结果(route={route or 'n/a'})。"
            "可能知识索引未 promote、向量库未构建,或查询无相关内容。"
        )
    lines = [
        f"检索路由: {route}  共召回 {len(results)} 条(knowledge-first + raw fallback):",
    ]
    if versions:
        lines.append(
            f"知识索引版本: {versions.get('index_version') or versions.get('collection') or ''} "
            f"build={versions.get('build_id', '')} units={versions.get('unit_count', '')}"
        )
    lines.append("")
    for i, r in enumerate(results, 1):
        unit = r.get("retrieval_unit", "")
        subj = r.get("subject", r.get("title", ""))
        src = r.get("source", r.get("collection", ""))
        lines.append(
            f"#{i} [score={r.get('score', '?')}] [{src}] "
            f"{(subj or '(无标题)')[:60]}"
        )
        if unit:
            lines.append(f"   retrieval_unit: {unit}")
        if r.get("event_id"):
            lines.append(f"   event_id: {r['event_id']}")
        if r.get("unit_id"):
            lines.append(f"   unit_id: {r['unit_id']}")
        if r.get("event_time"):
            lines.append(
                f"   时间: {r.get('event_time', '')} | 分类: {r.get('category_v2', '')} | "
                f"服务: {r.get('service', '')}"
            )
        content = (r.get("answer") or r.get("content") or "")[:400]
        if len(r.get("answer") or r.get("content") or "") > 400:
            content += "…"
        lines.append(f"   内容: {content}")
        lines.append("")
    return "\n".join(lines)


def _format_semantic_cards(rows: list[dict]) -> str:
    """把 MVP 会话卡检索结果(semantic_cards.search_cards 摘要行)格式化。

    rows: [{session_id, purpose, score, fact_hits, matched_facts}, ...]
    """
    if not rows:
        return (
            "无匹配结果。MVP 会话卡检索为关键词匹配"
            "(ASCII 标识符取全词、中文取 2-gram),可换更具体的关键词重试。"
        )
    lines = [f"MVP 会话卡检索: 共召回 {len(rows)} 条(按相关度降序):", ""]
    for i, r in enumerate(rows, 1):
        sid = r.get("session_id", "")
        parts = sid.split("|")
        short = f"{parts[-2]}:{parts[-1][:12]}" if len(parts) >= 2 else sid[:15]
        lines.append(f"#{i} [score={r.get('score', 0):.1f}] {short}")
        lines.append(f"   session_id: {sid}")
        lines.append(f"   目的: {(r.get('purpose') or '(无卡,仅事实命中)')[:80]}")
        if r.get("fact_hits"):
            lines.append(f"   命中事实 {r['fact_hits']} 条:")
            for fact in r.get("matched_facts", []):
                lines.append(f"   - {fact[:120]}")
        lines.append("")
    return "\n".join(lines)


def _format_query(results: list[dict]) -> str:
    """把精确查询结果格式化。"""
    if not results:
        return "无匹配结果。"
    lines = [f"共 {len(results)} 条(按时间倒序):", ""]
    for r in results:
        lines.append(
            f"[{r.get('source', '?')}] {r.get('event_time', '')} | "
            f"{(r.get('title') or '(无标题)')[:50]} | {r.get('category_v2', '')}"
        )
        lines.append(f"   event_id: {r.get('event_id', '')}")
    return "\n".join(lines)


def _format_detail(data: dict) -> str:
    """把单条详情格式化(全字段)。"""
    lines = []
    for k, v in data.items():
        val = "" if v is None else str(v)
        lines.append(f"{k}: {val}")
    return "\n".join(lines)


def _format_stats(data: dict) -> str:
    lines = [
        f"总事件数: {data.get('total_events', 0):,}",
        f"活跃月份: {data.get('active_months', 0)}",
        "按源分布:",
    ]
    for s, n in data.get("by_source", {}).items():
        lines.append(f"  {s}: {n:,}")
    if data.get("vector_available"):
        lines.append(f"向量库: {data.get('vector_count', 0):,} 条(可语义检索)")
    else:
        lines.append(f"向量库: 不可用({data.get('vector_error', '')})")
    ku = data.get("knowledge") or {}
    if ku:
        lines.append(
            f"知识索引: available={ku.get('available')} "
            f"collection={ku.get('active_collection') or '(none)'} "
            f"units={ku.get('unit_count')} "
            f"policy={ku.get('route_policy')}"
        )
    return "\n".join(lines)


def _format_knowledge_status(data: dict) -> str:
    lines = [
        f"available: {data.get('available')}",
        f"active_collection: {data.get('active_collection') or '(none)'}",
        f"unit_count: {data.get('unit_count')}",
        f"db_unit_count: {data.get('db_unit_count')}",
        f"canonical_current_count: {data.get('canonical_current_count')}",
        f"route_policy: {data.get('route_policy')}",
        f"fallback_policy: {data.get('fallback_policy')}",
        f"ssot: {data.get('ssot')}",
        f"chroma_available: {data.get('chroma_available')}",
    ]
    if data.get("chroma_error"):
        lines.append(f"chroma_error: {data.get('chroma_error')}")
    ver = data.get("version") or {}
    if ver:
        lines.append(
            f"version: status={ver.get('status')} build={ver.get('build_id')} "
            f"activated={ver.get('activated_at')}"
        )
    routes = data.get("semantic_routes") or {}
    if routes:
        lines.append("semantic_routes:")
        for k, v in routes.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def _format_categories(rows: list[dict]) -> str:
    if not rows:
        return "无分类数据。"
    lines = [f"共 {len(rows)} 个分类(降序):", ""]
    for r in rows:
        lines.append(f"  {r['category_v2']}: {r['n']}")
    return "\n".join(lines)


def _format_memory_profile(data: dict) -> str:
    if not data.get("available"):
        return data.get("hint", "记忆层未构建。")
    lines = [f"记忆总数: {data.get('total', 0)}", "按类型:"]
    for t, n in data.get("by_type", {}).items():
        lines.append(f"  {t}: {n}")
    lines.append("")
    lines.append("明细:")
    for item in data.get("items", [])[:50]:
        lines.append(
            f"- [{item.get('memory_type')}/{item.get('memory_subtype')}] "
            f"{item.get('subject')} | 证据 {item.get('evidence_count')} | "
            f"置信 {item.get('confidence')}"
        )
        lines.append(f"  {item.get('description', '')[:240]}")
    return "\n".join(lines)


def _format_memory_detail(data: dict) -> str:
    memory = data.get("memory") or {}
    lines = [
        f"[{memory.get('memory_type')}/{memory.get('memory_subtype')}] {memory.get('subject')}",
        f"置信度: {memory.get('confidence')} | 证据数: {memory.get('evidence_count')}",
        f"描述: {memory.get('description', '')}",
    ]
    relations = data.get("relations") or []
    if relations:
        lines.extend(["", "关系:"])
        for r in relations[:30]:
            lines.append(
                f"- {r.get('from_subject')} --{r.get('relation')}({r.get('strength')})--> "
                f"{r.get('to_subject')}"
            )
    neighbors = data.get("neighbors") or {}
    if neighbors.get("levels"):
        lines.extend(["", "邻居:"])
        for level in neighbors["levels"]:
            nodes = ", ".join(
                f"{n.get('subject')}[{n.get('memory_type')}]" for n in level.get("nodes", [])
            )
            lines.append(f"- {level.get('hop')}跳: {nodes or '(无)'}")
    return "\n".join(lines)


def _json_contract(data: dict) -> str:
    """Return contract JSON as tool text so every MCP client can consume it."""
    return json.dumps(data, ensure_ascii=False, default=str, indent=2)


def _event_contract_args(arguments: dict) -> dict:
    return {
        "source": arguments.get("source"),
        "service": arguments.get("service"),
        "category": arguments.get("category") or arguments.get("category_v2"),
        "time_from": arguments.get("time_from") or arguments.get("start_time"),
        "time_to": arguments.get("time_to") or arguments.get("end_time"),
        "keyword": arguments.get("keyword") or arguments.get("query"),
    }
