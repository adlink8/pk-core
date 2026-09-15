"""Wave 7: turn 叙述回流到向量库(独立 collection)。

PLAN.md Wave 7 主线:把通过 Prompt Lab gate 的 conversation_summary turn 叙述
作为**可检索单元**灌入向量库,闭环"清洗 → 入库 → 检索"的初心。

设计决策(用户拍板 B 方案):
- 独立 collection `conversation_turns`,与现有 `personal_events` 隔离。
- 不污染旧数据,符合项目"叠加不破坏"惯例。
- 检索单元 = turn 叙述(含 user+assistant+tool 因果),不是单条 message。

幂等:用 `{session_id}#{turn_id or turn_no}` 做去重键,重复运行先删 collection
再重建(与 build_vector_store 一致的全量重建策略)。

数据来源:conversation_summaries.json(build_conversation_summary.py 产物)。
向量单元:每个 turn 的 narrative(build_conversation_summary 已用 MiMo 生成)。

用法:
  python build_conversation_vector_store.py --dry-run    # 看会向量化多少 turn
  python build_conversation_vector_store.py --write      # 实际入库(需 chroma 服务)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from personal_knowledge.core import local_embed as ollama_embed
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError
from personal_knowledge.core.conversation_turn_units import (  # noqa: F401  (canonical home; OC-10)
    MIN_NARRATIVE_LEN,
    SUMMARIES_JSON,
    load_turn_units,
)
from personal_knowledge.core.project_paths import ROOT

COLLECTION_NAME = "conversation_turns"  # 独立 collection,绝不碰 personal_events
BATCH_SIZE = 32          # 每批 embedding + 写入的条数


def build(write: bool) -> dict:
    """主流程:把 turn 叙述向量化写入 conversation_turns collection。"""
    stats = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    loaded = load_turn_units()
    if not loaded:
        return stats
    units, skipped_short = loaded
    stats["total_turns_loaded"] = len(units) + skipped_short
    stats["vectorizable"] = len(units)
    stats["skipped_short"] = skipped_short

    print(f"加载 turn 叙述: {len(units)} 个可向量化(跳过 {skipped_short} 个过短)")
    if not units:
        print("[error] 没有可向量化的 turn 叙述")
        stats["error"] = "no vectorizable units"
        return stats

    if not write:
        print("\n[dry] 未写库。样本预览(前 3 个 turn 单元):")
        for u in units[:3]:
            print(f"  id={u['id'][:50]}.. topic={u['metadata']['main_topic'][:30]}")
            print(f"    text 前 80 字: {u['text'][:80]}")
        print(f"\n加 --write 实际入库(需 chroma 服务在线 + summary 已生成)。")
        stats["dry_run"] = True
        return stats

    # 1. 验证 embedding 模型
    print("[0/4] 验证 embedding 模型...")
    ok, msg, dim = ollama_embed.verify_model()
    if not ok:
        raise RuntimeError(f"embedding 模型不可用: {msg}")
    print(f"    {msg},维度 {dim}")
    stats["embed_dim"] = dim

    # 2. 连 chroma,全量重建 conversation_turns
    print(f"[1/4] 准备 collection {COLLECTION_NAME}...")
    client = ChromaClient()

    # 安全检查:绝不碰 personal_events
    existing = {c["name"] for c in client.list_collections()}
    if "personal_events" not in existing:
        print("[warn] personal_events 不存在(本脚本不负责建它,仅提示)")
    if COLLECTION_NAME in existing:
        print(f"    删除旧 {COLLECTION_NAME}(全量重建)...")
        client.delete_collection_by_name(COLLECTION_NAME)

    coll = client.get_or_create_collection(
        COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    print(f"    collection: {coll.name} (id={coll.id[:8]})")

    # 3. 批量 embedding + 写入
    print(f"[2/4] 批量向量化(每批 {BATCH_SIZE} 条)...")
    t0 = time.time()
    success = 0
    failed = 0
    for batch_start in range(0, len(units), BATCH_SIZE):
        batch = units[batch_start: batch_start + BATCH_SIZE]
        texts = [u["text"] for u in batch]
        embeddings = ollama_embed.embed_batch(texts)

        ok_units = []
        ok_emb = []
        for u, emb in zip(batch, embeddings):
            if emb is not None:
                ok_units.append(u)
                ok_emb.append(emb)
                success += 1
            else:
                failed += 1

        if ok_units:
            coll.upsert(
                ids=[u["id"] for u in ok_units],
                embeddings=ok_emb,
                documents=[u["text"][:2000] for u in ok_units],
                metadatas=[u["metadata"] for u in ok_units],
            )

        done = batch_start + len(batch)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        print(f"    {done}/{len(units)} 成功={success} 失败={failed} "
              f"({rate:.1f}/s)", flush=True)

    stats["embedded_success"] = success
    stats["embedded_failed"] = failed
    stats["elapsed_sec"] = round(time.time() - t0, 1)

    # 4. 统计
    print("[3/4] 统计...")
    final_count = coll.count()
    stats["final_collection_count"] = final_count
    print(f"    collection {COLLECTION_NAME} 现有 {final_count} 条向量")

    # 5. 抽样验证检索(用第一个 turn 的 main_topic 反查)
    print("[4/4] 抽样检索验证...")
    sample_ok = _verify_search(client, coll, units)
    stats["sample_search_ok"] = sample_ok

    stats["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return stats


def _verify_search(client: ChromaClient, coll, units: list[dict]) -> bool:
    """抽样验证:用某个 turn 的 main_topic 反查,确认能检索到。"""
    if not units:
        return False
    # 选一个 main_topic 较长的 turn 做查询
    probe = max(units, key=lambda u: len(u["metadata"]["main_topic"]))
    query = probe["metadata"]["main_topic"]
    if not query:
        return False
    try:
        query_vec = ollama_embed.embed(query)
        raw = coll.query(
            query_embeddings=[query_vec], n_results=3,
            include=["metadatas", "documents", "distances"],
        )
        ids = raw.get("ids", [[]])[0]
        distances = raw.get("distances", [[]])[0]
        if ids:
            top_score = 1 - distances[0]  # cosine distance -> similarity
            print(f"    查询 '{query[:30]}' -> top1 score={top_score:.3f} "
                  f"id={ids[0][:40]}")
            # 自检索应该命中自己或同 session 的 turn
            return probe["id"] in ids or top_score > 0.5
    except Exception as exc:
        print(f"    [warn] 抽样检索失败: {type(exc).__name__}: {str(exc)[:80]}")
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="turn 叙述回流向量库 (Wave 7)")
    p.add_argument("--dry-run", action="store_true", help="只看会向量化多少 turn,不入库")
    p.add_argument("--write", action="store_true", help="实际入库(需 chroma 服务)")
    args = p.parse_args(argv)
    if args.dry_run and args.write:
        print("[error] --dry-run 与 --write 互斥", file=sys.stderr)
        return 2
    if not args.dry_run and not args.write:
        print("[error] 必须指定 --dry-run 或 --write", file=sys.stderr)
        return 2

    print("=" * 60)
    print("turn 叙述回流 build_conversation_vector_store.py")
    print(f"  collection: {COLLECTION_NAME} (独立,不碰 personal_events)")
    print(f"  embedding: {ollama_embed.EMBED_MODEL} ({ollama_embed.EMBED_DIM}维)")
    print(f"  数据源: {SUMMARIES_JSON.relative_to(ROOT)}")
    print("=" * 60)

    try:
        stats = build(args.write)
    except Exception as e:
        print(f"\n❌ 失败: {e}")
        raise

    print()
    print("=" * 60)
    print("完成。统计:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
