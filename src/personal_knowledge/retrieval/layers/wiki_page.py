"""Wiki projection retrieval layer — compressed subject pages before KU.

Read-only over ``personal_wiki_projection.sqlite``. Missing or unreadable
stores degrade to no hits; this layer never writes the projection DB.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from personal_knowledge.retrieval import _constants as _C
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState
from personal_knowledge.retrieval.semantic_cards import _tokenize
from personal_knowledge.wiki.derived_store import connect_ro, latest_version
from personal_knowledge.wiki.materialization import dependency_manifest_checksum
from personal_knowledge.wiki.source_validation import current_claims, claim_evidence_refs
from personal_knowledge.wiki.page_reader import PAGE_BODY_SCHEMA, page_checksum


def _like_escape(needle: str) -> str:
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_wiki_pages(query: str, *, store: Path, limit: int) -> list[dict[str, Any]]:
    """Keyword-search latest wiki pages. Fail-closed to [] when the store is absent."""
    needles = _tokenize(query)
    if not needles or limit <= 0:
        return []
    target = Path(store)
    if not target.exists():
        return []
    try:
        con = connect_ro(target)
    except (FileNotFoundError, OSError, ValueError):
        return []
    try:
        escaped = [_like_escape(n) for n in needles]
        like_sql = " OR ".join("p.page_body LIKE ? ESCAPE '\\'" for _ in escaped)
        like_params = [f"%{e}%" for e in escaped]
        fetch = max(limit * 4, limit)
        rows = con.execute(
            f"""
            SELECT p.topic_id, p.topic_type, p.projection_version, p.page_body, p.page_checksum,
                   v.freshness_status, v.reason_codes_json
            FROM wiki_projection_pages p
            JOIN wiki_projection_versions v
              ON v.topic_id = p.topic_id AND v.projection_version = p.projection_version
            JOIN (
                SELECT topic_id, MAX(generated_at) AS latest_generated_at
                FROM wiki_projection_pages GROUP BY topic_id
            ) latest
              ON latest.topic_id = p.topic_id
             AND p.generated_at = latest.latest_generated_at
            WHERE {like_sql}
            ORDER BY p.topic_id
            LIMIT ?
            """,
            (*like_params, fetch),
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()

    scored: list[dict[str, Any]] = []
    for row in rows:
        # Current-only retrieval accepts only a version explicitly materialized
        # as fresh.  A stale, partial, unavailable, or unknown projection must
        # fall through to the authoritative layers.
        if str(row["freshness_status"] or "").lower() != "fresh":
            continue
        try:
            version, dependencies = latest_version(target, str(row["topic_id"]))
            if (
                version is None
                or version.projection_version != str(row["projection_version"])
                or version.freshness_status != "fresh"
                or dependency_manifest_checksum(dependencies) != version.dependency_manifest_checksum
            ):
                continue
        except Exception:
            continue
        body_json = str(row["page_body"] or "")
        if page_checksum(body_json) != str(row["page_checksum"] or ""):
            continue
        try:
            body = json.loads(body_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict) or body.get("schema") != PAGE_BODY_SCHEMA:
            continue
        subject = str(body.get("subject") or "")
        claims = current_claims(body.get("claims") if isinstance(body.get("claims"), list) else [], _C.UNIFIED_DB)
        answers = [str(claim.get("answer") or "") for claim in claims if isinstance(claim, dict)]
        haystack = " ".join([subject, *answers])
        score = 0.0
        lowered = haystack.lower()
        for needle in needles:
            score += lowered.count(needle.lower())
        if score <= 0:
            continue
        # Bind the result to the claim that actually matched.  Historical or
        # unresolved claims must never be presented as a current fact.
        matched_claims = []
        for claim in claims:
            if not isinstance(claim, dict) or str(claim.get("lifecycle") or "").lower() != "current":
                continue
            claim_text = " ".join(str(claim.get(key) or "") for key in ("question", "answer", "subject"))
            claim_score = sum(claim_text.lower().count(needle.lower()) for needle in needles)
            if claim_score > 0:
                matched_claims.append((claim_score, claim))
        if not matched_claims:
            continue
        _, claim = max(matched_claims, key=lambda pair: (pair[0], str(pair[1].get("unit_id") or "")))
        snippet = str(claim.get("answer") or subject)
        evidence_refs = claim_evidence_refs(claim)
        freshness = "fresh"
        lifecycle = "current"
        try:
            confidence = float(claim.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = {"high": 0.9, "medium": 0.7, "low": 0.4}.get(str(claim.get("confidence") or "").lower(), 0)
        scored.append(
            {
                "unit_id": str(row["topic_id"]),
                "subject": subject,
                "answer": snippet[:300],
                "score": round(score, 4),
                "lifecycle": lifecycle,
                "confidence": confidence,
                "source_message_ref": evidence_refs[0] if evidence_refs else "",
                "evidence_refs": evidence_refs,
                "projection_freshness": freshness,
                "projection_reason_codes": json.loads(row["reason_codes_json"] or "[]") if row["reason_codes_json"] else [],
                "collection": "wiki_projection",
                "retrieval_unit": "wiki_page",
                "rank_reason": "wiki topic page match",
            }
        )
    scored.sort(key=lambda item: (-float(item["score"]), str(item["unit_id"])))
    return scored[: max(1, int(limit))]


class WikiPageLayer(RetrieverLayer):
    """Subject wiki pages (deterministic projection, not a fact SSOT)."""

    layer_name = "wiki_page"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        need = min(_C._WIKI_SLOTS, state.remaining())
        if need <= 0:
            return []
        return search_wiki_pages(query, store=_C.WIKI_PROJECTION_DB, limit=need)
