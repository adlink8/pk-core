"""向量检索脚本(给 AI 用)。

从 chroma personal_events collection 做语义检索,返回与查询最相关的事件。
这是"喂给 AI"的核心接口 —— AI 想知道用户历史时,调 search() 召回真实事件。

两种用法:
1. 命令行:python search_vectors.py "PPT 排版怎么做" [--source Agent] [--top-k 5]
2. 模块:from search_vectors import search; results = search("查询")

检索流程:
  查询文本 -> 本地 bge-small-zh-v1.5 向量化 -> chroma cosine 检索 -> 返回 top-K 事件
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 用本地 GPU embedding(替代 ollama),接口兼容
from personal_knowledge.core import local_embed as ollama_embed
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError


COLLECTION_NAME = "personal_events"
CONVERSATION_COLLECTION = "conversation_turns"  # Wave 7 新增:turn 叙述(含因果链)
DEFAULT_TOP_K = 5


def _normalize_similarity(distance: float) -> float:
    return round(max(0.0, 1.0 - distance / 2.0), 4)


def _query_collection(
    query: str,
    collection_name: str,
    top_k: int,
    source: Optional[str],
    client: Optional[ChromaClient],
) -> tuple[list, list, list, list]:
    if not query or not query.strip():
        return [], [], [], []
    query_vec = ollama_embed.embed(query)
    if client is None:
        client = ChromaClient()
    coll = client.get_or_create_collection(collection_name)
    where = {"source": source} if source else None
    raw = coll.query(
        query_embeddings=[query_vec],
        n_results=top_k,
        where=where,
        include=["metadatas", "documents", "distances"],
    )
    return (
        raw.get("ids", [[]])[0],
        raw.get("distances", [[]])[0],
        raw.get("documents", [[]])[0],
        raw.get("metadatas", [[]])[0],
    )


def search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source: Optional[str] = None,
    client: Optional[ChromaClient] = None,
) -> list[dict]:
    """语义检索用户历史事件(personal_events collection)。"""
    if not query or not query.strip():
        return []

    ids, distances, documents, metadatas = _query_collection(
        query=query,
        collection_name=COLLECTION_NAME,
        top_k=top_k,
        source=source,
        client=client,
    )
    if not ids:
        return []

    results = []
    for i, eid in enumerate(ids):
        dist = distances[i] if i < len(distances) else 1.0
        meta = metadatas[i] if i < len(metadatas) else {}
        doc = documents[i] if i < len(documents) else ""
        results.append(
            {
                "event_id": eid,
                "source": meta.get("source", ""),
                "category_v2": meta.get("category_v2", ""),
                "event_type": meta.get("event_type", ""),
                "service": meta.get("service", ""),
                "event_time": meta.get("event_time", ""),
                "month": meta.get("month", ""),
                "title": meta.get("title", ""),
                "content": doc,
                "score": _normalize_similarity(dist),
                "collection": COLLECTION_NAME,
                "retrieval_unit": "event",
                "rank_reason": "personal_events semantic match",
            }
        )
    return results


def search_conversation_turns(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source: Optional[str] = None,
    client: Optional[ChromaClient] = None,
) -> list[dict]:
    """检索 turn 叙述(conversation_turns collection,Wave 7 新增)。"""
    if not query or not query.strip():
        return []
    try:
        ids, distances, documents, metadatas = _query_collection(
            query=query,
            collection_name=CONVERSATION_COLLECTION,
            top_k=top_k,
            source=source,
            client=client,
        )
    except ChromaError:
        return []

    if not ids:
        return []

    results = []
    for i, eid in enumerate(ids):
        dist = distances[i] if i < len(distances) else 1.0
        meta = metadatas[i] if i < len(metadatas) else {}
        doc = documents[i] if i < len(documents) else ""
        results.append({
            "event_id": eid,
            "source": meta.get("source", ""),
            "event_type": meta.get("event_type", "conversation_turn"),
            "session_id": meta.get("session_id", ""),
            "turn_id": meta.get("turn_id", ""),
            "turn_no": meta.get("turn_no", 0),
            "main_topic": meta.get("main_topic", ""),
            "title": meta.get("main_topic", ""),
            "category_v2": "对话叙述",
            "service": meta.get("source", ""),
            "event_time": "",
            "month": "",
            "content": doc,
            "score": _normalize_similarity(dist),
            "collection": CONVERSATION_COLLECTION,
            "retrieval_unit": "conversation_turn",
            "rank_reason": "conversation_turn causal narrative match",
        })
    return results


def _rank_key(result: dict) -> tuple:
    score = float(result.get("score", 0.0))
    collection = result.get("collection", "")
    retrieval_unit = result.get("retrieval_unit", "")
    turn_bonus = 1 if collection == CONVERSATION_COLLECTION else 0
    unit_bonus = 1 if retrieval_unit == "conversation_turn" else 0
    title_len = len((result.get("title") or "").strip())
    return (score, turn_bonus, unit_bonus, title_len)


def search_all(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source: Optional[str] = None,
    include_turns: bool = True,
) -> list[dict]:
    """跨 collection 合并检索(personal_events + conversation_turns)。

    规则:
    - 保留原始 score
    - 补充 collection / retrieval_unit / rank_reason
    - 分数接近时优先 conversation_turns,但不修改原始 score
    """
    if not query or not query.strip():
        return []
    client = ChromaClient()
    results = search(query, top_k=top_k, source=source, client=client)

    if include_turns:
        try:
            turns = search_conversation_turns(
                query, top_k=top_k, source=source, client=client)
            results.extend(turns)
        except (ChromaError, Exception):
            pass

    results.sort(key=_rank_key, reverse=True)
    return results[:top_k]


def format_result(r: dict, show_content: int = 200) -> str:
    """格式化单条结果用于打印。show_content 控制内容显示长度。"""
    lines = [
        f"[{r['score']:.3f}] [{r['source']}] {r['title'] or '(无标题)'}",
        f"    来源库: {r.get('collection', '')} | 单元: {r.get('retrieval_unit', '')} | 原因: {r.get('rank_reason', '')}",
        f"    时间: {r['event_time']} | 分类: {r['category_v2']} | 服务: {r['service']}",
    ]
    if r["content"]:
        content = r["content"][:show_content]
        if len(r["content"]) > show_content:
            content += "…"
        lines.append(f"    内容: {content}")
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="语义检索用户历史事件")
    parser.add_argument("query", help="自然语言查询")
    parser.add_argument("--source", default=None, help="过滤数据源(Google/GPT/Agent)")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="返回条数")
    parser.add_argument("--content-len", type=int, default=200, help="内容显示长度")
    args = parser.parse_args()

    print(f"检索: \"{args.query}\"" + (f" (source={args.source})" if args.source else ""))
    print(f"  top_k={args.top_k}")
    print("-" * 60)

    results = search(args.query, top_k=args.top_k, source=args.source)
    if not results:
        print("无匹配结果(可能向量库未构建,或查询无相关内容)")
        return

    for i, r in enumerate(results, 1):
        print(f"\n#{i}")
        print(format_result(r, show_content=args.content_len))

    print("\n" + "-" * 60)
    print(f"共 {len(results)} 条结果")


if __name__ == "__main__":
    main()
