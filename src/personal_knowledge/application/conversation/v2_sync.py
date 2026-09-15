"""Phase 62-04: v2 conversation orchestration (dry-run / shadow / activation).

The `pk-sync conversations --v2-*` seams (62-04 plan Task 3):
:func:`probe_conversation_sources` (dry-run, metadata-only), 
:func:`shadow_conversation_generation` (capture + adapt + stage NON-active
generations + metadata-only report), and :func:`activate_conversation_generation`
(activates ONLY via :mod:`.event_generations`; fires a metadata-only post-commit
delta only after success). :func:`add_conversations_v2_args` /
:func:`cmd_conversations_v2` are the CLI surface consumed by
:mod:`.application.sync`.

Command-level fail-closed gates run before the lifecycle: uncovered sources,
blocked/privacy-gated families, unknown family, missing coverage, stale manifest
and checksum mismatch all prevent activation. This module never touches the
live canonical stores (D-15/D-31); staging goes to a caller-supplied shadow db.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.adapters.conversation_sources.contracts import (
    PROBE_CONTENT_HASH,
    AdaptationResult,
    SourceArtifact,
    SourceArtifactSet,
)
from personal_knowledge.adapters.conversation_sources.registry import (
    adapt_for,
    capability_for,
    detect_family,
    known_families,
    resolve_family,
    select_adapter,
)
from personal_knowledge.adapters.conversation_sources.snapshots import (
    capture_file,
    capture_sqlite,
)
from personal_knowledge.application.conversation.event_generations import (
    GenerationActivationError,
    GenerationLifecycle,
)
from personal_knowledge.application.conversation.event_repository import (
    GenerationInput,
)
from personal_knowledge.core.project_paths import VAR_TMP
from personal_knowledge.core.conversation_events import FidelityProfile

ACTIVATION_APPROVAL = "APPROVE_PHASE62_ACTIVATION"


# --------------------------------------------------------------------- probe


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _probe_kind(head: bytes) -> str:
    """Detect sqlite (Phase 62 D-05) vs generic file for adapter probing.

    Fixes the 62-07 seam gap: cursor/zcode/mimo/opencode/antigravity/chatgpt
    detectors require ``source_kind == "sqlite"``, yet the shadow probe hard-coded
    ``source_kind="file"`` and therefore reported every SQLite family as
    ``no_source`` in the v2 shadow seam.
    """
    if head.startswith(_SQLITE_MAGIC):
        return "sqlite"
    return "file"


def _probe_artifact(relative: str, size: int, head: bytes = b"") -> SourceArtifact:
    """A minimal artifact used only by ``select_adapter`` detection."""
    kind = _probe_kind(head) if head else "file"
    return SourceArtifact(
        artifact_id=relative,
        family="",
        source_kind=kind,
        content_hash=PROBE_CONTENT_HASH,
        capture_method="probe",
        relative_path=relative,
        byte_size=size,
    )


def probe_conversation_sources(
    *,
    source_root: Path,
    byte_limit: int = 1_000_000,
    count_limit: int = 200,
) -> dict:
    """Dry-run probe: every registered family's capability + event estimates.

    Metadata-only; nothing is staged and no canonical database is created.
    """
    import tempfile

    if not source_root.exists():
        raise FileNotFoundError(f"v2 source root missing: {source_root}")
    detected = _detect_families(source_root)

    items: list[dict] = []
    probe_temp = VAR_TMP / "conversation-probe"
    probe_temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pk-v2-probe-", dir=probe_temp) as td:
        store = Path(td)
        for name in known_families():
            owner = resolve_family(name)
            cap = capability_for(name)
            matches = detected.get(owner, [])
            status = "detected" if matches else "no_source"
            event_estimate = 0
            if status == "detected" and len(matches) == 1:
                event_estimate = _estimate_events(
                    matches[0], store, owner, byte_limit, count_limit,
                    source_root=source_root,
                )
            items.append({
                "family": name,
                "adapter_version": cap.adapter_version,
                "contract_version": cap.contract_version,
                "event_kind_count": len(cap.supported_event_kinds),
                "relation_kind_count": len(cap.supported_relation_kinds),
                "status": status,
                "snapshot_estimate": len(matches),
                "event_estimate": event_estimate,
            })
    return {
        "mode": "dry-run",
        "source_root": str(source_root),
        "probed_families": items,
    }


def _detect_families(source_root: Path) -> dict[str, list[Path]]:
    """Map each source file to its detected family (no generic fallback).

    Recurses the source root so family-mirrored staging trees
    (``<stage>/<family>/<relative>`` from native discovery) are probed too.
    Reads a bounded file head so SQLite stores (cursor/zcode/mimo/opencode/
    antigravity/chatgpt) probe with ``source_kind="sqlite"`` instead of being
    misjudged as generic files (62-07 seam gap).
    """
    detected: dict[str, list[Path]] = {}
    known = set(known_families())
    from personal_knowledge.adapters.conversation_sources.discovery import (
        CAPTURE_TEMP_PREFIXES,
        mirror_path_for,
    )

    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in (".staging", "artifacts", ".hashes.json")
        )
        parent = Path(dirpath)
        # Native staging trees mirror <stage>/<family>/<relative>: a first-level
        # directory name that is a known family scopes probing to that family,
        # so SQLite stores never bleed across families via select_adapter order.
        hint = None
        try:
            rel = parent.relative_to(source_root)
            if rel.parts and rel.parts[0] in known:
                hint = rel.parts[0]
        except ValueError:
            hint = None
        for name in sorted(filenames):
            # Capture intermediates share the stage tree with real sources but
            # are derived from one of them, so adapting them duplicates events
            # (and their ids). Never treat them as sources.
            if name == ".hashes.json" or name.startswith(CAPTURE_TEMP_PREFIXES):
                continue
            f = parent / name
            head = b""
            try:
                with f.open("rb") as handle:
                    head = handle.read(16)
            except OSError:
                continue
            probe = _probe_artifact(name, f.stat().st_size, head=head)
            if hint is not None:
                try:
                    if detect_family(hint, probe, artifact_root=parent):
                        detected.setdefault(hint, []).append(f)
                except Exception:  # noqa: BLE001 - probe failure excludes the file
                    continue
                continue
            family = select_adapter(probe, artifact_root=parent)
            if family:
                detected.setdefault(family, []).append(f)
    return detected


def _estimate_events(
    path: Path, store: Path, family: str, byte_limit: int, count_limit: int,
    *, source_root: Path | None = None,
) -> int:
    """Best-effort typed event count for a single detected file."""

    # Local import, matching the other discovery uses in this module.
    # ``mirror_path_for`` was previously referenced here without being imported,
    # so every estimate raised NameError and was swallowed into ``0``.
    from personal_knowledge.adapters.conversation_sources.discovery import (
        mirror_path_for,
    )

    try:
        mirror_path = mirror_path_for(family, path, source_root=source_root)
        artifact, blob = capture_file(
            path, store, relative_path=path.name,
            byte_limit=byte_limit, count_limit=count_limit,
            family=family, mirror_path=mirror_path,
        )
        result = adapt_for(
            family, SourceArtifactSet((artifact,)), artifact_root=blob.parent
        )
        return len(result.events)
    except Exception:  # noqa: BLE001 - estimate is best-effort
        return 0


# -------------------------------------------------------------------- shadow


def _adapt_source_file(
    path: Path, store: Path, *, byte_limit: int, count_limit: int, family: str,
    mirror_path: str,
) -> tuple[SourceArtifact, AdaptationResult]:
    """Capture one source file (file or WAL-safe SQLite) then adapt it.

    SQLite families use :func:`capture_sqlite` with the family LIVE allowlist
    (62-01 D-05/D-08) instead of a loose file copy; the probe head decides.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(len(_SQLITE_MAGIC))
    except OSError:
        head = b""
    from personal_knowledge.adapters.conversation_sources.discovery import (
        SQLITE_ALLOWLISTS,
    )

    allowlist = SQLITE_ALLOWLISTS.get(family)
    if head.startswith(_SQLITE_MAGIC) and allowlist is not None:
        tables, columns = allowlist
        artifact, blob = capture_sqlite(
            path, store, allowed_tables=tables, allowed_columns=columns,
            byte_limit=byte_limit, count_limit=count_limit,
            family=family, mirror_path=mirror_path,
        )
    else:
        artifact, blob = capture_file(
            path, store, relative_path=path.name,
            byte_limit=byte_limit, count_limit=count_limit,
            family=family, mirror_path=mirror_path,
        )
    result = adapt_for(
        family, SourceArtifactSet((artifact,)), artifact_root=blob.parent
    )
    return artifact, result


