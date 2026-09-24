"""Phase 62-02: versioned family detection and adapter selection seam.

One explicit capability contract per family (D-02). The registry routes a
captured artifact to the family adapter that detects it, and never falls
back to a generic message parser. Aliases (e.g. ``vscode-copilot`` for the
Copilot adapter) resolve to their owning family.
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.adapters.conversation_sources import (
    antigravity,
    chatgpt,
    claude_qoder,
    codex,
    copilot,
    cursor,
    gemini,
    grok,
    mimo_opencode,
    pi,
    workbuddy_kimi,
    zcode,
)
from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
    CapabilityDescriptor,
    SourceArtifact,
    SourceArtifactSet,
)

# family -> (capability(), detect(artifact, artifact_root=...), adapt(set, artifact_root=...))
_ADAPTERS: dict[str, tuple] = {
    "codex": (codex.capability, codex.detect, codex.adapt),
    "claude": (lambda: claude_qoder.capability("claude"), lambda a, **k: claude_qoder.detect("claude", a, **k), lambda s, **k: claude_qoder.adapt("claude", s, **k)),
    "qoder": (lambda: claude_qoder.capability("qoder"), lambda a, **k: claude_qoder.detect("qoder", a, **k), lambda s, **k: claude_qoder.adapt("qoder", s, **k)),
    "pi": (pi.capability, pi.detect, pi.adapt),
    "workbuddy": (lambda: workbuddy_kimi.capability("workbuddy"), lambda a, **k: workbuddy_kimi.detect("workbuddy", a, **k), lambda s, **k: workbuddy_kimi.adapt("workbuddy", s, **k)),
    "kimi": (lambda: workbuddy_kimi.capability("kimi"), lambda a, **k: workbuddy_kimi.detect("kimi", a, **k), lambda s, **k: workbuddy_kimi.adapt("kimi", s, **k)),
    "kimi-work": (lambda: workbuddy_kimi.capability("kimi-work"), lambda a, **k: workbuddy_kimi.detect("kimi-work", a, **k), lambda s, **k: workbuddy_kimi.adapt("kimi-work", s, **k)),
    "copilot": (copilot.capability, copilot.detect, copilot.adapt),
    "gemini": (gemini.capability, gemini.detect, gemini.adapt),
    # Phase 62-03 families registered here (handoff documented in 62-03 SUMMARY).
    "zcode": (zcode.capability, zcode.detect, zcode.adapt),
    "mimo": (lambda: mimo_opencode.capability("mimo"), lambda a, **k: mimo_opencode.detect(a, **k), lambda s, **k: mimo_opencode.adapt("mimo", s, **k)),
    "opencode": (lambda: mimo_opencode.capability("opencode"), lambda a, **k: mimo_opencode.detect(a, **k), lambda s, **k: mimo_opencode.adapt("opencode", s, **k)),
    "antigravity": (antigravity.capability, antigravity.detect, antigravity.adapt),
    "grok": (grok.capability, grok.detect, grok.adapt),
    "chatgpt": (chatgpt.capability, chatgpt.detect, chatgpt.adapt),
    "cursor": (cursor.capability, cursor.detect, cursor.adapt),
}

# Aliases resolve to the owning family (D-02: family retains its own contract).
ALIASES: dict[str, str] = {
    "vscode-copilot": "copilot",
}

_KNOWN_FAMILIES = frozenset(_ADAPTERS) | frozenset(ALIASES)


def known_families() -> tuple[str, ...]:
    """All registered family names (including aliases)."""
    return tuple(sorted(_KNOWN_FAMILIES))


def resolve_family(name: str) -> str:
    """Resolve an alias to its owning family; unknown names fail closed."""
    if name in _ADAPTERS:
        return name
    owner = ALIASES.get(name)
    if owner is not None:
        return owner
    raise KeyError(f"no conversation adapter registered for family {name!r}")


def capability_for(name: str) -> CapabilityDescriptor:
    """Versioned capability contract for a family (or its alias)."""
    family = resolve_family(name)
    return _ADAPTERS[family][0]()


def detect_family(name: str, artifact: SourceArtifact, *, artifact_root: Path) -> bool:
    """Run a family's detector against an artifact."""
    family = resolve_family(name)
    return _ADAPTERS[family][1](artifact, artifact_root=artifact_root)


def adapt_for(name: str, artifact_set: SourceArtifactSet, *, artifact_root: Path) -> AdaptationResult:
    """Adapt an artifact set with the owning family adapter."""
    family = resolve_family(name)
    return _ADAPTERS[family][2](artifact_set, artifact_root=artifact_root)


def select_adapter(artifact: SourceArtifact, *, artifact_root: Path) -> str | None:
    """Return the first family whose detector matches, or None (no generic fallback)."""
    for family in _ADAPTERS:
        if _ADAPTERS[family][1](artifact, artifact_root=artifact_root):
            return family
    return None


__all__ = [
    "ALIASES",
    "adapt_for",
    "capability_for",
    "detect_family",
    "known_families",
    "resolve_family",
    "select_adapter",
]
