"""Conversation-turns dialogue fallback layer.

Port of the "Layered Phase 2b" block from semantic_search.search_knowledge_units.
"""
from __future__ import annotations

from typing import Any

from personal_knowledge.retrieval import _constants as _C
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState


class ConversationTurnsLayer(RetrieverLayer):
    """Turn-narrative dialogue retrieval on conversation_turns."""

    layer_name = "conversation_turns"
    role = "turn_retrieval"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        if not state.vector_available:
            return []
        # Look up through the owning module at call time so tests that patch
        # search_vectors.search_conversation_turns keep working.
        import personal_knowledge.retrieval.search_vectors as _sv  # noqa: E402
        from personal_knowledge.retrieval import semantic_search as _ss_module  # noqa: E402

        need = state.remaining()
        turns = _sv.search_conversation_turns(query, top_k=need + 4, source=state.source)

        out: list[dict[str, Any]] = []
        for ev in turns:
            item = _ss_module._raw_event_item(
                ev,
                retrieval_unit="dialogue",
                collection=_C.CONVERSATION_TURNS_COLLECTION,
                rank_reason="dialogue_fallback conversation_turns",
            )
            item["collection"] = _C.CONVERSATION_TURNS_COLLECTION
            item["retrieval_unit"] = "dialogue"
            item["evidence_ref"] = str(item.get("event_id") or "")
            out.append(item)
        return out
