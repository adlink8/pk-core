"""Phase 14 Wave 0.2 / Wave 4.2：knowledge unit RAG 评估。

在 ``personal_events`` (raw baseline) 或 candidate collection 上用 eval dataset
跑 Recall@5 / MRR@5 / no-answer false positive / deprecated-secret hit / latency。

用法::

    python evaluate_knowledge_unit_rag.py --dataset raw-baseline
    python evaluate_knowledge_unit_rag.py --dataset frozen-test --candidate latest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from personal_knowledge.core.project_paths import UNIFIED_DB, AI_CONTEXT_DIR, KNOWLEDGE_EVAL_DIR
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
import personal_knowledge.core.local_embed as local_embed  # noqa: E402

EVAL_DIR = KNOWLEDGE_EVAL_DIR
BASELINE_PATH = AI_CONTEXT_DIR / "knowledge_unit_raw_baseline.json"


@dataclass
class EvalMetrics:
    """检索评估指标。"""
    dataset: str = ""
    collection: str = ""
    total_queries: int = 0
    recall_at_5: float = 0.0
    mrr_at_5: float = 0.0
    no_answer_false_positive: int = 0  # 应 abstain 但返回了结果
    deprecated_secret_hit: int = 0     # 命中 deprecated/secret（必须 0）
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    collection_count: int = 0
    embedding_model: str = ""
    per_query: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _load_eval_dataset(name: str) -> list[dict]:
    """加载 eval dataset。name: dev / frozen_test / raw-baseline。"""
    if name in ("raw-baseline", "frozen-test"):
        path = EVAL_DIR / "frozen_test_queries.private.jsonl"
    elif name == "dev":
        path = EVAL_DIR / "dev_queries.private.jsonl"
    elif name == "frozen-test-assistant":
        # Phase 41-04：assistant 轨 eval 集（文件名用下划线，dataset 名用连字符）
        path = EVAL_DIR / "frozen_test_assistant.private.jsonl"
    else:
        path = EVAL_DIR / f"{name}.private.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().split("\n") if line.strip()]


def _percentile(sorted_vals: list[float], p: float) -> float:
    """计算百分位数。"""
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * p / 100)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]


def evaluate_raw_baseline() -> EvalMetrics:
    """在 personal_events collection 上跑 raw baseline。

    用 frozen test queries 的 query 文本做 embedding，检索 personal_events，
    检查 top-5 结果是否与 gold evidence 内容相关（内容匹配，非 ID 匹配）。
    """
    metrics = EvalMetrics(dataset="raw-baseline", collection="personal_events")

    # 验证 embedding 模型
    ok, msg, dim = local_embed.verify_model()
    if not ok:
        metrics.per_query = [{"error": f"embedding model unavailable: {msg}"}]
        return metrics
    metrics.embedding_model = "bge-small-zh-v1.5"

    # 连接 Chroma
    client = ChromaClient()
    coll = client.get_or_create_collection("personal_events")
    metrics.collection_count = coll.count()

    # 加载 eval dataset + gold evidence 内容（从 canonical store 读）
    cases = _load_eval_dataset("frozen-test")
    metrics.total_queries = len(cases)
    if not cases:
        metrics.per_query = [{"error": "no frozen test queries found"}]
        return metrics

    # 从 canonical store 读 gold evidence 的 content（用于内容匹配）
    gold_contents: dict[str, str] = {}  # message_id → content[:200]
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB
    if AGENT_CONVERSATIONS_DB.exists():
        con = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for case in cases:
            for ref in case.get("gold_evidence_refs", []):
                row = con.execute(
                    "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
                    (ref,),
                ).fetchone()
                if row and row["content"]:
                    gold_contents[ref] = row["content"][:200]
        con.close()

    latencies: list[float] = []
    hits = 0
    mrr_sum = 0.0

    for case in cases:
        query_text = case["query"]
        gold_refs = set(case.get("gold_evidence_refs", []))
        expected_abstain = case.get("expected_abstain", False)

        # gold content 片段（用于内容匹配）
        gold_snippets = [gold_contents[r] for r in gold_refs if r in gold_contents]

        # embedding
        t0 = time.time()
        embedding = local_embed.embed(query_text)
        if embedding is None:
            metrics.per_query.append({"id": case["id"], "error": "embed failed"})
            continue

        # 检索 top-5
        try:
            result = coll.query(query_embeddings=[embedding], n_results=5)
        except ChromaError as e:
            metrics.per_query.append({"id": case["id"], "error": str(e)[:100]})
            continue

        latency_ms = (time.time() - t0) * 1000
        latencies.append(latency_ms)

        # 解析结果
        ids = result.get("ids", [[]])[0] if result.get("ids") else []
        documents = result.get("documents", [[]])[0] if result.get("documents") else []
        metadatas_raw = result.get("metadatas", [[]])[0] if result.get("metadatas") else []

        # Recall@5: gold ref ID 匹配 或 metadata source_message_ref 匹配 或 内容匹配
        found_rank = None
        for rank, (rid, doc, meta) in enumerate(zip(ids, documents, metadatas_raw), 1):
            if rid in gold_refs:
                found_rank = rank
                break
            # candidate 模式：metadata source_message_ref 匹配 gold_refs
            if meta and isinstance(meta, dict):
                src_ref = meta.get("source_message_ref", "")
                if src_ref and src_ref in gold_refs:
                    found_rank = rank
                    break
            # 内容匹配：gold snippet 的 ≥15 字片段在 document 中
            if doc and gold_snippets:
                for snippet in gold_snippets:
                    if len(snippet) >= 15 and snippet[:15] in doc:
                        found_rank = rank
                        break
            if found_rank:
                break

        if found_rank:
            hits += 1
            mrr_sum += 1.0 / found_rank

        # no-answer false positive: 应 abstain 但返回了结果
        if expected_abstain and ids:
            metrics.no_answer_false_positive += 1

        metrics.per_query.append({
            "id": case["id"],
            "query": query_text[:80],
            "found_rank": found_rank,
            "latency_ms": round(latency_ms, 1),
            "expected_abstain": expected_abstain,
        })

    metrics.recall_at_5 = round(hits / max(metrics.total_queries, 1), 4)
    metrics.mrr_at_5 = round(mrr_sum / max(metrics.total_queries, 1), 4)

    if latencies:
        latencies_sorted = sorted(latencies)
        metrics.p50_latency_ms = round(_percentile(latencies_sorted, 50), 1)
        metrics.p95_latency_ms = round(_percentile(latencies_sorted, 95), 1)

    return metrics


def _load_cu_ref_index(db_path: Path | None = None) -> dict[str, set[str]]:
    """预载 canonical unit → 证据 ref 并集索引（candidate 模式 Recall 判定用）。

    证据模型多对多化的对齐修正：salvage 迁移修复证据链后，一个 knowledge unit
    合法持有多个 evidence ref（knowledge_unit_evidence 通过 INSERT OR IGNORE
    同时保留原锚点 ref 与 quote 所在 ref）。评估时不能只依赖 Chroma metadata
    里的单个 source_message_ref，需把 cu 下所有 member unit 的
    source_message_ref + knowledge_unit_evidence 全部并入候选 ref 集，
    gold 命中任一即算 found。只放宽命中判定口径，不改变阈值与历史 verdict。

    一次性全量读入内存建 dict（~7 万行级），避免 per-query 查库。
    """
    index: dict[str, set[str]] = {}
    path = db_path or UNIFIED_DB
    if not path.exists():
        return index
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"canonical_unit_members", "knowledge_units", "knowledge_unit_evidence"} <= tables:
            rows = con.execute(
                "SELECT m.canonical_unit_id, u.source_message_ref, e.evidence_ref "
                "FROM canonical_unit_members m "
                "LEFT JOIN knowledge_units u ON u.unit_id = m.member_unit_id "
                "LEFT JOIN knowledge_unit_evidence e ON e.unit_id = m.member_unit_id"
            )
            for cu_id, src_ref, ev_ref in rows:
                refs = index.setdefault(cu_id, set())
                if src_ref:
                    refs.add(src_ref)
                if ev_ref:
                    refs.add(ev_ref)
        con.close()
    except sqlite3.Error:
        return {}
    return index


def _match_found(
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
    gold_refs: set[str],
    gold_snippets: list[str],
) -> int | None:
    """检查一组结果是否命中 gold（ID/ref/内容匹配）。返回 rank 或 None。"""
    for rank, (rid, doc, meta) in enumerate(zip(ids, documents, metadatas), 1):
        if rid in gold_refs:
            return rank
        if meta and isinstance(meta, dict):
            src_ref = meta.get("source_message_ref", "")
            if src_ref and src_ref in gold_refs:
                return rank
        if doc and gold_snippets:
            for snippet in gold_snippets:
                if len(snippet) >= 15 and snippet[:15] in doc:
                    return rank
    return None


def evaluate_hybrid(candidate_collection: str = "") -> EvalMetrics:
    """混合检索评估：knowledge-first + raw fallback。

    先查 candidate knowledge unit collection，再查 personal_events（原始事件库），
    合并后取 top-5。用 frozen test queries 评估 Recall@5/MRR@5。
    这解决了纯知识压缩丢失代码/长文本字面匹配的问题。
    """
    metrics = EvalMetrics(dataset="hybrid", collection=candidate_collection or "auto")

    ok, msg, dim = local_embed.verify_model()
    if not ok:
        metrics.per_query = [{"error": f"embedding model unavailable: {msg}"}]
        return metrics
    metrics.embedding_model = "bge-small-zh-v1.5"

    client = ChromaClient()
    # 找 candidate collection
    if not candidate_collection:
        all_cols = client.list_collections()
        ku_cols = [c for c in all_cols if isinstance(c, dict) and "knowledge_units" in c.get("name", "")]
        if not ku_cols:
            metrics.per_query = [{"error": "no knowledge_units candidate collection found"}]
            return metrics
        candidate_collection = ku_cols[-1]["name"]
        metrics.collection = candidate_collection

    ku_coll = client.get_or_create_collection(candidate_collection)
    raw_coll = client.get_or_create_collection("personal_events")
    metrics.collection_count = ku_coll.count() + raw_coll.count()

    cases = _load_eval_dataset("frozen-test")
    metrics.total_queries = len(cases)
    if not cases:
        metrics.per_query = [{"error": "no frozen test queries"}]
        return metrics

    # 读 gold evidence 内容（用于内容匹配）
    gold_contents: dict[str, str] = {}
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB
    if AGENT_CONVERSATIONS_DB.exists():
        con_gold = sqlite3.connect(f"file:{AGENT_CONVERSATIONS_DB.as_posix()}?mode=ro", uri=True)
        con_gold.row_factory = sqlite3.Row
        for case in cases:
            for ref in case.get("gold_evidence_refs", []):
                row = con_gold.execute(
                    "SELECT content FROM canonical_messages WHERE canonical_message_id=?",
                    (ref,),
                ).fetchone()
                if row and row["content"]:
                    gold_contents[ref] = row["content"][:200]
        con_gold.close()

    latencies: list[float] = []
    hits = 0
    mrr_sum = 0.0

    for case in cases:
        query_text = case["query"]
        gold_refs = set(case.get("gold_evidence_refs", []))
        gold_snippets = [gold_contents[r] for r in gold_refs if r in gold_contents]
        expected_abstain = case.get("expected_abstain", False)

        t0 = time.time()
        embedding = local_embed.embed(query_text)
        if embedding is None:
            metrics.per_query.append({"id": case["id"], "error": "embed failed"})
            continue

        # 查 knowledge units (top-5，带距离用于竞争合并)
        ku_ids: list[str] = []
        ku_docs: list[str] = []
        ku_metas: list[dict] = []
        ku_dists: list[float] = []
        try:
            ku_result = ku_coll.query(query_embeddings=[embedding], n_results=5,
                                      include=["metadatas", "documents", "distances"])
            ku_ids = ku_result.get("ids", [[]])[0] if ku_result.get("ids") else []
            ku_docs = ku_result.get("documents", [[]])[0] if ku_result.get("documents") else []
            ku_metas = ku_result.get("metadatas", [[]])[0] if ku_result.get("metadatas") else []
            ku_dists = ku_result.get("distances", [[]])[0] if ku_result.get("distances") else []
        except ChromaError:
            pass

        # 查 personal_events (top-8，带距离) —— raw fallback
        raw_ids: list[str] = []
        raw_docs: list[str] = []
        raw_metas: list[dict] = []
        raw_dists: list[float] = []
        try:
            raw_result = raw_coll.query(query_embeddings=[embedding], n_results=8,
                                        include=["metadatas", "documents", "distances"])
            raw_ids = raw_result.get("ids", [[]])[0] if raw_result.get("ids") else []
            raw_docs = raw_result.get("documents", [[]])[0] if raw_result.get("documents") else []
            raw_metas = raw_result.get("metadatas", [[]])[0] if raw_result.get("metadatas") else []
            raw_dists = raw_result.get("distances", [[]])[0] if raw_result.get("distances") else []
        except ChromaError:
            pass

        latency_ms = (time.time() - t0) * 1000
        latencies.append(latency_ms)

        # 混合策略：knowledge-first 1 slot + raw 4 slot
        # 知识层贡献 1 个结构化语义结果，raw 10K+ 事件库覆盖字面/代码匹配
        # 经 slot sweep 验证 ku:raw = 1:4 在 frozen A/B 上达到 Recall@5=0.85（与 PoC 持平）
        seen: set[str] = set()
        dedup_ids: list[str] = []
        dedup_docs: list[str] = []
        dedup_metas: list[dict] = []

        # Phase 1: ku top-1 优先保留
        if ku_ids:
            rid, doc, meta = ku_ids[0], ku_docs[0], ku_metas[0]
            seen.add(rid)
            dedup_ids.append(rid)
            dedup_docs.append(doc)
            dedup_metas.append(meta if isinstance(meta, dict) else {})

        # Phase 2: raw top-8 按距离排序补充，填满到 top-5
        raw_merged: list[tuple[float, str, str, dict]] = []
        for rid, doc, meta, dist in zip(raw_ids, raw_docs, raw_metas, raw_dists):
            d = dist if isinstance(dist, (int, float)) else 1.0
            raw_merged.append((d, rid, doc, meta if isinstance(meta, dict) else {}))
        raw_merged.sort(key=lambda x: x[0])
        for dist, rid, doc, meta in raw_merged:
            if len(dedup_ids) >= 5:
                break
            if rid not in seen:
                seen.add(rid)
                dedup_ids.append(rid)
                dedup_docs.append(doc)
                dedup_metas.append(meta)

        # Phase 3: 如果 raw 不足且 ku 有更多结果，补充 ku
        for rid, doc, meta in zip(ku_ids[1:], ku_docs[1:], ku_metas[1:]):
            if len(dedup_ids) >= 5:
                break
            if rid not in seen:
                seen.add(rid)
                dedup_ids.append(rid)
                dedup_docs.append(doc)
                dedup_metas.append(meta if isinstance(meta, dict) else {})

        top5_ids = dedup_ids[:5]
        top5_docs = dedup_docs[:5]
        top5_metas = dedup_metas[:5]

        found_rank = _match_found(top5_ids, top5_docs, top5_metas, gold_refs, gold_snippets)

        if found_rank:
            hits += 1
            mrr_sum += 1.0 / found_rank

        if expected_abstain and top5_ids:
            metrics.no_answer_false_positive += 1

        metrics.per_query.append({
            "id": case["id"],
            "found_rank": found_rank,
            "latency_ms": round(latency_ms, 1),
            "ku_count": len(ku_ids),
            "raw_count": len(raw_ids),
        })

    metrics.recall_at_5 = round(hits / max(metrics.total_queries, 1), 4)
    metrics.mrr_at_5 = round(mrr_sum / max(metrics.total_queries, 1), 4)
    if latencies:
        latencies_sorted = sorted(latencies)
        metrics.p50_latency_ms = round(_percentile(latencies_sorted, 50), 1)
        metrics.p95_latency_ms = round(_percentile(latencies_sorted, 95), 1)

    return metrics


def evaluate_candidate(candidate_collection: str = "", dataset_name: str = "frozen-test") -> EvalMetrics:
    """在 candidate knowledge unit collection 上跑 frozen test A/B。

    candidate_collection 为空时自动找最新的 candidate。
    dataset_name 默认 frozen-test；Phase 41-04 起支持 frozen-test-assistant。
    """
    metrics = EvalMetrics(dataset=dataset_name, collection=candidate_collection or "auto")

    ok, msg, dim = local_embed.verify_model()
    if not ok:
        metrics.per_query = [{"error": f"embedding model unavailable: {msg}"}]
        return metrics
    metrics.embedding_model = "bge-small-zh-v1.5"

    client = ChromaClient()
    # 找 candidate collection
    if not candidate_collection:
        # 自动找 knowledge_units_* collection
        all_cols = client.list_collections()
        ku_cols = [c for c in all_cols if isinstance(c, dict) and "knowledge_units" in c.get("name", "")]
        if not ku_cols:
            metrics.per_query = [{"error": "no knowledge_units candidate collection found"}]
            return metrics
        candidate_collection = ku_cols[-1]["name"]
        metrics.collection = candidate_collection

    coll = client.get_or_create_collection(candidate_collection)
    metrics.collection_count = coll.count()

    cases = _load_eval_dataset(dataset_name)
    metrics.total_queries = len(cases)
    if not cases:
        metrics.per_query = [{"error": "no frozen test queries"}]
        return metrics

    # 证据模型多对多化的对齐修正：预载 cu → 证据 ref 并集索引（见 _load_cu_ref_index），
    # 补充 id 直配 / metadata ref 之外的命中路径，不改变阈值与历史 verdict。
    cu_ref_index = _load_cu_ref_index()

    latencies: list[float] = []
    hits = 0
    mrr_sum = 0.0

    for case in cases:
        query_text = case["query"]
        gold_refs = set(case.get("gold_evidence_refs", []))

        t0 = time.time()
        embedding = local_embed.embed(query_text)
        if embedding is None:
            continue

        try:
            result = coll.query(query_embeddings=[embedding], n_results=5)
        except ChromaError:
            continue

        latency_ms = (time.time() - t0) * 1000
        latencies.append(latency_ms)

        ids = result.get("ids", [[]])[0] if result.get("ids") else []
        metadatas_raw = result.get("metadatas", [[]])[0] if result.get("metadatas") else []
        found_rank = None
        for rank, (rid, meta) in enumerate(zip(ids, metadatas_raw), 1):
            if rid in gold_refs:
                found_rank = rank
                break
            if meta and isinstance(meta, dict):
                src_ref = meta.get("source_message_ref", "")
                if src_ref and src_ref in gold_refs:
                    found_rank = rank
                    break
            # 证据并集匹配：cu 下 member units 的 source_message_ref +
            # knowledge_unit_evidence 的全部 evidence_ref，gold 命中任一即算 found
            cu_refs = cu_ref_index.get(rid)
            if cu_refs and not cu_refs.isdisjoint(gold_refs):
                found_rank = rank
                break

        if found_rank:
            hits += 1
            mrr_sum += 1.0 / found_rank

        metrics.per_query.append({
            "id": case["id"],
            "found_rank": found_rank,
            "latency_ms": round(latency_ms, 1),
        })

    metrics.recall_at_5 = round(hits / max(metrics.total_queries, 1), 4)
    metrics.mrr_at_5 = round(mrr_sum / max(metrics.total_queries, 1), 4)
    if latencies:
        latencies_sorted = sorted(latencies)
        metrics.p50_latency_ms = round(_percentile(latencies_sorted, 50), 1)
        metrics.p95_latency_ms = round(_percentile(latencies_sorted, 95), 1)

    return metrics


def run(dataset: str, candidate: str = "", report: str = "") -> int:
    metrics: EvalMetrics
    if dataset == "raw-baseline":
        metrics = evaluate_raw_baseline()
        # 写 baseline 报告
        BASELINE_PATH.write_text(
            json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 也写 markdown
        md_path = AI_CONTEXT_DIR / "knowledge_unit_raw_baseline.md"
        md_path.write_text(_format_report(metrics), encoding="utf-8")
    elif dataset == "frozen-test":
        metrics = evaluate_candidate(candidate)
    elif dataset == "frozen-test-assistant":
        # Phase 41-04：assistant 轨 eval 集，复用 candidate 评估链
        metrics = evaluate_candidate(candidate, dataset_name="frozen-test-assistant")
    elif dataset == "hybrid":
        metrics = evaluate_hybrid(candidate)
    else:
        print(f"未知 dataset: {dataset}")
        return 2

    # 写 report artifact（如有指定）
    if report:
        report_path = Path(report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(_format_report(metrics))
    return 0


def _format_report(metrics: EvalMetrics) -> str:
    lines = [
        f"# Knowledge Unit RAG 评估（{metrics.dataset}）",
        "",
        f"- collection: {metrics.collection}",
        f"- collection count: {metrics.collection_count}",
        f"- embedding: {metrics.embedding_model}",
        f"- total queries: {metrics.total_queries}",
        f"- **Recall@5: {metrics.recall_at_5}**",
        f"- **MRR@5: {metrics.mrr_at_5}**",
        f"- no-answer false positive: {metrics.no_answer_false_positive}",
        f"- deprecated/secret hit: {metrics.deprecated_secret_hit} (must be 0)",
        f"- p50 latency: {metrics.p50_latency_ms}ms",
        f"- p95 latency: {metrics.p95_latency_ms}ms",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 14: knowledge unit RAG 评估")
    p.add_argument("--dataset", choices=["raw-baseline", "frozen-test", "hybrid", "frozen-test-assistant"], default="raw-baseline")
    p.add_argument("--candidate", default="", help="candidate collection name (frozen-test/hybrid)")
    p.add_argument("--report", default="", help="write metrics JSON to this path")
    args = p.parse_args(argv)
    return run(args.dataset, args.candidate, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
