"""Non-dialogue raw fallback layer (prefers Google personal_events).

Port of the "Layered Phase 3" block from semantic_search.search_knowledge_units.
"""
from __future__ import annotations

from typing import Any

from personal_knowledge.retrieval import _constants as _C
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState


class NonDialogueRawLayer(RetrieverLayer):
    """Raw personal_events fallback, defaulting to the Google source."""

    layer_name = "non_dialogue_raw"
    role = "google_normalized"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        if not state.vector_available:
            return []
        # Look up through the owning module at call time so tests that patch
        # semantic_search._semantic_search keep working (the helper reads the
        # module-global _semantic_search at call time too).
        from personal_knowledge.retrieval import semantic_search as _ss_module  # noqa: E402

        need = state.remaining()
        raw_source = state.source if state.source else _C._NON_DIALOGUE_PREFERRED_SOURCE
        raw_events = _ss_module._search_personal_events_filtered(
            query, top_k=need + 4, source=raw_source,
        )

        out: list[dict[str, Any]] = []
        for ev in raw_events:
            if not state.source and (ev.get("source") or "") != _C._NON_DIALOGUE_PREFERRED_SOURCE:
                continue
            item = _ss_module._raw_event_item(
                ev,
                retrieval_unit="event",
                collection="personal_events",
                rank_reason="non_dialogue_raw personal_events",
            )
            if str(item.get("source") or "").lower() == "google":
                item["evidence_ref"] = str(item.get("event_id") or "")
            out.append(item)
        return out
