"""Hybrid retrieval fallback policy: layer order, quotas, skip rules.

Split from semantic_search.search_knowledge_units (OC-4). This module owns:
  - fallback policy resolution (arg / env) and the legacy pad gate
  - the layered vs legacy layer chains and their order
  - per-layer quotas (fetch-ahead) and skip conditions
  - the serving-snapshot binding rule and version role map

search_knowledge_units assembles the chain from here and keeps the shared
telemetry / dedup / evidence concerns.
"""
from __future__ import annotations

import os
from typing import Any

from personal_knowledge.retrieval._constants import (
    COMPRESSED_LAYER_NAMES,
    DEFAULT_FALLBACK_POLICY,
    FALLBACK_POLICIES,
    LAYERED_FALLBACK_ORDER,
)
from personal_knowledge.retrieval.layers.base import SearchState

# Serving-snapshot member role bound to each versionable layer (used for the
# telemetry.version field on the response).
LAYER_ROLES: dict[str, str] = {
    "knowledge_unit": "knowledge_retrieval",
    "canonical_messages": "canonical_message",
    "conversation_turns": "turn_retrieval",
    "non_dialogue_raw": "google_normalized",
}

# Post-primary fallback chain. wiki_page / semantic_card / knowledge_unit are
# run by the assembler before this chain.
_PRIMARY_LAYERS = frozenset((*COMPRESSED_LAYER_NAMES, "knowledge_unit"))
LAYERED_CHAIN: tuple[str, ...] = tuple(
    name for name in LAYERED_FALLBACK_ORDER if name not in _PRIMARY_LAYERS
)

# Legacy mode fallback chain: full personal_events raw fallback (old behavior).
LEGACY_CHAIN: tuple[str, ...] = ("legacy_personal_events",)

# Per-layer fetch-ahead: how many extra candidates beyond the remaining slots.
LAYER_FETCH_AHEAD: dict[str, int] = {
    "canonical_messages": 4,
    "conversation_turns": 4,
    "non_dialogue_raw": 4,
    "legacy_pad": 8,
    "legacy_personal_events": 4,
}

# Per-layer gating config:
#   role             serving-snapshot member role (snapshot-enforced gate)
#   unbound_only     layer only runs when NOT bound to a serving snapshot
#   requires_pad_allowed  layer skipped silently when allow_legacy_pad=False
_LAYER_CONFIG: dict[str, dict[str, Any]] = {
    "canonical_messages": {"role": "canonical_message"},
    "conversation_turns": {"role": "turn_retrieval"},
    "non_dialogue_raw": {"role": "google_normalized"},
    "legacy_pad": {"unbound_only": True, "requires_pad_allowed": True},
    "legacy_personal_events": {"unbound_only": True},
}


def resolve_fallback_policy(fallback_policy: str | None = None) -> str:
    """Resolve hybrid fallback policy from arg or env PERSONAL_DATA_FALLBACK_POLICY."""
    if fallback_policy is None:
        raw = os.environ.get("PERSONAL_DATA_FALLBACK_POLICY", DEFAULT_FALLBACK_POLICY)
    else:
        raw = fallback_policy
    policy = (raw or DEFAULT_FALLBACK_POLICY).strip().lower()
    if policy not in FALLBACK_POLICIES:
        return DEFAULT_FALLBACK_POLICY
    return policy


def resolve_allow_legacy_pad(allow_legacy_pad: bool | None = None) -> bool:
    """Whether layered mode may pad with non-Google personal_events when still short.

    Default True (transition safety; see retrieval-ssot.md § legacy_pad rollout).
    Env: PERSONAL_DATA_ALLOW_LEGACY_PAD=0|1|true|false.
    """
    if allow_legacy_pad is not None:
        return bool(allow_legacy_pad)
    env = os.environ.get("PERSONAL_DATA_ALLOW_LEGACY_PAD")
    if env is None or not str(env).strip():
        return True
    return str(env).strip().lower() in ("1", "true", "yes", "on")


