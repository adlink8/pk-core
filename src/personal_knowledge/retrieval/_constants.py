"""Shared constants for retrieval submodules (split from unified_search.py).
Path constants (UNIFIED_DB etc.) live here as the single source of truth —
tests monkeypatch THIS module to redirect the DB."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_THIS_DIR = _SCRIPTS_DIR

from personal_knowledge.core.project_paths import (  # noqa: E402
    ROOT, UNIFIED_DB, GOOGLE_DB, AGENT_CONVERSATIONS_DB, DB_DIR, VAR_DB,
    WIKI_PROJECTION_DB,
)

DEFAULT_MEMORY_GRAPH_LIMIT = 100
MAX_MEMORY_GRAPH_LIMIT = 200
DEFAULT_RELATION_REVIEW_LIMIT = 50
MAX_RELATION_REVIEW_LIMIT = 200
RELATION_REVIEW_STATUSES = {"review", "accepted", "rejected"}
DEFAULT_DATA_LIMIT = 100
MAX_DATA_LIMIT = 500
MAX_EXPORT_LIMIT = 5000

DEFAULT_EVENT_FIELDS = [
    "event_id", "source", "event_time", "title", "service", "category_v2",
]
EVENT_FIELD_SQL = {
    "event_id": "ue.event_id", "source": "ue.source",
    "source_table": "ue.source_table", "source_id": "ue.source_id",
    "event_type": "ue.event_type", "service": "ue.service",
    "event_time": "ue.event_time", "month": "ue.month", "title": "ue.title",
    "category": "ue.category", "category_v2": "c.category_v2",
    "url": "ue.url", "domain": "ue.domain", "file_name": "ue.file_name",
    "session_id": "ue.session_id", "weight": "ue.weight",
    "content": "ue.content", "content_rich": "r.content_rich",
    "has_rich": "(r.content_rich IS NOT NULL)",
}
AGGREGATE_GROUP_SQL = {
    "source": "ue.source", "service": "ue.service",
    "category_v2": "c.category_v2", "category": "c.category_v2",
    "event_type": "ue.event_type",
    "month": "substr(ue.event_time, 1, 7)",
    "day": "substr(ue.event_time, 1, 10)",
    "year": "substr(ue.event_time, 1, 4)",
}
_KU_SLOTS = 3
_WIKI_SLOTS = 2
_CARD_SLOTS = 2
_RAW_SLOTS_DEFAULT = 4
_KU_PORT = 8001
FALLBACK_POLICIES = ("legacy", "layered")
DEFAULT_FALLBACK_POLICY = "layered"
CONVERSATION_TURNS_COLLECTION = "conversation_turns"
CANONICAL_MESSAGES_COLLECTION = "canonical_messages"
_NON_DIALOGUE_PREFERRED_SOURCE = "Google"

# Disposable projection stores (read-only in retrieval; not fact SSOT).
CARDS_DB = VAR_DB / "semantic_mvp_v3.sqlite"

# Layered hybrid order (Phase 15 / 22 contract, wiki-first 2026-09).
# Retrieval surface is lifecycle=current only; growth line is pk-ku history.
# Order: wiki projection → session cards → KU → dialogue → Google → optional pad.
LAYERED_FALLBACK_ORDER = (
    "wiki_page",
    "semantic_card",
    "knowledge_unit",
    "canonical_messages",
    "conversation_turns",
    "non_dialogue_raw",
    "legacy_pad",
)
COMPRESSED_LAYER_NAMES = ("wiki_page", "semantic_card")
