"""Message-level dialogue fallback layer (canonical_messages).

Port of the "Layered Phase 2a" block from semantic_search.search_knowledge_units.
Delegates to semantic_search._search_dialogue_canonical_messages (kept in the
original module so its public test surface is unchanged).
"""
from __future__ import annotations

from typing import Any

from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState


class CanonicalMessagesLayer(RetrieverLayer):
    """Dialogue snippet/token search on canonical_messages (read-only)."""

    layer_name = "canonical_messages"
    role = "canonical_message"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        # Look up through the owning module at call time so tests that patch
        # semantic_search._search_dialogue_canonical_messages keep working.
        from personal_knowledge.retrieval import semantic_search as _ss_module  # noqa: E402

        need = state.remaining()
        return _ss_module._search_dialogue_canonical_messages(query, top_k=need + 4)
