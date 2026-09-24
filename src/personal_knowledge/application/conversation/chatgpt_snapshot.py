"""Backward-compatible private ChatGPT snapshot wrapper."""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.adapters.conversation_sources import chatgpt
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.application.conversation.agentsview_unavailable_snapshot import (
    capture_pathless_agent_snapshot,
)


def capture_chatgpt_snapshot(
    source: Path,
    dest: Path,
    *,
    byte_limit: int = 2_000_000_000,
) -> tuple[SourceArtifact, Path]:
    """Capture only active ChatGPT rows with a NULL/blank native locator."""

    return capture_pathless_agent_snapshot(
        source, dest, family=chatgpt.FAMILY, byte_limit=byte_limit
    )


__all__ = ["capture_chatgpt_snapshot"]
