"""Read-only resolution of the single composite serving authority."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_knowledge.application.serving.snapshots import get_active_snapshot
from personal_knowledge.core.project_paths import KNOWLEDGE_ACTIVE_POINTER, UNIFIED_DB


@dataclass(frozen=True)
class ServingState:
    snapshot_id: str | None
    manifest_hash: str | None
    members: dict[str, dict[str, Any]]
    drift: list[str]
    legacy: bool = False

    def member(self, role: str) -> dict[str, Any] | None:
        return self.members.get(role)


class ServingSnapshotResolver:
    def __init__(self, db_path: Path = UNIFIED_DB, pointer_path: Path = KNOWLEDGE_ACTIVE_POINTER):
        self.db_path = db_path
        self.pointer_path = pointer_path

    def resolve(self) -> ServingState:
        try:
            active = get_active_snapshot(self.db_path)
        except Exception:
            active = None
        if not active:
            pointer = self.pointer_path.read_text(encoding="utf-8").strip() if self.pointer_path.exists() else ""
            members = {"knowledge_retrieval": {"location_ref": pointer, "version": pointer}} if pointer else {}
            return ServingState(None, None, members, [], legacy=True)
        members = active.get("members") or {}
        drift: list[str] = []
        projected = self.pointer_path.read_text(encoding="utf-8").strip() if self.pointer_path.exists() else ""
        expected = str((members.get("knowledge_retrieval") or {}).get("location_ref") or "")
        if projected != expected:
            drift.append("knowledge_active_pointer")
        return ServingState(
            str(active["snapshot_id"]),
            str(active["manifest_hash"]),
            members,
            drift,
            legacy=False,
        )


def member_version(member: dict[str, Any] | None) -> str | None:
    if not member:
        return None
    return str(member.get("version") or member.get("artifact_version_id") or "") or None
