"""Build a zero-provider Phase 62 shadow cohort from live native locators."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.adapters.conversation_sources import (
    antigravity, mimo_opencode, zcode,
)
from personal_knowledge.adapters.conversation_sources.contracts import (
    AdaptationResult, SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.registry import (
    adapt_for, capability_for, known_families, resolve_family,
)
from personal_knowledge.adapters.conversation_sources.snapshots import (
    capture_file, capture_sqlite,
)
from personal_knowledge.application.conversation.agentsview_unavailable_snapshot import (
    capture_pathless_agent_snapshot,
)
from personal_knowledge.application.conversation.event_generations import GenerationLifecycle
from personal_knowledge.application.conversation.event_repository import GenerationInput
from personal_knowledge.application.conversation.native_inventory import (
    inventory_summary, read_native_inventory,
)
from personal_knowledge.core.conversation_events import FidelityProfile

_SQLITE_POLICIES = {
    "mimo": (mimo_opencode.LIVE_ALLOWED_TABLES, mimo_opencode.LIVE_ALLOWED_COLUMNS),
    "opencode": (mimo_opencode.LIVE_ALLOWED_TABLES, mimo_opencode.LIVE_ALLOWED_COLUMNS),
    "zcode": (zcode.LIVE_ALLOWED_TABLES, zcode.LIVE_ALLOWED_COLUMNS),
    "antigravity": (antigravity.LIVE_ALLOWED_TABLES, antigravity.LIVE_ALLOWED_COLUMNS),
}


def build_live_native_shadow(
    *, agentsview_db: Path, db: Path, artifact_store: Path, report_path: Path,
) -> dict:
    """Capture every unique available native file and stage one cohort.

    Pathless ChatGPT and Grok rows are captured through immutable, family- and
    column-filtered AgentsView observations. Native artifacts retain authority;
    the filtered SQLite rows remain honest compatibility observations. No
    provider is called.
    """
    rows = read_native_inventory(agentsview_db)
    inventory = inventory_summary(rows)
    by_family: dict[str, list[Path]] = {}
    for row in rows:
        if row.source_exists and row.source_path is not None:
            by_family.setdefault(row.family, []).append(row.source_path)
    by_family = {
        family: sorted(set(paths), key=lambda p: str(p).lower())
        for family, paths in by_family.items()
    }
    unavailable_session_ids: dict[str, tuple[str, ...]] = {
        family: tuple(sorted(
            row.session_id for row in rows
            if row.family == family and not row.source_exists
        ))
        for family in ("chatgpt", "grok")
    }

    pathless_results = {
        family: _pathless_observation(
            agentsview_db, artifact_store, family=family,
            session_ids=unavailable_session_ids[family],
        )
        for family in ("chatgpt", "grok")
        if unavailable_session_ids[family]
    }

    inputs: list[GenerationInput] = []
    entries: dict[str, dict] = {}
    family_results: dict[str, AdaptationResult] = {}
    for owner in sorted(set(resolve_family(name) for name in known_families())):
        paths = by_family.get(owner, [])
        item = {
            "family": owner, "status": "no_source",
            "snapshot_count": 0, "event_count": 0, "session_count": 0,
            "native_snapshot_count": 0,
            "compatibility_observation_count": 0,
            "compatibility_observation_session_count": 0,
            "generation_id": None, "source_manifest_id": None,
            "artifact_hashes": [], "dataset_digest": None,
            "family_dataset_digest": None, "fidelity": None,
            "privacy_blocked": False, "reason": None,
            "discovered_sessions": inventory.get(owner, {}).get("sessions", 0),
            "native_available_sessions": inventory.get(owner, {}).get(
                "native_available_sessions", 0
            ),
        }
        if paths:
            try:
                native_results = [
                    _capture_adapt(owner, path, artifact_store) for path in paths
                ]
                observation = pathless_results.get(owner)
                results = [
                    *native_results,
                    *((observation,) if observation is not None else ()),
                ]
                merged = _merge(owner, results)
                family_results[owner] = merged
                cap = capability_for(owner)
                manifest = _digest("family-manifest", *sorted(
                    artifact.content_hash for artifact in merged.artifacts
                ))
                inputs.append(GenerationInput(
                    family=owner, adapter_version=merged.adapter_version,
                    contract_version=merged.contract_version,
                    capability_digest=cap.digest(), source_manifest_id=manifest,
                    dataset_digest=merged.dataset_digest, artifacts=merged.artifacts,
                    sessions=merged.sessions, events=merged.events,
                    relations=merged.relations,
                    dispositions=merged.field_dispositions,
                    warnings=merged.warnings,
                ))
                item.update({
                    "status": "partial" if merged.fidelity.has_loss() or merged.warnings else "full",
                    "snapshot_count": len(merged.artifacts),
                    "native_snapshot_count": sum(
                        len(result.artifacts) for result in native_results
                    ),
                    "compatibility_observation_count": (
                        len(observation.artifacts) if observation is not None else 0
                    ),
                    "compatibility_observation_session_count": (
                        len(observation.sessions) if observation is not None else 0
                    ),
                    "event_count": len(merged.events),
                    "session_count": len(merged.sessions),
                    "artifact_hashes": sorted(a.content_hash for a in merged.artifacts),
                    "artifact_refs": [
                        {"artifact_id": a.artifact_id,
                         "family": a.family,
                         "content_hash": a.content_hash,
                         "relative_path": a.relative_path,
                         "source_kind": a.source_kind,
                         "byte_size": a.byte_size,
                         "schema_digest": a.schema_digest,
                         "privacy_dispositions": list(a.privacy_dispositions)}
                        for a in sorted(merged.artifacts, key=lambda a: a.artifact_id)
                    ],
                    "family_dataset_digest": merged.dataset_digest,
                    "fidelity": merged.fidelity.to_dict(),
                })
            except Exception as exc:  # noqa: BLE001 - explicit fail-closed entry
                item.update({
                    "status": "blocked", "privacy_blocked": True,
                    "reason": f"{type(exc).__name__}:{str(exc)[:160]}",
                })
        elif owner in pathless_results:
            merged = pathless_results[owner]
            family_results[owner] = merged
            cap = capability_for(owner)
            manifest = _digest(
                "family-manifest", *(a.content_hash for a in merged.artifacts)
            )
            inputs.append(GenerationInput(
                family=owner, adapter_version=merged.adapter_version,
                contract_version=merged.contract_version,
                capability_digest=cap.digest(), source_manifest_id=manifest,
                dataset_digest=merged.dataset_digest, artifacts=merged.artifacts,
                sessions=merged.sessions, events=merged.events,
                relations=merged.relations,
                dispositions=merged.field_dispositions, warnings=merged.warnings,
            ))
            item.update({
                "status": "partial", "snapshot_count": len(merged.artifacts),
                "native_snapshot_count": 0,
                "compatibility_observation_count": len(merged.artifacts),
                "compatibility_observation_session_count": len(merged.sessions),
                "event_count": len(merged.events),
                "session_count": len(merged.sessions),
                "artifact_hashes": [a.content_hash for a in merged.artifacts],
                "artifact_refs": [
                    {"artifact_id": a.artifact_id,
                     "family": a.family,
                     "content_hash": a.content_hash,
                     "relative_path": a.relative_path,
                     "source_kind": a.source_kind,
                     "byte_size": a.byte_size,
                     "schema_digest": a.schema_digest,
                     "privacy_dispositions": list(a.privacy_dispositions)}
                    for a in merged.artifacts
                ],
                "family_dataset_digest": merged.dataset_digest,
                "fidelity": merged.fidelity.to_dict(),
            })
        entries[owner] = item

    if not inputs:
        raise RuntimeError("no live native artifacts could be adapted")
    cohort_digest = _digest("cohort", *sorted(r.dataset_digest for r in family_results.values()))
    manifest_id = _digest("cohort-manifest", *sorted(
        a.content_hash for r in family_results.values() for a in r.artifacts
    ))
    generation_id = f"live-cohort-{cohort_digest[:16]}"
    db.parent.mkdir(parents=True, exist_ok=True)
    GenerationLifecycle(db).prepare_cohort(
        tuple(inputs), generation_id=generation_id,
        source_manifest_id=manifest_id, dataset_digest=cohort_digest,
    )
    for item in entries.values():
        if item["status"] in ("full", "partial"):
            item.update({
                "generation_id": generation_id,
                "source_manifest_id": manifest_id,
                "dataset_digest": cohort_digest,
            })

    generations = {
        name: dict(entries[resolve_family(name)]) for name in known_families()
    }
    report = {
        "mode": "live_native_shadow", "created_at": _now(),
        "source_root": str(agentsview_db), "generation_id": generation_id,
        "inventory": inventory, "generations": generations,
        "uncovered_sources": [],
        "summary": {
            status: sum(1 for item in generations.values() if item["status"] == status)
            for status in ("full", "partial", "blocked", "no_source")
        },
        "paid_calls": 0,
    }
    report["gates"] = {
        "all_available_files_captured": all(
            item["status"] == "blocked" or
            item["native_snapshot_count"] == inventory.get(owner, {}).get("unique_files", 0)
            for owner, item in entries.items()
        ),
        "detected_families_unblocked": not any(
            item["status"] == "blocked" for item in entries.values()
        ),
        "all_unavailable_sessions_observed": all(
            entries[family]["status"] == "blocked" or
            entries[family]["compatibility_observation_session_count"]
            == len(unavailable_session_ids[family])
            for family in ("chatgpt", "grok")
        ),
        "paid_calls_zero": True,
    }
    report["gates"]["overall"] = all(report["gates"].values())
    report["report_digest"] = _report_digest(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return report


def _capture_adapt(family: str, source: Path, store: Path) -> AdaptationResult:
    dest = store / family / hashlib.sha256(str(source).encode()).hexdigest()[:12]
    if family in _SQLITE_POLICIES:
        tables, columns = _SQLITE_POLICIES[family]
        artifact, blob = capture_sqlite(
            source, dest, allowed_tables=tables, allowed_columns=columns,
            byte_limit=1_000_000_000, count_limit=16,
        )
    else:
        artifact, blob = capture_file(
            source, dest, relative_path=source.name,
            byte_limit=max(source.stat().st_size + 1, 1_000_000), count_limit=1,
        )
    # Consolidate blobs into the root store expected by fidelity replay.
    root_blob = store / "artifacts" / artifact.content_hash[:32]
    root_blob.parent.mkdir(parents=True, exist_ok=True)
    if not root_blob.exists():
        shutil.copy2(blob, root_blob)
    return adapt_for(
        family, SourceArtifactSet((artifact,)), artifact_root=root_blob.parent
    )


def _pathless_observation(
    agentsview_db: Path,
    store: Path,
    *,
    family: str,
    session_ids: tuple[str, ...],
) -> AdaptationResult:
    """Capture one family's declared pathless AgentsView rows and columns."""
    artifact, blob = capture_pathless_agent_snapshot(
        agentsview_db, store / family, family=family,
        session_ids=session_ids,
    )
    root_blob = store / "artifacts" / artifact.content_hash[:32]
    root_blob.parent.mkdir(parents=True, exist_ok=True)
    if not root_blob.exists():
        shutil.copy2(blob, root_blob)
    return adapt_for(
        family, SourceArtifactSet((artifact,)), artifact_root=root_blob.parent
    )


def _merge(family: str, results: list[AdaptationResult]) -> AdaptationResult:
    first = results[0]
    return AdaptationResult(
        family=family, adapter_version=first.adapter_version,
        contract_version=first.contract_version,
        artifacts=tuple(a for r in results for a in r.artifacts),
        sessions=tuple(s for r in results for s in r.sessions),
        events=tuple(e for r in results for e in r.events),
        relations=tuple(rel for r in results for rel in r.relations),
        field_dispositions=tuple(d for r in results for d in r.field_dispositions),
        warnings=tuple(w for r in results for w in r.warnings),
        fidelity=FidelityProfile.worst(*(r.fidelity for r in results)),
    )


def _digest(prefix: str, *values: str) -> str:
    return hashlib.sha256("|".join((prefix, *values)).encode()).hexdigest()


def _report_digest(report: dict) -> str:
    value = {key: val for key, val in report.items() if key != "report_digest"}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = ["build_live_native_shadow"]
