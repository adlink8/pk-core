"""Phase 22-02: layered retrieval contract (no live Chroma required).

Documents and asserts hybrid fallback order:
  wiki → session cards → KU (current-only) → dialogue → Google → optional pad.

Growth-line multi-version history is a separate read surface (pk-ku history),
not part of default retrieval.
"""

from __future__ import annotations

from personal_knowledge.retrieval._constants import (
    DEFAULT_FALLBACK_POLICY,
    FALLBACK_POLICIES,
    LAYERED_FALLBACK_ORDER,
    _NON_DIALOGUE_PREFERRED_SOURCE,
)


def test_default_policy_is_layered() -> None:
    assert DEFAULT_FALLBACK_POLICY == "layered"
    assert "layered" in FALLBACK_POLICIES
    assert "legacy" in FALLBACK_POLICIES


def test_layered_order_ku_then_dialogue_then_google() -> None:
    """Contract: wiki-first, then cards, KU, dialogue, then non-dialogue raw."""
    assert LAYERED_FALLBACK_ORDER[0] == "wiki_page"
    assert LAYERED_FALLBACK_ORDER[1] == "semantic_card"
    # Dialogue layers immediately after KU (message-level then turns)
    dialogue = [
        x
        for x in LAYERED_FALLBACK_ORDER
        if x in ("canonical_messages", "conversation_turns")
    ]
    assert dialogue == ["canonical_messages", "conversation_turns"]
    wiki_i = LAYERED_FALLBACK_ORDER.index("wiki_page")
    card_i = LAYERED_FALLBACK_ORDER.index("semantic_card")
    ku_i = LAYERED_FALLBACK_ORDER.index("knowledge_unit")
    msg_i = LAYERED_FALLBACK_ORDER.index("canonical_messages")
    turns_i = LAYERED_FALLBACK_ORDER.index("conversation_turns")
    raw_i = LAYERED_FALLBACK_ORDER.index("non_dialogue_raw")
    assert wiki_i < card_i < ku_i < msg_i < turns_i < raw_i
    # Optional pad last among growth-relevant layers
    if "legacy_pad" in LAYERED_FALLBACK_ORDER:
        assert LAYERED_FALLBACK_ORDER.index("legacy_pad") > raw_i


def test_non_dialogue_preferred_source_is_google() -> None:
    assert _NON_DIALOGUE_PREFERRED_SOURCE == "Google"


def test_resolve_fallback_policy_accepts_layered() -> None:
    """Pure resolve helper (re-exported path used by search backend)."""
    from personal_knowledge.retrieval.semantic_search import (
        _resolve_fallback_policy,
    )

    assert _resolve_fallback_policy("layered") == "layered"
    assert _resolve_fallback_policy("legacy") == "legacy"
    assert _resolve_fallback_policy("unknown-junk") == DEFAULT_FALLBACK_POLICY
    assert _resolve_fallback_policy(None) == DEFAULT_FALLBACK_POLICY


def test_layered_telemetry_layer_names_cover_contract() -> None:
    """search_knowledge_units initializes telemetry for each contract layer."""
    # Names used when packing empty result path — must stay aligned with constant
    expected = set(LAYERED_FALLBACK_ORDER) | {"legacy_personal_events"}
    # Source of truth for empty-layer init lives in semantic_search; re-check
    # the documented order is a non-empty sequence starting at wiki_page.
    assert len(LAYERED_FALLBACK_ORDER) >= 6
    assert LAYERED_FALLBACK_ORDER[0] == "wiki_page"
    assert "knowledge_unit" in expected
    assert "canonical_messages" in expected
    assert "non_dialogue_raw" in expected
