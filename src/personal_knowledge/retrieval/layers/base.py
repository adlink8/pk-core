"""Retriever layer interface + shared per-request search state.

Split from semantic_search.search_knowledge_units (OC-4): each hybrid
retrieval surface becomes a RetrieverLayer producing normalized candidate
items; the assembler in semantic_search threads SearchState through the
chain and owns telemetry / dedup / evidence resolution.
"""
from __future__ import annotations

from typing import Any, Callable


class SearchState:
    """Per-request state threaded through the fallback layer chain.

    Carries the resolved policy/snapshot context plus the shared result
    collections and hooks. Layer classes read hooks through the owning
    module attributes at call time so test monkeypatches keep working.
    """

    def __init__(
        self,
        *,
        top_k: int,
        source: str | None,
        include_evidence: bool,
        policy: str,
        pad_allowed: bool,
        snapshot_enforced: bool,
        serving: Any,
    ) -> None:
        self.top_k = top_k
        self.source = source
        self.include_evidence = include_evidence
        self.policy = policy
        self.pad_allowed = pad_allowed
        self.snapshot_enforced = snapshot_enforced
        self.serving = serving
        # Filled in by the assembler before the chain runs.
        self.ku_collection: str = ""
        self.embedding: list[float] | None = None
        self.client: Any = None
        # False when the optional Chroma/embedding path is unavailable. The
        # assembler still runs SQLite-backed canonical fallback layers, while
        # vector-only layers must not invoke the optional model stack again.
        self.vector_available: bool = True
        self.resolve_support_ref: Callable[..., Any] | None = None
        # Shared mutable results / signals.
        self.route: str = "knowledge"
        self.versions: dict[str, Any] = {}
        self.compressed_results: list[dict[str, Any]] = []
        self.ku_results: list[dict[str, Any]] = []
        self.fallback_results: list[dict[str, Any]] = []
        self.seen_ids: set[str] = set()
        self.ku_abstained: int = 0

    def remaining(self) -> int:
        """Slots still to fill across compressed + knowledge + fallback results."""
        return (
            self.top_k
            - len(self.compressed_results)
            - len(self.ku_results)
            - len(self.fallback_results)
        )

    def role_allowed(self, role: str) -> bool:
        """Whether a serving-snapshot member role may be queried."""
        return not self.snapshot_enforced or self.serving.member(role) is not None


class RetrieverLayer:
    """Base class for a hybrid retrieval layer.

    ``layer_name`` matches the response-telemetry layer key. ``retrieve``
    returns normalized hybrid-schema candidate items; the assembler applies
    shared dedup / evidence gating / quota bookkeeping on top.
    """

    layer_name: str = ""
    # Optional serving-snapshot member role (snapshot-enforced gating).
    role: str | None = None

    def __init__(self, telemetry: dict[str, Any] | None = None) -> None:
        self.telemetry = telemetry

    def retrieve(self, query: str, state: SearchState) -> list[dict[str, Any]]:
        raise NotImplementedError
