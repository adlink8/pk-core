"""Phase 62: conversation source adapters package.

Seams provided:
  - ``contracts``  — family adapter capability/result contract types
  - ``snapshots``  — allowlisted content-addressed immutable capture

Family parsers and the 17-family registry land in later Phase 62 plans.
"""

from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult,
    CapabilityDescriptor,
    SourceArtifact,
    SourceArtifactSet,
)

__all__ = [
    "AdaptationResult",
    "CapabilityDescriptor",
    "SourceArtifact",
    "SourceArtifactSet",
]
