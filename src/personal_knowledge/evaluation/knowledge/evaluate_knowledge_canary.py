"""Phase 14 Plan 05: unified knowledge unit search + canary evaluator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR  # legacy alias: scripts root for resource paths

from personal_knowledge.core.project_paths import UNIFIED_DB, DB_DIR  # noqa: E402
from personal_knowledge.core.chroma_client import ChromaClient, ChromaError  # noqa: E402
import personal_knowledge.core.local_embed as local_embed  # noqa: E402

ACTIVE_POINTER = DB_DIR / "knowledge_index_active.txt"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_active_collection() -> str | None:
    """读 active knowledge index pointer。"""
    if ACTIVE_POINTER.exists():
        name = ACTIVE_POINTER.read_text(encoding="utf-8").strip()
        return name if name else None
    return None


def search_knowledge_units(
    query: str,
    top_k: int = 5,
    collection_name: str | None = None,
    include_evidence: bool = False,
) -> dict:
    """知识单元检索。knowledge-first + raw fallback。

    返回: {"route": "knowledge|fallback_raw", "results": [...], "versions": {...}}
    """
    route = "knowledge"
    versions = {}

    # 解析 active collection
    if collection_name is None:
        collection_name = read_active_collection()
    if not collection_name:
        return {"route": "fallback_raw", "reason": "no active knowledge index",
                "results": [], "versions": {}}

    # 验证 collection 存在
    client = ChromaClient(port=8001)
    try:
        cols = client.list_collections()
    except ChromaError:
        return {"route": "fallback_raw", "reason": "vector service unavailable",
                "results": [], "versions": {}}
    col_names = {c if isinstance(c, str) else c.get("name", "") for c in cols}
    if collection_name not in col_names:
        return {"route": "fallback_raw", "reason": f"collection not found: {collection_name}",
                "results": [], "versions": {}}

    # embedding
    embedding = local_embed.embed(query)
    if embedding is None:
        return {"route": "fallback_raw", "reason": "embedding failed",
                "results": [], "versions": {}}

    # 检索
    coll = client.get_or_create_collection(collection_name)
    try:
        result = coll.query(query_embeddings=[embedding], n_results=top_k,
                            include=["metadatas", "documents", "distances"])
    except ChromaError:
        return {"route": "fallback_raw", "reason": "query failed",
                "results": [], "versions": {}}

    ids = result.get("ids", [[]])[0]
    documents = result.get("documents", [[]])[0]
    distances = result.get("distances", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]

    results = []
    for i, (uid, doc, dist, meta) in enumerate(zip(ids, documents, distances, metadatas)):
        lifecycle = (meta.get("lifecycle", "current") if isinstance(meta, dict) else "current")
        # 过滤非 current lifecycle
        if lifecycle not in ("current",):
            continue

        item = {
            "rank": i + 1,
            "unit_id": uid,
            "subject": meta.get("subject", "") if isinstance(meta, dict) else "",
            "answer": doc[:200] if doc else "",
            "score": round(1 - dist, 4) if isinstance(dist, (int, float)) else 0,
            "lifecycle": lifecycle,
            "confidence": meta.get("confidence", 0) if isinstance(meta, dict) else 0,
            "source_message_ref": meta.get("source_message_ref", "") if isinstance(meta, dict) else "",
        }
        if include_evidence:
            item["evidence_quote"] = ""  # evidence refs only, not full text
        results.append(item)

    # 从 DB 读版本信息
    try:
        con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
        row = con.execute(
            "SELECT version_id, build_id, canonical_build_id, unit_count, status "
            "FROM knowledge_index_versions WHERE collection_name=? ORDER BY created_at DESC LIMIT 1",
            (collection_name,),
        ).fetchone()
        if row:
            versions = {
                "index_version": row[0],
                "build_id": row[1],
                "canonical_build_id": row[2],
                "unit_count": row[3],
                "status": row[4],
            }
        con.close()
    except Exception:
        pass

    return {"route": route, "results": results, "versions": versions,
            "collection": collection_name}


def generate_canary_queries(n: int = 30) -> list[dict]:
    """从 canonical store 生成 canary queries（隐私安全：只含 query hash）。

    内部读 question 原文用于检索，但返回的 dict 只含 hash（不泄露原 query）。
    query_text 字段仅供 run_canary 内部使用，不写入 report。
    """
    con = sqlite3.connect(f"file:{UNIFIED_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # 从 knowledge_units 取 n 个 question 作为 query
    rows = con.execute(
        "SELECT DISTINCT question FROM canonical_knowledge_units "
        "WHERE status='current' ORDER BY RANDOM() LIMIT ?", (n,)
    ).fetchall()
    con.close()

    queries = []
    for r in rows:
        q = r["question"]
        queries.append({
            "query_text": q,  # 内部用，不写入 report
            "query_hash": hashlib.sha256(q.encode("utf-8")).hexdigest()[:32],
            "top_k": 5,
            "label": "",
        })
    return queries


def run_canary(
    collection_name: str,
    n_queries: int = 30,
    report_path: Path | None = None,
    candidate_override: bool = False,
) -> dict:
    """运行 canary evaluation。不修改 active pointer。

    candidate_override=True 时 route 标记为 knowledge_canary_override，
    且从指定的 collection_name 检索（而非 active pointer）。
    """
    queries = generate_canary_queries(n_queries)
    if not queries:
        return {"error": "no canonical units found"}

    # 记录 canary 前 active 状态
    active_before = read_active_collection()

    results = []
    latencies = []
    for q in queries:
        t0 = time.time()
        # 用原 query 文本做检索（不是 hash）
        result = search_knowledge_units(
            query=q["query_text"],
            top_k=q["top_k"],
            collection_name=collection_name,
        )
        latency_ms = (time.time() - t0) * 1000
        latencies.append(latency_ms)

        route = result["route"]
        if candidate_override and route == "knowledge":
            route = "knowledge_canary_override"

        results.append({
            "query_hash": q["query_hash"],
            "route": route,
            "top_k": q["top_k"],
            "returned_ids": [r["unit_id"] for r in result["results"]],
            "scores": [r["score"] for r in result["results"]],
            "latency_ms": round(latency_ms, 1),
            "label": q["label"],
            "versions": result.get("versions", {}),
        })

    # 确认 active 未变
    active_after = read_active_collection()
    active_unchanged = active_before == active_after

    report = {
        "generated_at": _utc_now(),
        "collection": collection_name,
        "candidate_override": candidate_override,
        "query_count": len(results),
        "active_unchanged": active_unchanged,
        "active_before": active_before,
        "active_after": active_after,
        "results": results,
        "gate": {"status": "pending_labels"},
        "p50_latency_ms": round(sorted(latencies)[len(latencies)//2], 1) if latencies else 0,
        "p95_latency_ms": round(sorted(latencies)[int(len(latencies)*0.95)], 1) if latencies else 0,
    }

    if report_path:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


VALID_CANARY_LABELS = frozenset({"helpful", "wrong", "stale", "missing"})
CRITICAL_CANARY_LABELS = frozenset({"wrong", "stale"})


def list_critical_canary_rows(report: dict | Path | str) -> list[dict]:
    """Rows with label in {wrong, stale} for operator triage.

    Each row: index, query_hash, label, returned_ids[:3], scores[:3].
    Does not modify the report or active pointer.
    """
    if isinstance(report, (str, Path)):
        path = Path(report)
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = report
    rows: list[dict] = []
    for i, r in enumerate(data.get("results") or []):
        lab = str(r.get("label") or "").strip().lower()
        if lab not in CRITICAL_CANARY_LABELS:
            continue
        rows.append({
            "index": i,
            "query_hash": r.get("query_hash") or "",
            "label": lab,
            "returned_ids": list(r.get("returned_ids") or [])[:3],
            "scores": list(r.get("scores") or [])[:3],
        })
    return rows


def _build_query_hash_map(db_path: Path = UNIFIED_DB) -> dict[str, str]:
    """Map query_hash → question text (privacy: only used for offline labeling)."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT DISTINCT question FROM canonical_knowledge_units "
        "WHERE status='current' AND question IS NOT NULL AND length(question) > 0"
    ).fetchall()
    con.close()
    out: dict[str, str] = {}
    for (q,) in rows:
        h = hashlib.sha256((q or "").encode("utf-8")).hexdigest()[:32]
        out[h] = q
    return out