def snapshot_binding_enforced(
    *,
    serving: Any,
    collection_override: str | None,
    resolved_active: str,
    snapshot_collection: str,
) -> bool:
    """Whether the active serving snapshot constrains every retrieval role.

    Enforced only when there is a real snapshot, no caller override, and the
    resolved active collection matches the snapshot's knowledge location.
    """
    return bool(
        serving.snapshot_id
        and collection_override is None
        and resolved_active
        and resolved_active == snapshot_collection
    )


def layer_order(policy: str, *, snapshot_enforced: bool) -> tuple[str, ...]:
    """Fallback layer names in execution order for the policy + snapshot binding.

    Legacy policy with an enforced serving snapshot still runs the layered
    chain (the legacy raw layer is skipped and marked on its telemetry) —
    this mirrors the original ``if policy == "legacy" and not snapshot_enforced
    ... else ...`` branch semantics.
    """
    if policy == "legacy" and not snapshot_enforced:
        return LEGACY_CHAIN
    return LAYERED_CHAIN


def layer_fetch_ahead(layer_name: str) -> int:
    """Extra candidates a layer fetches beyond the remaining slots."""
    return LAYER_FETCH_AHEAD.get(layer_name, 0)


def layer_is_gated(layer_name: str, *, pad_allowed: bool) -> bool:
    """True when the layer must be skipped without touching its telemetry.

    Currently only the legacy pad: with allow_legacy_pad=False it is neither
    attempted nor marked with a skip reason.
    """
    cfg = _LAYER_CONFIG.get(layer_name, {})
    return bool(cfg.get("requires_pad_allowed") and not pad_allowed)


def layer_skip_reason(layer_name: str, state: SearchState) -> str | None:
    """Return the telemetry skipped_reason when the layer must be gated, else None.

    - unbound-only layers (legacy raw / pad) are skipped when a serving
      snapshot is enforced ("not_bound_to_serving_snapshot").
    - snapshot-role-gated layers are skipped when the snapshot lacks the role
      ("snapshot_member_missing:<role>").
    """
    cfg = _LAYER_CONFIG.get(layer_name, {})
    if cfg.get("unbound_only") and state.snapshot_enforced:
        return "not_bound_to_serving_snapshot"
    role = cfg.get("role")
    if role is not None and state.snapshot_enforced and not state.role_allowed(role):
        return f"snapshot_member_missing:{role}"
    return None


def mark_snapshot_skipped_layers(
    policy: str,
    layers: dict[str, dict[str, Any]],
    *,
    snapshot_enforced: bool,
) -> None:
    """Mark telemetry for layers excluded by the snapshot binding rule.

    Mirrors the original god-function branch: under the legacy policy with an
    enforced serving snapshot, the legacy raw layer is not part of the running
    chain (the layered chain runs instead) and its telemetry carries
    "not_bound_to_serving_snapshot".
    """
    if policy == "legacy" and snapshot_enforced:
        entry = layers.get("legacy_personal_events")
        if entry is not None:
            entry["skipped_reason"] = "not_bound_to_serving_snapshot"


def build_fallback_chain(policy: str, telemetry: dict[str, dict[str, Any]], *, snapshot_enforced: bool):
    """Instantiate the fallback layer chain for the resolved policy.

    Each layer receives its response-telemetry dict for any layer-local
    signals. The primary knowledge_unit layer is NOT part of the chain.
    """
    from personal_knowledge.retrieval.layers import (  # noqa: E402
        CanonicalMessagesLayer,
        ConversationTurnsLayer,
        LegacyPadLayer,
        LegacyPersonalEventsLayer,
        NonDialogueRawLayer,
    )

    _FACTORY = {
        "canonical_messages": CanonicalMessagesLayer,
        "conversation_turns": ConversationTurnsLayer,
        "non_dialogue_raw": NonDialogueRawLayer,
        "legacy_pad": LegacyPadLayer,
        "legacy_personal_events": LegacyPersonalEventsLayer,
    }
    chain = []
    for name in layer_order(policy, snapshot_enforced=snapshot_enforced):
        cls = _FACTORY[name]
        chain.append(cls(telemetry[name]))
    return chain