def _status_for(result: AdaptationResult, artifact: SourceArtifact) -> str:
    """full | partial | blocked for one adapted family."""
    blocked = any(
        d.startswith("blocked:") for d in artifact.privacy_dispositions
    )
    if blocked:
        return "blocked"
    if result.fidelity.has_loss() or result.warnings:
        return "partial"
    return "full"


def shadow_conversation_generation(
    *,
    source_root: Path,
    db: Path,
    artifact_store: Path,
    report_path: Path,
    byte_limit: int = 1_000_000,
    count_limit: int = 200,
) -> dict:
    """Explicit shadow: capture, adapt, and stage NON-active v2 generations.

    One staged generation per detected family. Writes a metadata-only JSON
    report (hashes/fidelity/counts, never bodies). The authority pointer is
    never touched here: activation is a separate explicit step.
    """
    if not source_root.exists():
        raise FileNotFoundError(f"v2 source root missing: {source_root}")
    artifact_store.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)

    by_family = _detect_families(source_root)
    uncovered = sorted(
        f.name for f in source_root.iterdir()
        if f.is_file() and f.name not in _all_detected_names(by_family)
    )
    life = GenerationLifecycle(db)
    generations = _stage_all_families(
        life, by_family, artifact_store, byte_limit=byte_limit,
        count_limit=count_limit, source_root=source_root,
    )
    report = {
        "mode": "shadow",
        "source_root": str(source_root),
        "created_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "generations": generations,
        "uncovered_sources": uncovered,
        "summary": {
            "full": sum(1 for g in generations.values() if g["status"] == "full"),
            "partial": sum(1 for g in generations.values() if g["status"] == "partial"),
            "blocked": sum(1 for g in generations.values() if g["status"] == "blocked"),
            "no_source": sum(1 for g in generations.values() if g["status"] == "no_source"),
        },
    }
    report["gates"] = {
        "uncovered_sources": not uncovered,
        "detected_families_unblocked": all(
            item["status"] != "blocked"
            for item in generations.values()
            if item["status"] != "no_source"
        ),
    }
    report["gates"]["overall"] = all(report["gates"].values())
    report["report_digest"] = _report_digest(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return report


def _all_detected_names(by_family: dict[str, list[Path]]) -> set[str]:
    names: set[str] = set()
    for paths in by_family.values():
        names.update(p.name for p in paths)
    return names


def _stage_all_families(
    life: GenerationLifecycle,
    by_family: dict[str, list[Path]],
    store: Path,
    *,
    byte_limit: int,
    count_limit: int,
    source_root: Path | None = None,
) -> dict[str, dict]:
    """Stage all detected owners as one atomic multi-family cohort."""

    # Local import, matching the other discovery uses in this module.
    # ``mirror_path_for`` was previously referenced here without being imported,
    # so every family raised NameError and was reported as ``adapt_failed``.
    from personal_knowledge.adapters.conversation_sources.discovery import (
        mirror_path_for,
    )

    owner_entries: dict[str, dict] = {}
    generation_inputs: list[GenerationInput] = []
    all_digests: list[str] = []
    all_hashes: list[str] = []

    for owner in sorted(set(resolve_family(name) for name in known_families())):
        matches = by_family.get(owner, [])
        entry = {
            "family": owner, "status": "no_source",
            "snapshot_count": len(matches), "event_count": 0,
            "generation_id": None, "source_manifest_id": None,
            "artifact_hashes": [], "dataset_digest": None,
            "fidelity": None, "privacy_blocked": False, "reason": None,
        }
        if matches:
            try:
                results: list[AdaptationResult] = []
                seen_content: set[str] = set()
                for path in matches:
                    mirror_path = mirror_path_for(owner, path, source_root=source_root)
                    artifact, result = _adapt_source_file(
                        path, store, byte_limit=byte_limit,
                        count_limit=count_limit, family=owner,
                        mirror_path=mirror_path,
                    )
                    # Dedup is keyed by ``content_hash`` (product decision, kept):
                    # since milestone 1 the slot ``artifact_id`` is derived from
                    # (family, mirror path), so two byte-identical files in
                    # different directories have DIFFERENT artifact ids but the
                    # SAME content hash. Two staged copies of one payload must
                    # still be adapted once, or they emit the same event ids and
                    # fail the merge contract (observed: three byte-identical
                    # grok chat_history.jsonl in distinct session dirs).
                    if artifact.content_hash in seen_content:
                        continue
                    seen_content.add(artifact.content_hash)
                    results.append(result)
                    all_hashes.append(artifact.content_hash)
                merged = _merge_family_results(owner, results)
                _assert_referential_integrity(owner, merged)
                cap = capability_for(owner)
                family_manifest = _digest("manifest", *sorted(
                    a.content_hash for a in merged.artifacts
                ))
                generation_inputs.append(GenerationInput(
                    family=merged.family,
                    adapter_version=merged.adapter_version,
                    contract_version=merged.contract_version,
                    capability_digest=cap.digest(),
                    source_manifest_id=family_manifest,
                    dataset_digest=merged.dataset_digest,
                    artifacts=merged.artifacts,
                    sessions=merged.sessions,
                    events=merged.events,
                    relations=merged.relations,
                    dispositions=merged.field_dispositions,
                    warnings=merged.warnings,
                ))
                all_digests.append(merged.dataset_digest)
                status = "partial" if merged.fidelity.has_loss() or merged.warnings else "full"
                entry.update({
                    "status": status,
                    "event_count": len(merged.events),
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
                    "reason": None,
                })
            except Exception as exc:  # noqa: BLE001 - family fails closed
                entry.update({
                    "status": "blocked",
                    "privacy_blocked": True,
                    "reason": f"adapt_failed:{type(exc).__name__}",
                })
        owner_entries[owner] = entry

    if generation_inputs:
        cohort_digest = _digest("cohort", *sorted(all_digests))
        manifest_id = _digest("manifest-cohort", *sorted(all_hashes))
        generation_id = f"shadow-cohort-{cohort_digest[:12]}"
        life.prepare_cohort(
            tuple(generation_inputs), generation_id=generation_id,
            source_manifest_id=manifest_id, dataset_digest=cohort_digest,
        )
        for entry in owner_entries.values():
            if entry["status"] in ("full", "partial"):
                entry.update({
                    "generation_id": generation_id,
                    "source_manifest_id": manifest_id,
                    "dataset_digest": cohort_digest,
                })

    return {
        name: dict(owner_entries[resolve_family(name)])
        for name in known_families()
    }


def _digest(prefix: str, *values: str) -> str:
    return hashlib.sha256("|".join((prefix, *values)).encode("utf-8")).hexdigest()


def _report_digest(report: dict) -> str:
    payload = {k: v for k, v in report.items() if k != "report_digest"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _assert_referential_integrity(family: str, merged: AdaptationResult) -> None:
    """Fail closed when a family emits rows the generation cannot carry.

    ``ce_events`` has foreign keys onto ``ce_sessions(generation_id, session_id)``
    and ``ce_source_artifacts(artifact_id)``, and SQLite's ``INSERT OR IGNORE``
    does *not* suppress foreign-key violations — a dangling reference aborts the
    whole cohort write with an opaque ``IntegrityError``. Check it per family so
    the report names the family and the offending id instead.
    """
    from personal_knowledge.core.conversation_events import EventContractError

    session_ids = {s.session_id for s in merged.sessions}
    artifact_ids = {a.artifact_id for a in merged.artifacts}
    for session in merged.sessions:
        if session.provenance.artifact_id not in artifact_ids:
            raise EventContractError(
                f"{family}: session {session.session_id} references artifact "
                f"{session.provenance.artifact_id} outside this family"
            )
    for event in merged.events:
        if event.session_id not in session_ids:
            raise EventContractError(
                f"{family}: event {event.event_id} references session "
                f"{event.session_id} with no session record"
            )
        if event.provenance.artifact_id not in artifact_ids:
            raise EventContractError(
                f"{family}: event {event.event_id} references artifact "
                f"{event.provenance.artifact_id} outside this family"
            )


def _merge_family_results(
    family: str, results: list[AdaptationResult]
) -> AdaptationResult:
    """Merge repeated native artifacts for one family without flattening them."""
    first = results[0]
    return AdaptationResult(
        family=family,
        adapter_version=first.adapter_version,
        contract_version=first.contract_version,
        artifacts=tuple(a for result in results for a in result.artifacts),
        sessions=tuple(s for result in results for s in result.sessions),
        events=tuple(e for result in results for e in result.events),
        relations=tuple(r for result in results for r in result.relations),
        field_dispositions=tuple(
            d for result in results for d in result.field_dispositions
        ),
        warnings=tuple(w for result in results for w in result.warnings),
        fidelity=FidelityProfile.worst(*(result.fidelity for result in results)),
    )


def _stage_family(
    life: GenerationLifecycle, family: str, path: Path, store: Path,
    *, byte_limit: int, count_limit: int, source_root: Path | None = None,
) -> dict:
    """Stage one family's generation and return its metadata-only entry.

    Fail closed per family: an adaptation OR staging failure marks the family
    blocked instead of aborting the whole cohort, so a single mis-shaped live
    artifact never prevents a complete metadata-only report (D-04/D-18)."""
    blocked = {
        "status": "blocked", "reason": None, "generation_id": None,
        "snapshot_count": 1, "event_count": 0, "source_manifest_id": None,
        "artifact_hashes": [], "dataset_digest": None, "fidelity": None,
        "privacy_blocked": False,
    }
    try:
        mirror_path = mirror_path_for(family, path, source_root=source_root)
        artifact, result = _adapt_source_file(
            path, store, byte_limit=byte_limit,
            count_limit=count_limit, family=family,
            mirror_path=mirror_path,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed into a blocked status
        blocked["reason"] = f"adapt_failed:{type(exc).__name__}"
        return blocked

    try:
        cap = capability_for(family)
        generation_id = f"shadow-{family}-{result.dataset_digest[:10]}"
        manifest_id = f"manifest-{family}-{artifact.content_hash[:12]}"
        gen = GenerationInput(
            family=result.family,
            adapter_version=result.adapter_version,
            contract_version=result.contract_version,
            capability_digest=cap.digest(),
            source_manifest_id=manifest_id,
            dataset_digest=result.dataset_digest,
            artifacts=result.artifacts,
            sessions=result.sessions,
            events=result.events,
            relations=result.relations,
            dispositions=result.field_dispositions,
            warnings=result.warnings,
        )
        life.prepare(gen, generation_id)
    except Exception as exc:  # noqa: BLE001 - staging write fails closed
        blocked["reason"] = f"staging_failed:{type(exc).__name__}"
        return blocked
    status = _status_for(result, artifact)
    return {
        "generation_id": generation_id,
        "status": status,
        "snapshot_count": 1,
        "event_count": len(result.events),
        "source_manifest_id": manifest_id,
        "artifact_hashes": [artifact.content_hash],
        "dataset_digest": result.dataset_digest,
        "fidelity": result.fidelity.to_dict(),
        "privacy_blocked": status == "blocked",
        "reason": None,
    }


# ---------------------------------------------------------------- activation


def activate_conversation_generation(
    *,
    db: Path,
    generation_id: str,
    report: dict,
    expected_adapter_families: tuple[str, ...],
    approval: str | None = None,
    hooks=None,
    publication_publisher=None,
    delta_publisher=None,
) -> dict:
    """Explicit activation: delegates ONLY to the generation lifecycle.

    Command-level fail-closed gates (uncovered sources, blocked/privacy gate)
    run first; unknown family / missing coverage / stale manifest / checksum
    mismatch are enforced by :class:`GenerationLifecycle`. The delta is
    metadata-only and fires only after success."""
    entry = _report_entry(report, generation_id)
    if entry.get("status") == "blocked" or entry.get("privacy_blocked"):
        raise GenerationActivationError(
            f"privacy_gate_blocked: privacy gate blocks activation of "
            f"generation {generation_id}",
            generation_id=generation_id, reason="privacy_gate_blocked",
        )
    uncovered = report.get("uncovered_sources") or []
    if uncovered:
        raise GenerationActivationError(
            f"uncovered_sources prevent activation: {uncovered}",
            generation_id=generation_id, reason="uncovered_sources",
        )
    if report.get("report_digest") != _report_digest(report):
        raise GenerationActivationError(
            "shadow report digest mismatch",
            generation_id=generation_id, reason="report_digest_mismatch",
        )
    if not (report.get("gates") or {}).get("overall"):
        raise GenerationActivationError(
            "shadow report gates are not ready",
            generation_id=generation_id, reason="shadow_gates_not_ready",
        )
    if approval != ACTIVATION_APPROVAL:
        raise GenerationActivationError(
            "exact human activation approval is required",
            generation_id=generation_id, reason="human_approval_required",
        )

    families = expected_adapter_families or (entry["family"],)
    _validate_cohort_report(report, generation_id, tuple(families))
    life = GenerationLifecycle(db)
    prior_generation_id = life.authority_generation_id()
    result = life.activate(
        generation_id,
        source_manifest_id=entry["source_manifest_id"],
        expected_dataset_digest=entry["dataset_digest"],
        expected_adapter_families=tuple(families),
        hooks=hooks,
    )
    publications: list[dict] = []
    if publication_publisher is not None:
        try:
            publications = list(publication_publisher())
        except Exception as exc:  # noqa: BLE001 - compensate cross-store failure
            restored = False
            try:
                if prior_generation_id is None:
                    life.deactivate()
                else:
                    life.rollback_to(prior_generation_id)
                restored = True
            except Exception:
                restored = False
            raise GenerationActivationError(
                "publication binding failed after canonical activation; "
                f"prior state restored={restored}: {exc}",
                generation_id=generation_id,
                reason=f"publication_failed:{type(exc).__name__}",
                restored=restored,
            ) from exc
    result["publications"] = publications
    delta = {"published": False, "reason": "v2_delta_not_configured"}
    if delta_publisher is not None:
        delta = delta_publisher({
            "generation_id": generation_id,
            "delta_id": result["delta_id"],
            "source_manifest_id": entry["source_manifest_id"],
            "projection_digest": result["projection_digest"],
            "dataset_digest": entry["dataset_digest"],
            "artifact_hashes": entry.get("artifact_hashes", []),
            "event_count": entry.get("event_count", 0),
        })
    result["delta"] = delta
    return result


def _validate_cohort_report(
    report: dict, generation_id: str, families: tuple[str, ...]
) -> None:
    entries = report.get("generations") or {}
    for family in families:
        item = entries.get(family)
        if item is None:
            try:
                owner = resolve_family(family)
            except KeyError as exc:
                raise GenerationActivationError(
                    f"unknown_adapter:{family}", generation_id=generation_id,
                    reason=f"unknown_adapter:{family}",
                ) from exc
            item = next(
                (value for value in entries.values() if value.get("family") == owner),
                None,
            )
        if item is None or item.get("generation_id") != generation_id:
            raise GenerationActivationError(
                f"missing_family_coverage:{family} is not bound to cohort "
                f"{generation_id}", generation_id=generation_id,
                reason=f"missing_family_coverage:{family}",
            )
        if item.get("status") not in ("full", "partial"):
            raise GenerationActivationError(
                f"family {family} is not activatable: {item.get('status')}",
                generation_id=generation_id, reason="cohort_family_blocked",
            )


def _report_entry(report: dict, generation_id: str) -> dict:
    """Find the generation entry in the shadow report; fail closed if absent."""
    for family, item in (report.get("generations") or {}).items():
        if item.get("generation_id") == generation_id:
            entry = dict(item)
            entry["family"] = family
            return entry
    raise GenerationActivationError(
        f"generation {generation_id} is not in the shadow report",
        generation_id=generation_id, reason="generation_not_in_report",
    )


# ------------------------------------------------------------------- CLI glue


def add_conversations_v2_args(parser: argparse.ArgumentParser) -> None:
    """Add the additive Phase 62-04 v2 flags (explicit/opt-in; default behavior
    unchanged until Plan 62-08)."""
    parser.add_argument(
        "--v2-dry-run",
        action="store_true",
        help="Phase 62 v2: probe every family capability and snapshot/event "
             "estimate (metadata-only, no canonical writes)",
    )
    parser.add_argument(
        "--v2-native",
        action="store_true",
        help="Phase 62 v2: discover machine-local client directories, stage new "
             "and changed files, then run a NON-active shadow (never activates)",
    )
    parser.add_argument(
        "--v2-native-dry-run",
        action="store_true",
        help="Phase 62 v2: metadata-only discovery report (no capture, no staging)",
    )
    parser.add_argument(
        "--v2-stage",
        type=Path,
        default=Path("data") / "staging" / "v2" / "native",
        help="Phase 62 v2: native staging root (family-mirrored files)",
    )
    parser.add_argument(
        "--v2-byte-limit",
        type=int,
        default=600_000_000,
        help="Phase 62 v2: per-artifact byte limit for capture (default 600MB, "
             "covers the zcode live store snapshot)",
    )
    parser.add_argument(
        "--v2-shadow",
        action="store_true",
        help="Phase 62 v2: capture sources, adapt, and stage NON-active v2 "
             "generations plus a metadata-only report",
    )
    parser.add_argument(
        "--v2-activate",
        metavar="GENERATION_ID",
        default=None,
        help="Phase 62 v2: explicitly activate a staged generation (delegates "
             "only to event_generations; default never activates)",
    )
    parser.add_argument(
        "--v2-source",
        type=Path,
        default=None,
        help="Phase 62 v2: source root for v2 dry-run/shadow (default: none)",
    )
    parser.add_argument(
        "--v2-db",
        type=Path,
        default=Path("data") / "staging" / "v2" / "agent_conversations_v2.sqlite",
        help="Phase 62 v2: shadow database (default: data/staging/v2, never "
             "the live canonical store)",
    )
    parser.add_argument(
        "--v2-artifact-store",
        type=Path,
        default=Path("data") / "staging" / "v2" / "artifacts",
        help="Phase 62 v2: content-addressed artifact store",
    )
    parser.add_argument(
        "--v2-report",
        type=Path,
        default=Path("data") / "staging" / "v2" / "report.json",
        help="Phase 62 v2: metadata-only shadow report path",
    )
    parser.add_argument(
        "--v2-families",
        default=None,
        help="Phase 62 v2: comma-separated expected adapter families for "
             "activation (default: the generation's own family)",
    )
    parser.add_argument(
        "--v2-approval",
        default=None,
        help="Exact local human checkpoint phrase required for v2 activation",
    )


def cmd_conversations_v2(args) -> int:
    """CLI routing for the explicit v2 modes (dry-run / shadow / activation).

    Metadata-only outputs; writes only to the caller-supplied shadow database
    (D-15/D-31, zero-paid)."""
    if args.v2_native_dry_run:
        from personal_knowledge.adapters.conversation_sources.discovery import (
            discover_client_sources,
        )

        found = discover_client_sources()
        report = {
            "mode": "native-dry-run",
            "detected": {
                family: sorted(str(p) for p in paths)
                for family, paths in sorted(found.items()) if paths
            },
            "no_source": sorted(
                family for family, paths in found.items() if not paths
            ),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.v2_native:
        from personal_knowledge.adapters.conversation_sources.discovery import (
            stage_client_sources,
        )

        staged = stage_client_sources(
            stage_root=args.v2_stage, byte_limit=args.v2_byte_limit,
        )
        print(json.dumps(staged, ensure_ascii=False, indent=2))
        if staged["staged"] == 0 and staged["skipped"] == 0:
            print("[native] nothing discovered to stage; no shadow run.")
            return 0
        report = shadow_conversation_generation(
            source_root=args.v2_stage,
            db=args.v2_db,
            artifact_store=args.v2_artifact_store,
            report_path=args.v2_report,
            byte_limit=args.v2_byte_limit,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n[native] metadata-only shadow report: {args.v2_report}")
        print("[native] no generation activated; use --v2-activate explicitly.")
        return 0

    if args.v2_dry_run:
        report = probe_conversation_sources(source_root=args.v2_source)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.v2_shadow:
        report = shadow_conversation_generation(
            source_root=args.v2_source,
            db=args.v2_db,
            artifact_store=args.v2_artifact_store,
            report_path=args.v2_report,
            byte_limit=args.v2_byte_limit,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n[shadow] metadata-only report: {args.v2_report}")
        print("[shadow] no generation activated; use --v2-activate explicitly.")
        return 0

    if args.v2_activate:
        if not args.v2_report.exists():
            print(f"[error] v2 shadow report missing: {args.v2_report}")
            return 1
        try:
            report = json.loads(args.v2_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[error] cannot read v2 report: {exc}")
            return 1
        families = tuple(
            f.strip() for f in (args.v2_families or "").split(",") if f.strip()
        ) or None
        try:
            publication_publisher = None
            from personal_knowledge.core.project_paths import (
                AGENT_CONVERSATIONS_DB,
                UNIFIED_DB,
            )

            if Path(args.v2_db).resolve() == AGENT_CONVERSATIONS_DB.resolve():
                from personal_knowledge.application.serving.versions import (
                    record_conversation_publications,
                )

                publication_publisher = lambda: record_conversation_publications(
                    UNIFIED_DB, AGENT_CONVERSATIONS_DB
                )
            result = activate_conversation_generation(
                db=args.v2_db,
                generation_id=args.v2_activate,
                report=report,
                expected_adapter_families=tuple(families) if families else (),
                approval=args.v2_approval,
                publication_publisher=publication_publisher,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on the command line
            print(f"[error] v2 activation blocked: {exc}")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("[error] internal: unreachable v2 mode")
    return 2


__all__ = [
    "activate_conversation_generation",
    "ACTIVATION_APPROVAL",
    "add_conversations_v2_args",
    "cmd_conversations_v2",
    "probe_conversation_sources",
    "shadow_conversation_generation",
]