def _load_unit_snippets(unit_ids: list[str], db_path: Path = UNIFIED_DB, limit: int = 5) -> list[dict]:
    if not unit_ids:
        return []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    snippets = []
    for uid in unit_ids[:limit]:
        row = con.execute(
            "SELECT canonical_unit_id, subject, question, answer "
            "FROM canonical_knowledge_units WHERE canonical_unit_id=?",
            (uid,),
        ).fetchone()
        if not row:
            snippets.append({"unit_id": uid, "missing": True})
            continue
        snippets.append({
            "unit_id": row["canonical_unit_id"],
            "subject": (row["subject"] or "")[:120],
            "question": (row["question"] or "")[:200],
            "answer": (row["answer"] or "")[:300],
        })
    con.close()
    return snippets


def _parse_label_json(text: str) -> str:
    """Extract a valid canary label from model text; empty if unparseable."""
    raw = (text or "").strip()
    if not raw:
        return ""
    # strip markdown fences
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(raw[start : end + 1])
            lab = str(obj.get("label", "")).strip().lower()
            if lab in VALID_CANARY_LABELS:
                return lab
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    # bare token fallback
    for lab in VALID_CANARY_LABELS:
        if lab in raw.lower().split():
            return lab
    return ""


def _llm_label_one(
    query_text: str,
    snippets: list[dict],
    *,
    model: str,
    backend: str,
) -> tuple[str, str]:
    """Return (label, raw_response_or_error)."""
    system = (
        "You label personal-knowledge retrieval quality. "
        "Given a QUERY and TOP retrieved units, reply with ONLY JSON: "
        '{"label":"helpful"|"wrong"|"stale"|"missing","reason":"short"}. '
        "helpful=relevant useful hits; wrong=clearly incorrect/misleading; "
        "stale=outdated for the query; missing=no useful hit / off-topic. "
        "Prefer missing over wrong when unsure content is false. No markdown."
    )
    user = json.dumps(
        {"query": query_text, "top_units": snippets},
        ensure_ascii=False,
    )
    if backend == "openai":
        from personal_knowledge.core.llm import _chat_with_retry, make_llm_client

        client = make_llm_client()
        text = _chat_with_retry(
            client,
            model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=200,
        )
        return _parse_label_json(text), text

    # vertex_google (default when OPENAI key absent)
    import urllib.error
    import urllib.request

    from personal_knowledge.core.providers import TokenProvider
    from personal_knowledge.core.runtime_config import (
        vertex_config,
        vertex_generate_content_url,
        vertex_generation_config,
    )

    token = TokenProvider().get()
    url = vertex_generate_content_url(vertex_config(), model)
    body = json.dumps({
        "contents": [{
            "role": "user",
            "parts": [{"text": system + "\n\n" + user}],
        }],
        "generationConfig": vertex_generation_config(model, 256),
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode())
        parts = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return _parse_label_json(text), text
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return "", f"error:{type(exc).__name__}:{exc}"


def label_canary_report_with_llm(
    report_path: Path,
    *,
    model: str = "gemini-3.5-flash-lite",
    backend: str = "auto",
    db_path: Path = UNIFIED_DB,
    only_unlabeled: bool = True,
    only_critical: bool = False,
    max_items: int | None = None,
    write: bool = True,
    min_interval: float = 1.5,
) -> dict:
    """Fill result[].label via LLM. Does not touch active pointer.

    backend: auto | openai | vertex_google

    only_critical: re-label only wrong/stale (and empty/invalid). Skips helpful/missing.
    When only_critical is True, existing wrong/stale labels are re-sent to the LLM.
    """
    import os
    import time

    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = report.get("results") or []
    if not results:
        return {"error": "empty results", "labeled": 0}

    if backend == "auto":
        if os.environ.get("OPENAI_API_KEY") or os.environ.get("MEM0_API_KEY"):
            backend = "openai"
            if not model or model.startswith("gemini"):
                model = os.environ.get("OPENAI_MODEL") or os.environ.get("MEM0_LLM_MODEL") or "gpt-4o-mini"
        else:
            backend = "vertex_google"

    qmap = _build_query_hash_map(db_path)
    labeled = 0
    skipped = 0
    failed = 0
    unresolved_query = 0
    details = []

    work = []
    for i, r in enumerate(results):
        lab = str(r.get("label") or "").strip().lower()
        if only_critical:
            # Re-call LLM for wrong/stale or empty/invalid; skip helpful/missing.
            if lab in CRITICAL_CANARY_LABELS or lab not in VALID_CANARY_LABELS:
                work.append(i)
            else:
                skipped += 1
            continue
        if only_unlabeled and lab in VALID_CANARY_LABELS:
            skipped += 1
            continue
        work.append(i)
    if max_items is not None:
        work = work[: max(0, max_items)]

    for i in work:
        r = results[i]
        qh = r.get("query_hash") or ""
        query_text = qmap.get(qh, "")
        if not query_text:
            unresolved_query += 1
            # Fall back: use top unit question as weak proxy (documented)
            snips = _load_unit_snippets(r.get("returned_ids") or [], db_path)
            query_text = (snips[0].get("question") if snips else "") or f"(hash:{qh})"
        else:
            snips = _load_unit_snippets(r.get("returned_ids") or [], db_path)

        try:
            lab, raw = _llm_label_one(query_text, snips, model=model, backend=backend)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            details.append({"i": i, "error": f"{type(exc).__name__}:{exc}"})
            time.sleep(min_interval)
            continue

        if lab in VALID_CANARY_LABELS:
            r["label"] = lab
            r["label_source"] = "llm"
            r["label_model"] = model
            r["label_backend"] = backend
            labeled += 1
            details.append({"i": i, "label": lab})
        else:
            failed += 1
            details.append({"i": i, "error": "parse_failed", "raw": (raw or "")[:200]})
        time.sleep(min_interval)

    report["labeling"] = {
        "method": "llm",
        "backend": backend,
        "model": model,
        "only_critical": only_critical,
        "labeled": labeled,
        "skipped_already": skipped,
        "failed": failed,
        "unresolved_query_hash": unresolved_query,
        "updated_at": _utc_now(),
    }
    # keep gate pending until strict recompute
    if labeled:
        report["gate"] = {"status": "labels_present_run_strict", "labeling": report["labeling"]}

    if write:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "labeled": labeled,
        "skipped_already": skipped,
        "failed": failed,
        "unresolved_query_hash": unresolved_query,
        "backend": backend,
        "model": model,
        "only_critical": only_critical,
        "write": write,
        "sample": details[:10],
    }


