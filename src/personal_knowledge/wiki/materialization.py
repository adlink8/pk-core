"""Deterministic Wiki projection metadata materialization and validation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from personal_knowledge.wiki.topic_key import TopicKey, opaque_topic_id
from personal_knowledge.wiki.derived_store import (
    ProjectionDependency,
    ProjectionVersion,
    SCHEMA_VERSION,
    connect_rw,
    insert_version,
    latest_version,
)


REASON_CODES = frozenset({
    "serving_snapshot_changed", "personal_snapshot_changed", "external_snapshot_changed",
    "decision_sequence_changed", "dependency_missing", "dependency_lifecycle_changed",
    "dependency_checksum_mismatch", "authority_unavailable", "projection_record_missing",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def canonical_dependencies(dependencies: Iterable[ProjectionDependency]) -> tuple[ProjectionDependency, ...]:
    return tuple(sorted(dependencies, key=lambda item: (item.order_key or f"{item.authority}:{item.stable_ref}", item.authority, item.stable_ref)))


def dependency_manifest(dependencies: Iterable[ProjectionDependency]) -> list[dict[str, Any]]:
    return [item.canonical() for item in canonical_dependencies(dependencies)]


def dependency_manifest_checksum(dependencies: Iterable[ProjectionDependency]) -> str:
    return _checksum(dependency_manifest(dependencies))


def projection_checksum(
    *, topic_id: str, topic_type: str, snapshot_bindings: Mapping[str, Any],
    dependencies: Iterable[ProjectionDependency], source_refs: Mapping[str, Any] | None = None,
) -> str:
    return _checksum({
        "topic_id": topic_id, "topic_type": topic_type,
        "projection_format_version": SCHEMA_VERSION,
        "snapshot_bindings": dict(snapshot_bindings),
        "dependencies": dependency_manifest(dependencies),
        "source_refs": dict(source_refs or {}),
    })


def _reason_for_change(dep: ProjectionDependency, current: Mapping[str, Any] | None) -> str:
    if current is None:
        return "dependency_missing"
    if dep.authority == "serving":
        return "serving_snapshot_changed"
    if dep.authority == "personal":
        return "personal_snapshot_changed"
    if dep.authority == "external":
        return "external_snapshot_changed"
    if dep.authority == "decision":
        return "decision_sequence_changed"
    if dep.expected_checksum and current.get("checksum") != dep.expected_checksum:
        return "dependency_checksum_mismatch"
    return "dependency_lifecycle_changed"


def classify_dependencies(
    captured: Iterable[ProjectionDependency],
    current: Iterable[ProjectionDependency],
) -> dict[str, Any]:
    captured = tuple(captured)
    current = tuple(current)
    current_by_key = {(item.authority, item.stable_ref): item for item in current}
    reasons: list[str] = []
    partial = False
    for dep in canonical_dependencies(captured):
        current_item = current_by_key.get((dep.authority, dep.stable_ref))
        if current_item is None:
            reasons.append(_reason_for_change(dep, None))
            continue
        if (
            dep.expected_version != current_item.expected_version
            or dep.expected_checksum != current_item.expected_checksum
            or dep.expected_sequence != current_item.expected_sequence
        ):
            reasons.append(_reason_for_change(dep, current_item))
            if not dep.essential:
                partial = True
    unique = tuple(dict.fromkeys(reason for reason in reasons if reason in REASON_CODES))
    if not unique:
        status = "fresh"
    elif partial and not any(dep.essential and _reason_for_change(dep, current_by_key.get((dep.authority, dep.stable_ref))) in unique for dep in captured):
        status = "partial"
    else:
        status = "stale"
    return {"status": status, "reason_codes": unique, "partial": status == "partial"}


class WikiMaterializer:
    """Explicit local materializer; no GET path should instantiate its writes."""

    def __init__(self, store_path: Path | str, *, now=None):
        self.store_path = Path(store_path)
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    def materialize(
        self, key: TopicKey, *, snapshot_bindings: Mapping[str, Any],
        dependencies: Iterable[ProjectionDependency], source_refs: Mapping[str, Any] | None = None,
        freshness_status: str = "fresh", reason_codes: Iterable[str] = (),
        projection_version: str | None = None,
    ) -> ProjectionVersion:
        if not isinstance(key, TopicKey):
            raise ValueError("canonical_topic_required")
        deps = canonical_dependencies(dependencies)
        reasons = tuple(code for code in reason_codes if code in REASON_CODES)
        topic_id = opaque_topic_id(key)
        version = projection_version or self._next_version(topic_id)
        projection = ProjectionVersion(
            topic_id=topic_id, topic_type=key.topic_type, projection_format_version=SCHEMA_VERSION,
            projection_version=version,
            projection_checksum=projection_checksum(topic_id=topic_id, topic_type=key.topic_type, snapshot_bindings=snapshot_bindings, dependencies=deps, source_refs=source_refs),
            generated_at=self.now(), freshness_status=freshness_status,
            reason_codes=reasons, snapshot_bindings=dict(snapshot_bindings),
            dependency_manifest_checksum=dependency_manifest_checksum(deps),
        )
        con = connect_rw(self.store_path)
        try:
            insert_version(con, projection, deps)
        finally:
            con.close()
        return projection

    def _next_version(self, topic_id: str) -> str:
        try:
            current, _ = latest_version(self.store_path, topic_id)
        except FileNotFoundError:
            current = None
        if current is None:
            return "pv_1"
        try:
            return f"pv_{int(current.projection_version.rsplit('_', 1)[-1]) + 1}"
        except ValueError:
            return f"pv_{_checksum([topic_id, current.projection_version])[:12]}"

    def validate_latest(self, key: TopicKey, current_dependencies: Iterable[ProjectionDependency]) -> dict[str, Any]:
        topic_id = opaque_topic_id(key)
        try:
            version, captured = latest_version(self.store_path, topic_id)
        except FileNotFoundError:
            return {"status": "missing", "reason_codes": ("projection_record_missing",), "version": None, "dependencies": ()}
        if version is None:
            return {"status": "missing", "reason_codes": ("projection_record_missing",), "version": None, "dependencies": ()}
        verdict = classify_dependencies(captured, current_dependencies)
        stored_status = version.freshness_status if version.freshness_status in {"stale", "partial", "unavailable"} else None
        if stored_status and verdict["status"] == "fresh":
            verdict = {
                **verdict,
                "status": stored_status,
                "reason_codes": tuple(version.reason_codes),
                "partial": stored_status == "partial",
            }
        return {**verdict, "version": version, "dependencies": captured}

    def rebuild(self, key: TopicKey, **kwargs: Any) -> ProjectionVersion:
        return self.materialize(key, **kwargs)


__all__ = [
    "REASON_CODES", "WikiMaterializer", "canonical_dependencies", "classify_dependencies",
    "dependency_manifest", "dependency_manifest_checksum", "projection_checksum",
]
