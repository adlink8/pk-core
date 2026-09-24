"""Legacy raw-event layers.

- LegacyPersonalEventsLayer: old behavior — full personal_events semantic
  fallback after KU (legacy policy).
- LegacyPadLayer: optional layered-mode pad with non-Google personal_events
  when the layered chain is still short.

Ports of the "Legacy Phase 2" and "Layered Phase 4" blocks from
semantic_search.search_knowledge_units.
"""
from __future__ import annotations

from typing import Any

from personal_knowledge.retrieval import _constants as _C
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState


class LegacyPersonalEventsLayer(RetrieverLayer):
    """Legacy policy: raw personal_events fill for every remaining slot."""

    layer_name = "legacy_personal_events"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        if not state.vector_available:
            return []
        # Look up through the owning module at call time so tests that patch
        # semantic_search._semantic_search keep working.
        from personal_knowledge.retrieval import semantic_search as _ss_module  # noqa: E402

        raw_target = state.remaining()
        raw_events = _ss_module._semantic_search(
            query, top_k=raw_target + 4, source=state.source,
        )
        return [
            _ss_module._raw_event_item(
                ev,
                retrieval_unit="event",
                collection="personal_events",
                rank_reason="raw event semantic match",
            )
            for ev in raw_events
        ]


class LegacyPadLayer(RetrieverLayer):
    """Layered-mode optional pad with non-Google personal_events."""

    layer_name = "legacy_pad"

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        if not state.vector_available:
            return []
        from personal_knowledge.retrieval import semantic_search as _ss_module  # noqa: E402

        need = state.remaining()
        pad_events = _ss_module._search_personal_events_filtered(
            query, top_k=need + 8, source=state.source,
        )

        out: list[dict[str, Any]] = []
        for ev in pad_events:
            src = ev.get("source") or ""
            if not state.source and src == _C._NON_DIALOGUE_PREFERRED_SOURCE:
                continue
            item = _ss_module._raw_event_item(
                ev,
                retrieval_unit="event",
                collection="personal_events",
                rank_reason="legacy_pad",
            )
            out.append(item)
        return out
