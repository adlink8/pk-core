"""向量库构建脚本(阶段二核心)。

从统合库 personal_system.sqlite 读取 content_rich(阶段一补全的真实文本),
经本地 bge-small-zh-v1.5 向量化,写入 chroma personal_events collection。

管道:
  unified_events_rich (8136 条)
    │ 过滤:content_rich 非空且 >=10 字符
    │ 本地批量 embed(每批 64 条,带进度)
    ▼
  chroma personal_events collection
    元数据: source / category_v2 / event_time / month / service / event_type

设计原则(沿用阶段一二):
- 幂等:重复运行结果一致(先 delete_collection 再重建)
- 不动 SQLite 表(只读)
- 不碰 chroma 的 novel_6/novel_7(只操作 personal_events)
- 断点续传:进度存 JSON,中断后可从断点继续
- 失败重试:批次失败时保留进度,支持断点续传

运行: python integration\\scripts\\build_vector_store.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 阶段二改用本地 GPU embedding(sentence-transformers + bge-small-zh),
# 替代 ollama(批量 embedding 不稳定会 hang)。接口兼容,别名导入。
from personal_knowledge.core import local_embed as ollama_embed
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError


# === 配置 ===
ROOT = Path(__file__).resolve().parents[3]
UNIFIED_DB = ROOT / "integration" / "db" / "personal_system.sqlite"
PROGRESS_FILE = ROOT / "integration" / "db" / "vector_build_progress.json"
COLLECTION_NAME = "personal_events"  # 独立 collection,不碰 novel_*
MIN_CONTENT_LEN = 10  # content_rich 最短长度,短于此跳过(无语义价值)
BATCH_SIZE = 64  # 每批 embedding + 写入的条数(批量越大每条越快,但单次失败重试成本高)


def load_events(db_path: Path = UNIFIED_DB) -> list[dict]:
    """从统合库读取待向量化的事件(title + content_rich + 元数据)。

    需要 unified_events_rich 和 event_categories_v2(阶段一产物)。
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in con.execute(
            "SELECT ue.event_id, ue.source, ue.source_table, ue.event_type, "
            "ue.service, ue.event_time, ue.month, ue.title, ue.content, "
            "r.content_rich, c.category_v2 "
            "FROM unified_events ue "
            "LEFT JOIN unified_events_rich r ON r.event_id = ue.event_id "
            "LEFT JOIN event_categories_v2 c ON c.event_id = ue.event_id"
        )
    ]
    con.close()
    return rows


def filter_vectorizable(rows: list[dict]) -> list[dict]:
    """过滤出可向量化的内容。

    规则:
    - content_rich 非空且 >= MIN_CONTENT_LEN 字符(优先)
    - 否则用 content(若 >= MIN_CONTENT_LEN,如 Google 的 raw_excerpt)
    - 给每条加 _text 字段(最终用于 embedding 的文本:title + 内容)
    """
    out = []
    skipped = 0
    for r in rows:
        content = (r.get("content_rich") or "").strip()
        if len(content) < MIN_CONTENT_LEN:
            # 回退到原始 content
            content = (r.get("content") or "").strip()
        if len(content) < MIN_CONTENT_LEN:
            skipped += 1
            continue
        title = (r.get("title") or "").strip()
        # 拼接 title + content 作为 embedding 文本(title 提供主题锚点)
        text = f"{title} {content}".strip() if title else content
        r["_text"] = text
        out.append(r)
    return out, skipped


