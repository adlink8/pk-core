"""Phase 62: family adapter capability / probe / result public seam.

One explicit contract per agent family (D-02). A family adapter consumes an
immutable :class:`SourceArtifactSet` plus a versioned :class:`CapabilityDescriptor`
and produces an :class:`AdaptationResult` containing sessions, typed events,
first-class relations, fidelity, field dispositions, warnings and a
deterministic dataset digest.

The capture seam (:mod:`.snapshots`) produces :class:`SourceArtifact` objects;
family parsers (later plans) are the only producers of :class:`AdaptationResult`.
This module never parses native formats and never publishes data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    dataset_digest,
    EventContractError,
    EventKind,
    EventRelation,
    FieldDispositionRecord,
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    Provenance,
    RelationKind,
    TypedEvent,
)


@dataclass(frozen=True)
class SourceArtifact:
    """An immutable source artifact captured from one stable source slot.

    ``artifact_id`` is the *slot* identity — ``sha256("art|<family>|<mirror
    path>")`` (see ``snapshots.make_slot_artifact_id``), constant across content
    edits so event/session ids do not rotate when a file is edited.
    ``content_hash`` is the byte hash (``sha256(bytes)``) and is what addresses
    the on-disk blob store. Paths are therefore always resolved through
    ``content_hash``, never through ``artifact_id``.

    ``schema_digest``/``privacy_dispositions`` are metadata-only — never bodies
    or credentials.
    """

    artifact_id: str
    family: str
    source_kind: str  # 'file' | 'directory' | 'sqlite'
    content_hash: str
    capture_method: str
    relative_path: str
    byte_size: int
    schema_digest: str | None = None
    privacy_dispositions: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceArtifactSet:
    """Immutable set of source artifacts handed to a family adapter."""

    artifacts: tuple[SourceArtifact, ...] = ()

    def digest(self) -> str:
        """Deterministic digest over the artifact set (stable under ordering)."""
        payload = "|".join(
            a.artifact_id for a in sorted(self.artifacts, key=lambda a: a.artifact_id)
        )
        return hashlib.sha256(f"artifacts|{payload}".encode("utf-8")).hexdigest()

    def by_id(self) -> dict[str, SourceArtifact]:
        return {a.artifact_id: a for a in self.artifacts}


# ``content_hash`` sentinel carried by *probe* artifacts. A probe is built from a
# file head before the file is captured (``v2_sync._probe_artifact``,
# ``discovery._artifact_for``) and is rooted at the source directory, so it has no
# byte hash and no blob yet.
PROBE_CONTENT_HASH = "probe"


def artifact_bytes_path(artifact_root: Path, artifact: SourceArtifact) -> Path:
    """Resolve the file that holds ``artifact``'s bytes under ``artifact_root``.

    Two different rooting conventions reach the same call site, and only one of
    the two identity fields is meaningful for each:

    * a **captured** artifact lives in the content-addressed blob store, keyed by
      ``content_hash[:32]`` (never by the slot ``artifact_id``);
    * a **probe** artifact (``content_hash == PROBE_CONTENT_HASH``) is rooted at
      the source directory it was probed from, and resolves through
      ``relative_path``.

    Deriving the path from ``content_hash`` alone silently broke every probe: it
    looked for a file literally named ``probe``, so ``detect()`` returned False
    for every source and ``pk-sync --v2-native`` reported ``no_source`` for all
    families. Deriving it from ``artifact_id`` is the mirror-image bug (see the
    module docstring of :mod:`.snapshots`). Adapters must call this helper
    instead of joining either field themselves.
    """
    if artifact.content_hash == PROBE_CONTENT_HASH:
        return Path(artifact_root) / artifact.relative_path
    return Path(artifact_root) / artifact.content_hash[:32]


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Versioned capability contract of one family adapter (D-02).

    ``digest()`` is stable for identical capabilities and changes when the
    adapter or contract version changes, enabling schema/version gates.
    """

    family: str
    adapter_version: str
    contract_version: str
    supported_event_kinds: tuple[EventKind, ...] = ()
    supported_relation_kinds: tuple[RelationKind, ...] = ()
    fidelity_dimensions: tuple[FidelityDimension, ...] = ()
    capabilities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.family:
            raise EventContractError("capability requires a family")
        if not self.adapter_version or not self.contract_version:
            raise EventContractError(
                "capability requires adapter and contract versions"
            )

    def digest(self) -> str:
        payload = {
            "family": self.family,
            "adapter_version": self.adapter_version,
            "contract_version": self.contract_version,
            "event_kinds": sorted(k.value for k in self.supported_event_kinds),
            "relation_kinds": sorted(k.value for k in self.supported_relation_kinds),
            "fidelity_dimensions": sorted(
                d.value for d in self.fidelity_dimensions
            ),
            "capabilities": dict(sorted(self.capabilities.items())),
        }
        return hashlib.sha256(
            f"cap|{payload}".encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class AdaptationResult:
    """The full output of one family adaptation run.

    Validation (constructor): every event must carry resolvable provenance and
    every relation endpoint must reference a known event in the same result —
    a complete-looking but unprovenanced/lossy record cannot be emitted (D-04).

    ``dataset_digest`` is a deterministic property recomputed from the contents,
    so replay of identical input always yields the same digest.
    """

    family: str
    adapter_version: str
    contract_version: str
    artifacts: tuple[SourceArtifact, ...]
    events: tuple[TypedEvent, ...]
    fidelity: FidelityProfile
    sessions: tuple[AdaptedSession, ...] = ()
    relations: tuple[EventRelation, ...] = ()
    field_dispositions: tuple[FieldDispositionRecord, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.family or not self.adapter_version or not self.contract_version:
            raise EventContractError(
                "adaptation result requires family, adapter and contract versions"
            )
        event_ids = [event.event_id for event in self.events]
        duplicate_events = len(event_ids) - len(set(event_ids))
        if duplicate_events:
            raise EventContractError(
                f"adaptation result contains {duplicate_events} duplicate event id(s)"
            )
        session_ids = [session.session_id for session in self.sessions]
        duplicate_sessions = len(session_ids) - len(set(session_ids))
        if duplicate_sessions:
            raise EventContractError(
                f"adaptation result contains {duplicate_sessions} duplicate session id(s)"
            )
        relation_ids = [relation.relation_id for relation in self.relations]
        duplicate_relations = len(relation_ids) - len(set(relation_ids))
        if duplicate_relations:
            raise EventContractError(
                f"adaptation result contains {duplicate_relations} duplicate relation id(s)"
            )
        for event in self.events:
            if not event.provenance.resolvable():
                raise EventContractError(
                    f"event {event.event_id} is unprovenanced (no artifact/native locator)"
                )
        known = {e.event_id for e in self.events}
        for relation in self.relations:
            if (
                relation.source_event_id not in known
                or relation.target_event_id not in known
            ):
                raise EventContractError(
                    f"relation {relation.relation_id} references an event "
                    "outside this adaptation result"
                )
        if not isinstance(self.fidelity, FidelityProfile):
            raise EventContractError("adaptation result requires a fidelity profile")
        children = tuple(e.fidelity for e in self.events) + tuple(
            s.fidelity for s in self.sessions
        )
        rolled_up = FidelityProfile.worst(self.fidelity, *children)
        if self.warnings:
            rolled_up = rolled_up.with_at_least(
                FidelityDimension.STRUCTURE_COMPLETENESS,
                FidelityLevel.PARTIAL,
            )
        object.__setattr__(self, "fidelity", rolled_up)

    @property
    def dataset_digest(self) -> str:
        return dataset_digest(
            family=self.family,
            adapter_version=self.adapter_version,
            contract_version=self.contract_version,
            artifacts=self.artifacts,
            sessions=self.sessions,
            events=self.events,
            relations=self.relations,
        )


__all__ = [
    "AdaptationResult",
    "CapabilityDescriptor",
    "SourceArtifact",
    "SourceArtifactSet",
]
