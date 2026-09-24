"""Scoped Wiki-first read router with explicit source provenance.

The router accepts only a canonical P0 TopicKey for the Wiki branch.  Fallback
adapters are injected read adapters; this module deliberately does not import
KU writers, Chroma clients, providers, evidence writers, or mutation services.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from personal_knowledge.wiki.topic_key import (
    TopicProjectionError,
    WIKI_REASON_CODES,
    make_wiki_envelope,
    parse_topic_key,
)


SOURCE_ORDER = ("structured_authority", "active_ku_search", "raw_evidence")


def _call(reader: Any, *, topic_key: str | None, query: str | None) -> Any:
    if reader is None:
        return None
    if callable(reader):
        return reader(topic_key=topic_key, query=query)
    return None


class WikiReadRouter:
    """Select fresh Wiki or a bounded, provenance-preserving fallback."""

    def __init__(
        self,
        *,
        topic_service: Any,
        structured_reader: Callable[..., Any] | None = None,
        ku_reader: Callable[..., Any] | None = None,
        evidence_reader: Callable[..., Any] | None = None,
    ) -> None:
        self.topic_service = topic_service
        self.structured_reader = structured_reader
        self.ku_reader = ku_reader
        self.evidence_reader = evidence_reader

    @staticmethod
    def _fallback_reason(status: str | None, error: str | None) -> str:
        if error in WIKI_REASON_CODES:
            return error
        return {
            "stale": "snapshot_mismatch",
            "partial": "projection_partial",
            "missing": "projection_record_missing",
            "unavailable": "authority_unavailable",
        }.get(status or "", "topic_not_found")

    @staticmethod
    def _usable(result: Any) -> bool:
        if result is None:
            return False
        if isinstance(result, Mapping):
            return result.get("ok", True) is True and result.get("data", result) is not None
        return True

    @staticmethod
    def _source_result(source: str, result: Any) -> dict[str, Any]:
        if isinstance(result, Mapping):
            return {
                "source": source,
                "data": result.get("data", result),
                "snapshot_bindings": dict(result.get("snapshot_bindings") or {}),
                "evidence_refs": list(result.get("evidence_refs") or ())[:16],
                "epistemic_label": result.get("epistemic_label") or source,
                "limitations": list(result.get("limitations") or ())[:8],
            }
        return {"source": source, "data": result, "snapshot_bindings": {}, "evidence_refs": [], "epistemic_label": source, "limitations": []}

    def resolve(self, *, topic_key: str | None = None, query: str | None = None) -> dict[str, Any]:
        key = None
        fallback_reason = "long_tail_bypass"
        attempted: list[str] = []
        if topic_key:
            try:
                key = parse_topic_key(topic_key)
            except TopicProjectionError as exc:
                fallback_reason = exc.reason_code
        if key is not None:
            attempted.append("wiki")
            wiki = self.topic_service.invoke("topic.get", topic_key=key.canonical)
            if wiki.get("ok") is True and wiki.get("status") == "fresh" and wiki.get("data") is not None:
                data = {
                    "selected_source": "fresh_wiki",
                    "attempted_sources": attempted,
                    "fallback_reason": None,
                    "source": self._source_result("fresh_wiki", wiki),
                    "topic": wiki.get("data", {}).get("topic"),
                }
                return make_wiki_envelope(
                    "topic.resolve", ok=True, data=data,
                    generated_at=wiki.get("generated_at"), snapshot_bindings=wiki.get("snapshot_bindings"),
                    freshness={**dict(wiki.get("freshness") or {}), "status": "fresh"},
                    authorities=wiki.get("authorities"), limitations=wiki.get("limitations"),
                    projection_checksum=wiki.get("projection_checksum"), status="fresh",
                )
            fallback_reason = self._fallback_reason(wiki.get("status"), wiki.get("error"))
        for source, reader in zip(SOURCE_ORDER, (self.structured_reader, self.ku_reader, self.evidence_reader)):
            attempted.append(source)
            try:
                result = _call(reader, topic_key=key.canonical if key else None, query=query)
            except Exception:  # noqa: BLE001 — adapter details never cross the boundary
                result = None
            if self._usable(result):
                selected = self._source_result(source, result)
                data = {
                    "selected_source": source,
                    "attempted_sources": attempted,
                    "fallback_reason": fallback_reason,
                    "source": selected,
                    "topic": {"canonical_key": key.canonical} if key else None,
                }
                return make_wiki_envelope(
                    "topic.resolve", ok=True, data=data,
                    snapshot_bindings=selected["snapshot_bindings"],
                    freshness={"state": "fallback", "status": "fallback"},
                    authorities={source: "ok"}, limitations=selected["limitations"] + ["当前结果不是 fresh Wiki 投影。"],
                    projection_checksum=None, status="partial",
                )
        return make_wiki_envelope(
            "topic.resolve", ok=False, data=None, error="authority_unavailable",
            generated_at=None, snapshot_bindings={}, freshness={"state": "unavailable", "status": "unavailable"},
            authorities={source: "unavailable" for source in attempted}, partial=True,
            limitations=["Wiki 与所有受限回退 authority 均不可用。"], status="unavailable",
        )


__all__ = ["SOURCE_ORDER", "WikiReadRouter"]