def load_progress() -> set[str]:
    """加载已处理的 event_id 集合(断点续传)。"""
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            return set(data.get("processed_ids", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def save_progress(processed_ids: set[str]) -> None:
    """保存进度。"""
    PROGRESS_FILE.write_text(
        json.dumps(
            {"processed_ids": sorted(processed_ids), "count": len(processed_ids)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def build(resume: bool = False) -> dict:
    """主流程:构建向量库。

    resume=True 时尝试断点续传(跳过已处理的 event_id);
    resume=False 时全量重建(删 collection + 清进度)。
    返回统计 dict。
    """
    stats = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # 1. 验证 embedding 模型
    print("[0/4] 验证 embedding 模型...")
    ok, msg, dim = ollama_embed.verify_model()
    if not ok:
        raise RuntimeError(f"embedding 模型不可用: {msg}")
    print(f"    {msg},维度 {dim}")
    stats["embed_dim"] = dim

    # 2. 加载数据
    print("[1/4] 加载统合事件...")
    all_rows = load_events()
    print(f"    总事件: {len(all_rows)}")
    rows, skipped = filter_vectorizable(all_rows)
    print(f"    可向量化: {len(rows)},跳过(内容过短): {skipped}")
    stats["total_events"] = len(all_rows)
    stats["vectorizable"] = len(rows)
    stats["skipped_short"] = skipped

    # 3. 断点续传 or 全量重建
    print("[2/4] 准备 chroma collection...")
    client = ChromaClient()

    # 安全检查:绝不碰 novel_*
    existing = {c["name"] for c in client.list_collections()}
    if not resume and COLLECTION_NAME in existing:
        print(f"    删除旧 {COLLECTION_NAME}(全量重建)...")
        client.delete_collection_by_name(COLLECTION_NAME)
        # 清进度
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()

    processed = load_progress() if resume else set()
    if resume and processed:
        print(f"    断点续传:已有 {len(processed)} 条进度记录")

    # 创建 collection(cosine 相似度)
    coll = client.get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    print(f"    collection: {coll.name} (id={coll.id[:8]})")

    # 4. 批量 embedding + 写入
    print(f"[3/4] 批量向量化(每批 {BATCH_SIZE} 条)...")
    pending = [r for r in rows if r["event_id"] not in processed]
    print(f"    待处理: {len(pending)} 条(已跳过 {len(rows) - len(pending)} 条已完成的)")

    t0 = time.time()
    success = 0
    failed = 0
    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start : batch_start + BATCH_SIZE]
        texts = [r["_text"] for r in batch]

        # 批量 embedding(local_embed.embed_batch 无 progress_every 参数)
        embeddings = ollama_embed.embed_batch(texts)

        # 分离成功/失败
        ok_rows = []
        ok_emb = []
        for r, emb in zip(batch, embeddings):
            if emb is not None:
                ok_rows.append(r)
                ok_emb.append(emb)
                processed.add(r["event_id"])
                success += 1
            else:
                failed += 1

        # 写入 chroma
        if ok_rows:
            coll.upsert(
                ids=[r["event_id"] for r in ok_rows],
                embeddings=ok_emb,
                documents=[r["_text"][:2000] for r in ok_rows],  # chroma 存原文(截断)
                metadatas=[
                    {
                        "source": r.get("source") or "",
                        "source_table": r.get("source_table") or "",
                        "event_type": r.get("event_type") or "",
                        "service": r.get("service") or "",
                        "event_time": r.get("event_time") or "",
                        "month": r.get("month") or "",
                        "category_v2": r.get("category_v2") or "",
                        "title": (r.get("title") or "")[:100],
                    }
                    for r in ok_rows
                ],
            )

        # 存进度(每批一次)
        save_progress(processed)

        # 进度打印
        done = batch_start + len(batch)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(pending) - done) / rate if rate > 0 else 0
        print(
            f"    {done}/{len(pending)} 成功={success} 失败={failed} "
            f"({rate:.1f}/s, ETA {eta/60:.1f}min)",
            flush=True,
        )

    stats["embedded_success"] = success
    stats["embedded_failed"] = failed
    stats["elapsed_sec"] = round(time.time() - t0, 1)

    # 5. 最终统计
    print("[4/4] 统计...")
    final_count = coll.count()
    stats["final_collection_count"] = final_count
    print(f"    collection {COLLECTION_NAME} 现有 {final_count} 条向量")

    # 完成后清进度文件
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    stats["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return stats


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="构建向量库")
    parser.add_argument("--resume", action="store_true", help="断点续传(跳过已处理)")
    args = parser.parse_args()

    print("=" * 60)
    print("向量库构建 build_vector_store.py")
    print(f"  collection: {COLLECTION_NAME}")
    print(f"  embedding: {ollama_embed.EMBED_MODEL} ({ollama_embed.EMBED_DIM}维)")
    print(f"  模式: {'断点续传' if args.resume else '全量重建'}")
    print("=" * 60)

    try:
        stats = build(resume=args.resume)
    except Exception as e:
        print(f"\n❌ 失败: {e}")
        raise

    print()
    print("=" * 60)
    print("完成。统计:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    main()
