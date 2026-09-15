"""Hybrid retrieval fallback layers.

Split from semantic_search.search_knowledge_units (OC-4). Each layer is a
retrieval strategy producing normalized candidate items; the assembler in
semantic_search owns the shared telemetry / dedup / evidence concerns.
"""
from personal_knowledge.retrieval.layers.base import RetrieverLayer, SearchState  # noqa: F401
from personal_knowledge.retrieval.layers.canonical_messages import CanonicalMessagesLayer  # noqa: F401
from personal_knowledge.retrieval.layers.conversation_turns import ConversationTurnsLayer  # noqa: F401
from personal_knowledge.retrieval.layers.knowledge_unit import KnowledgeUnitLayer  # noqa: F401
from personal_knowledge.retrieval.layers.legacy_pad import LegacyPadLayer, LegacyPersonalEventsLayer  # noqa: F401
from personal_knowledge.retrieval.layers.non_dialogue_raw import NonDialogueRawLayer  # noqa: F401
from personal_knowledge.retrieval.layers.semantic_card import SemanticCardLayer  # noqa: F401
from personal_knowledge.retrieval.layers.wiki_page import WikiPageLayer  # noqa: F401

__all__ = [
    "RetrieverLayer",
    "SearchState",
    "WikiPageLayer",
    "SemanticCardLayer",
    "KnowledgeUnitLayer",
    "CanonicalMessagesLayer",
    "ConversationTurnsLayer",
    "NonDialogueRawLayer",
    "LegacyPadLayer",
    "LegacyPersonalEventsLayer",
]
