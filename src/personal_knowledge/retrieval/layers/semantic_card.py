"""Session-card retrieval layer — compressed cards before the empty KU index."""
from __future__ import annotations

import json
from typing import Any

from personal_knowledge.retrieval import _constants as _C
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState
from personal_knowledge.retrieval.semantic_cards import open_cards_db, search_cards


def _matched_evidence_by_session(con, rows: list[dict]) -> dict[str, dict[str, Any]]:
    """Resolve the exact returned assertion against active facts, never a neighbour."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = str(row.get("session_id") or "")
        matches = row.get("matched_facts")
        if not sid or not isinstance(matches, list) or not matches:
            continue
        fact = matches[0]
        if not isinstance(fact, str):
            continue
        records = con.execute(
            "SELECT evidence_refs, confidence FROM ku_facts "
            "WHERE session_id=? AND fact=? AND status='active' ORDER BY fact_key",
            (sid, fact),
        ).fetchall()
        refs: list[str] = []
        confidences: list[float] = []
        for record in records:
            try:
                values = json.loads(record["evidence_refs"] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(values, list):
                continue
            valid_refs = [ref for ref in values if isinstance(ref, str) and ref.strip()]
            if not valid_refs:
                continue
            refs.extend(ref for ref in valid_refs if ref not in refs)
            raw = record["confidence"]
            try:
                confidence = float(raw)
            except (TypeError, ValueError):
                confidence = {"high": 0.9, "medium": 0.7, "low": 0.4}.get(str(raw).lower(), 0)
            confidences.append(confidence if 0 <= confidence <= 1 else 0)
        if refs:
            out[sid] = {"refs": refs, "confidence": min(confidences), "fact": fact}
    return out


class SemanticCardLayer(RetrieverLayer):
    """Active session cards / ku_facts (status=active only). Read-only."""

    layer_name = "semantic_card"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        need = min(_C._CARD_SLOTS, state.remaining())
        if need <= 0:
            return []
        try:
            con = open_cards_db(_C.CARDS_DB)
        except Exception:
            return []
        try:
            rows = search_cards(query, limit=need, con=con)
            evidence_by_sid = _matched_evidence_by_session(con, rows)
        except Exception:
            return []
        finally:
            con.close()

        items: list[dict[str, Any]] = []
        for row in rows:
            sid = str(row.get("session_id") or "")
            if not sid:
                continue
            facts = row.get("matched_facts") if isinstance(row.get("matched_facts"), list) else []
            evidence = evidence_by_sid.get(sid, {})
            snippet = str(facts[0] if facts else row.get("purpose") or sid)
            items.append(
                {
                    "unit_id": sid,
                    "subject": str(row.get("purpose") or sid)[:80],
                    "answer": snippet[:300],
                    "score": float(row.get("score") or 0),
                    "lifecycle": "current",
                    "confidence": evidence.get("confidence", 0),
                    "source_message_ref": evidence["refs"][0],
                    "evidence_refs": evidence["refs"],
                    "collection": "semantic_cards",
                    "retrieval_unit": "semantic_card",
                    "rank_reason": "semantic session card match",
                }
            )
        return items