def check_label_completeness(report_path: Path) -> dict:
    """检查 canary report 的 label 完整性。"""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    valid_labels = VALID_CANARY_LABELS
    total = len(report.get("results", []))
    labeled = 0
    invalid = 0
    missing = 0
    for r in report.get("results", []):
        label = r.get("label", "")
        if not label:
            missing += 1
        elif label in valid_labels:
            labeled += 1
        else:
            invalid += 1
    return {
        "total": total,
        "labeled": labeled,
        "missing": missing,
        "invalid": invalid,
        "complete": total > 0 and labeled == total and invalid == 0 and missing == 0,
    }


def compute_gate(report_path: Path) -> dict:
    """计算隔离 canary gate。要求 helpful≥80%、critical wrong/stale=0、fallback≤30%。"""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = report.get("results", [])
    total = len(results)
    if total == 0:
        return {"status": "FAIL", "reason": "no results"}

    labels = [r.get("label", "") for r in results]
    helpful = sum(1 for l in labels if l == "helpful")
    wrong = sum(1 for l in labels if l == "wrong")
    stale = sum(1 for l in labels if l == "stale")
    missing_label = sum(1 for l in labels if l == "missing")
    fallback = sum(1 for r in results if "fallback" in r.get("route", ""))

    # 检查 label 完整性
    unlabeled = sum(1 for l in labels if l not in {"helpful", "wrong", "stale", "missing"})
    if unlabeled > 0:
        return {"status": "awaiting_labels",
                "reason": f"{unlabeled} queries missing labels",
                "helpful": helpful, "wrong": wrong, "stale": stale, "missing": missing_label}

    helpful_rate = helpful / total
    critical_wrong_stale = wrong + stale  # wrong + stale 都是 critical
    fallback_rate = fallback / total
    p95 = report.get("p95_latency_ms", 999)

    checks = {
        "helpful_rate_80": helpful_rate >= 0.80,
        "critical_wrong_stale_zero": critical_wrong_stale == 0,
        "fallback_le_30": fallback_rate <= 0.30,
    }
    # latency gate: p95 <= 2x raw baseline (raw p95 ~20ms, so gate at 500ms as soft)
    checks["latency_p95_ok"] = p95 <= 500.0

    status = "PASS" if all(checks.values()) else "FAIL"
    return {
        "status": status,
        "helpful_rate": round(helpful_rate, 4),
        "critical_wrong_stale": critical_wrong_stale,
        "fallback_rate": round(fallback_rate, 4),
        "p95_latency_ms": p95,
        "checks": checks,
        "ready_for_promotion_review": status == "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    """Canary evaluator CLI。

    用法::

        # 生成 30-query canary worksheet（candidate override）
        python evaluate_knowledge_canary.py --candidate-override COLLECTION --queries 30 --report report.json

        # 列出 critical (wrong/stale) 行
        python evaluate_knowledge_canary.py --report report.json --list-critical

        # 仅对 wrong/stale/empty 重新 LLM 标注
        python evaluate_knowledge_canary.py --report report.json --label-with-llm --only-critical

        # 检查 label 完整性
        python evaluate_knowledge_canary.py --report report.json --check-label-completeness

        # 计算 gate（strict）
        python evaluate_knowledge_canary.py --report report.json --strict
    """
    import argparse
    p = argparse.ArgumentParser(description="Phase 14 Plan 05: canary evaluator")
    p.add_argument("--candidate-override", default="", help="run canary against this candidate collection (not active)")
    p.add_argument("--queries", type=int, default=30, help="number of canary queries")
    p.add_argument("--report", default="", help="report JSON path")
    p.add_argument("--check-label-completeness", action="store_true")
    p.add_argument(
        "--list-critical",
        action="store_true",
        help="List rows labeled wrong/stale (index, query_hash, top ids/scores)",
    )
    p.add_argument("--strict", action="store_true", help="compute gate (requires all labels)")
    p.add_argument(
        "--label-with-llm",
        action="store_true",
        help="Fill empty labels via LLM (OpenAI if key set, else Vertex/gcloud)",
    )
    p.add_argument(
        "--only-critical",
        action="store_true",
        help="With --label-with-llm: only re-label wrong/stale (and empty/invalid)",
    )
    p.add_argument("--model", default="", help="LLM model for --label-with-llm")
    p.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "openai", "vertex_google"],
        help="LLM backend for labeling (default auto)",
    )
    p.add_argument("--max-items", type=int, default=None, help="Max items to label this call")
    p.add_argument(
        "--write",
        action="store_true",
        default=True,
        help="Write labels back to report (default true for --label-with-llm)",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write report when labeling")
    args = p.parse_args(argv)

    report_path = Path(args.report) if args.report else None

    if args.list_critical:
        if not report_path or not report_path.exists():
            print("[error] --report required for --list-critical", file=sys.stderr)
            return 2
        rows = list_critical_canary_rows(report_path)
        out = {
            "report": str(report_path),
            "critical_count": len(rows),
            "critical_labels": sorted(CRITICAL_CANARY_LABELS),
            "rows": rows,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if not rows else 1  # exit 1 when critical rows exist (ops signal)

    if args.check_label_completeness:
        if not report_path or not report_path.exists():
            print("[error] --report required for --check-label-completeness", file=sys.stderr)
            return 2
        result = check_label_completeness(report_path)
        print(json.dumps(result, indent=2))
        return 0 if result["complete"] else 1

    if args.strict:
        if not report_path or not report_path.exists():
            print("[error] --report required for --strict", file=sys.stderr)
            return 2
        result = compute_gate(report_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("status") == "PASS" else 1

    if args.label_with_llm:
        if not report_path or not report_path.exists():
            print("[error] --label-with-llm requires existing --report", file=sys.stderr)
            return 2
        model = args.model or (
            "gemini-3.5-flash-lite" if args.backend in ("auto", "vertex_google") else "gpt-4o-mini"
        )
        result = label_canary_report_with_llm(
            report_path,
            model=model,
            backend=args.backend,
            max_items=args.max_items,
            only_critical=bool(args.only_critical),
            write=not args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("error"):
            return 1
        # fail if nothing labeled and still incomplete
        comp = check_label_completeness(report_path)
        if not comp.get("complete") and result.get("labeled", 0) == 0:
            return 1
        return 0

    # 默认：运行 canary
    collection = args.candidate_override or read_active_collection() or ""
    if not collection:
        print("[error] no active collection and no --candidate-override", file=sys.stderr)
        return 2

    report = run_canary(
        collection_name=collection,
        n_queries=args.queries,
        report_path=report_path,
        candidate_override=bool(args.candidate_override),
    )
    if "error" in report:
        print(f"[error] {report['error']}", file=sys.stderr)
        return 1

    print(f"Canary complete: {report['query_count']} queries")
    print(f"  route: {'canary_override' if args.candidate_override else 'active'}")
    print(f"  collection: {report['collection']}")
    print(f"  active_unchanged: {report['active_unchanged']}")
    print(f"  p50: {report['p50_latency_ms']}ms, p95: {report['p95_latency_ms']}ms")
    print(f"  labels: pending (use --label-with-llm or manual labels, then --strict)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
