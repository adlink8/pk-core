"""Primary knowledge-unit layer — top of the hybrid retrieval chain.

Port of the "Phase 1: 知识层检索" block from semantic_search.search_knowledge_units.
This layer is not part of the fallback chain; the assembler runs it first,
then feeds the remaining slots through fallback_policy.build_fallback_chain.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from personal_knowledge.retrieval import _constants as _C
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState
from personal_knowledge.retrieval.relevance import annotate_candidate_support
from personal_knowledge.retrieval.semantic_cards import _tokenize


class KnowledgeUnitLayer(RetrieverLayer):
    """Current-KU retrieval: Chroma active collection, then SQLite fallback.

    Chroma remains the preferred vector path. When the active collection is
    empty, missing, or unreachable, current rows in UNIFIED_DB are searched
    read-only. This layer never rewrites the active pointer.
    """

    layer_name = "knowledge_unit"
    role = "knowledge_retrieval"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        results = self._from_chroma(query, state)
        if results:
            return results
        if state.policy != "layered":
            state.route = "fallback_raw"
            return []
        sqlite_hits = self._from_sqlite(query, state)
        if sqlite_hits:
            state.route = "knowledge"
            return sqlite_hits
        state.route = "fallback_raw"
        return []

    def _from_chroma(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        ku_collection = state.ku_collection
        if not ku_collection or state.client is None or state.embedding is None:
            return results

        client = state.client
        try:
            cols = client.list_collections()
        except Exception:
            return results
        col_names = {c if isinstance(c, str) else c.get("name", "") for c in cols}
        if ku_collection not in col_names:
            return results

        try:
            ku_coll = client.get_or_create_collection(ku_collection)
            ku_fetch = max(state.top_k, _C._KU_SLOTS)
            kr = ku_coll.query(
                query_embeddings=[state.embedding], n_results=ku_fetch,
                include=["metadatas", "documents", "distances"],
            )
        except Exception:
            return results
        ku_ids = kr.get("ids", [[]])[0] if kr.get("ids") else []
        ku_docs = kr.get("documents", [[]])[0] if kr.get("documents") else []
        ku_dists = kr.get("distances", [[]])[0] if kr.get("distances") else []
        ku_metas = kr.get("metadatas", [[]])[0] if kr.get("metadatas") else []

        resolve_support_ref = state.resolve_support_ref
        for uid, doc, dist, meta in zip(ku_ids, ku_docs, ku_dists, ku_metas):
            lc = meta.get("lifecycle", "current") if isinstance(meta, dict) else "current"
            if lc not in ("current",):
                continue
            item: dict[str, Any] = {
                "unit_id": uid,
                "subject": meta.get("subject", "") if isinstance(meta, dict) else "",
                "answer": doc[:300] if doc else "",
                "score": round(1 - dist, 4) if isinstance(dist, (int, float)) else 0,
                "lifecycle": lc,
                "confidence": meta.get("confidence", 0) if isinstance(meta, dict) else 0,
                "source_message_ref": meta.get("source_message_ref", "") if isinstance(meta, dict) else "",
                "collection": ku_collection,
                "retrieval_unit": "knowledge_unit",
                "rank_reason": "knowledge unit semantic match",
            }
            if state.include_evidence:
                item["evidence_quote"] = ""
            decision = annotate_candidate_support(
                query,
                item,
                resolve=lambda ref: resolve_support_ref(item, ref),
            )
            if decision.state == "unsupported":
                state.ku_abstained += 1
                continue
            results.append(item)
            if len(results) >= _C._KU_SLOTS:
                break

        # 读版本信息 (read index version row for telemetry/response).
        try:
            con = sqlite3.connect(f"file:{_C.UNIFIED_DB.as_posix()}?mode=ro", uri=True)
            row = con.execute(
                "SELECT version_id, build_id, canonical_build_id, unit_count, status "
                "FROM knowledge_index_versions WHERE collection_name=? ORDER BY created_at DESC LIMIT 1",
                (ku_collection,),
            ).fetchone()
            if row:
                state.versions = {
                    "index_version": row[0], "build_id": row[1],
                    "canonical_build_id": row[2], "unit_count": row[3], "status": row[4],
                }
            con.close()
        except Exception:
            pass
        return results

    def _from_sqlite(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        needles = _tokenize(query)
        if not needles:
            return []
        need = min(_C._KU_SLOTS, max(1, state.remaining()))
        db = _C.UNIFIED_DB
        if not db.exists():
            return []
        like = [f"%{n.replace('%', '').replace('_', '')}%" for n in needles]
        where = " OR ".join(
            "(subject LIKE ? OR question LIKE ? OR answer LIKE ?)" for _ in like
        )
        params: list[str] = []
        for pattern in like:
            params.extend((pattern, pattern, pattern))
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT unit_id, subject, answer, confidence, lifecycle, "
                "COALESCE(source_message_ref,'') AS source_message_ref "
                "FROM knowledge_units "
                f"WHERE lifecycle='current' AND status='current' AND ({where}) "
                "LIMIT ?",
                (*params, need * 8),
            ).fetchall()
            con.close()
        except Exception:
            return []
        scored: list[dict[str, Any]] = []
        for row in rows:
            haystack = f"{row['subject']} {row['answer']}".lower()
            score = sum(haystack.count(n.lower()) for n in needles)
            if score <= 0:
                continue
            item = {
                "unit_id": str(row["unit_id"]),
                "subject": str(row["subject"] or ""),
                "answer": str(row["answer"] or "")[:300],
                "score": float(score),
                "lifecycle": "current",
                "confidence": row["confidence"] or 0,
                "source_message_ref": str(row["source_message_ref"] or ""),
                "collection": "knowledge_units_sqlite",
                "retrieval_unit": "knowledge_unit",
                "rank_reason": "knowledge unit sqlite current match",
            }
            decision = annotate_candidate_support(query, item, resolve=None)
            if decision.state == "unsupported":
                continue
            scored.append(item)
        scored.sort(key=lambda item: (-float(item["score"]), str(item["unit_id"])))
        return scored[:need]
