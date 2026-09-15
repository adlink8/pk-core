"""Phase 62-04: generation lifecycle owner (stage -> validate -> activate -> rollback).

The sole owner of the v2 activation state machine (Phase 62 CONTEXT D-18):

  - :meth:`GenerationLifecycle.prepare` — stage a complete generation
  - :meth:`GenerationLifecycle.validate` — schema/FK/digest/provenance/fidelity/
    adapter-coverage gate
  - :meth:`GenerationLifecycle.activate` — build and validate the compatibility
    projection, run the optional consumer-parity gate, then commit the event
    authority pointer + compatibility projection + version/watermark/fingerprint
    binding in ONE transaction
  - :meth:`GenerationLifecycle.rollback_to` — restore an existing staged
    generation as the active authority (the exact-restore path used by the
    sync command's rollback target)

Fail-closed semantics: any failure before the commit, or any failure after the
authority pointer is written (projection / pointer / version binding), rolls the
whole transaction back and restores the exact prior authority rows, compatibility
tables, version/watermark and fingerprint. Old generation rows and the
append-only activation audit log are preserved, never deleted.

Injected failure seams (:class:`ActivationHooks`) let callers/tests provoke
failures at every stage; the defaults are the production implementations. This
module never calls a provider and never touches live canonical data (D-31).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from personal_knowledge.application.conversation.compatibility_projection import (
    CompatibilityProjectionError,
    CompatibilityProjectionReport,
    build_compatibility_projection,
    clear_compatibility_projection,
    write_compatibility_projection,
)
from personal_knowledge.application.conversation.event_repository import (
    EventRepository,
    GenerationInput,
)


class GenerationActivationError(RuntimeError):
    """Activation failed and prior canonical state was restored (fail closed)."""

    def __init__(
        self,
        message: str,
        *,
        generation_id: str | None = None,
        reason: str | None = None,
        restored: bool = True,
    ) -> None:
        super().__init__(message)
        self.generation_id = generation_id
        self.reason = reason or message
        self.restored = restored


@dataclass(frozen=True)
class ActivationHooks:
    """Injected stage implementations; ``None`` means the production default.

    - ``projection_builder``: ``() -> CompatibilityProjectionReport``
    - ``consumer_parity``: ``() -> dict`` with ``ok``/``reason``; a non-ok result
      blocks activation before any commit
    - ``authority_writer``: ``(con) -> None`` (writes the active pointer)
    - ``projection_writer``: ``(con, report) -> None``
    - ``version_binder``: ``(con, report) -> None``
    """

    projection_builder: Callable[[], CompatibilityProjectionReport] | None = None
    consumer_parity: Callable[[], dict] | None = None
    authority_writer: Callable[[sqlite3.Connection], None] | None = None
    projection_writer: Callable[
        [sqlite3.Connection, CompatibilityProjectionReport], None
    ] | None = None
    version_binder: Callable[
        [sqlite3.Connection, CompatibilityProjectionReport], None
    ] | None = None


_BINDING_KINDS = ("projection_version", "projection_watermark", "projection_fingerprint")


class GenerationLifecycle:
    """Stage, validate, activate and roll back v2 event generations."""

    def __init__(self, db: Path) -> None:
        self.db = Path(db)
        self._repo = EventRepository(self.db)

    # --------------------------------------------------------------- staging

    def prepare(self, gen: GenerationInput, generation_id: str) -> str:
        """Stage a complete generation (idempotent replay)."""
        self._repo.create_schema()
        return self._repo.write_generation(gen, generation_id)

    def prepare_cohort(
        self,
        generations: tuple[GenerationInput, ...],
        *,
        generation_id: str,
        source_manifest_id: str,
        dataset_digest: str,
    ) -> str:
        """Stage one multi-family authority generation atomically."""
        self._repo.create_schema()
        return self._repo.write_generation_cohort(
            generations,
            generation_id=generation_id,
            source_manifest_id=source_manifest_id,
            dataset_digest=dataset_digest,
        )

    def authority_generation_id(self) -> str | None:
        """Read-only: the currently active generation, if any."""
        return self._repo.authority_generation_id()

    # ------------------------------------------------------------- validation

    def validate(
        self,
        generation_id: str,
        *,
        source_manifest_id: str | None = None,
        expected_dataset_digest: str | None = None,
        expected_adapter_families: tuple[str, ...] | None = None,
    ) -> dict:
        """Fail-closed gate over schema/FK/digests/provenance/fidelity/coverage."""
        checks: dict = {
            "exists": False,
            "integrity": "absent",
            "source_manifest": "unchecked",
            "dataset_digest": "unchecked",
            "adapter_coverage": {"expected": 0, "covered": 0,
                                 "unknown": [], "missing": []},
            "provenance": {"events": 0, "unprovenanced": 0},
            "fidelity": {"sessions": 0, "invalid": 0},
        }
        con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT source_manifest_id, dataset_digest FROM ce_event_generations "
                "WHERE generation_id=?", (generation_id,)
            ).fetchone()
            if row is None:
                return self._fail(generation_id, "generation_absent", checks)
            checks["exists"] = True
            integrity = self._repo.validate_generation(generation_id)
            checks["integrity"] = "ok" if integrity["ok"] else "invalid"
            if not integrity["ok"]:
                return self._fail(generation_id, "integrity_or_fk", checks)
            failure = self._manifest_digest_gate(
                generation_id, row, checks,
                source_manifest_id, expected_dataset_digest,
            )
            if failure:
                return self._fail(generation_id, failure, checks)
            if expected_adapter_families:
                self._check_adapter_coverage(con, generation_id,
                                             expected_adapter_families, checks)
                unknown = checks["adapter_coverage"]["unknown"]
                missing = checks["adapter_coverage"]["missing"]
                if unknown or missing:
                    failure = (
                        f"unknown_adapter:{','.join(unknown)}" if unknown
                        else f"missing_family_coverage:{','.join(missing)}"
                    )
                    return self._fail(generation_id, failure, checks)
            self._check_provenance_fidelity(con, generation_id, checks)
            if checks["provenance"]["unprovenanced"] or checks["fidelity"]["invalid"]:
                return self._fail(
                    generation_id, "unprovenanced_or_invalid_fidelity", checks
                )
            return {
                "ok": True, "generation_id": generation_id,
                "failure": None, "checks": checks,
            }
        except sqlite3.Error as exc:
            return self._fail(
                generation_id, f"v2_schema_unavailable:{type(exc).__name__}",
                checks,
            )
        finally:
            con.close()

    def _manifest_digest_gate(
        self, generation_id: str, row, checks: dict,
        source_manifest_id: str | None, expected_dataset_digest: str | None,
    ) -> str | None:
        """Stale-manifest / checksum gates. Returns a failure reason or None."""
        if source_manifest_id is not None:
            match = row["source_manifest_id"] == source_manifest_id
            checks["source_manifest"] = "match" if match else "stale"
            if not match:
                return "stale_source_manifest"
        if expected_dataset_digest is not None:
            match = row["dataset_digest"] == expected_dataset_digest
            checks["dataset_digest"] = "match" if match else "mismatch"
            if not match:
                return "dataset_checksum_mismatch"
        return None

    def _fail(self, generation_id: str, failure: str, checks: dict) -> dict:
        return {
            "ok": False, "generation_id": generation_id,
            "failure": failure, "checks": checks,
        }

    def _check_adapter_coverage(self, con, generation_id, families, checks) -> None:
        from personal_knowledge.adapters.conversation_sources.registry import (
            resolve_family,
        )

        runs = {
            str(r["family"]) for r in con.execute(
                "SELECT family FROM ce_adapter_runs WHERE generation_id=?",
                (generation_id,),
            )
        }
        unknown: list[str] = []
        missing: list[str] = []
        covered = 0
        for family in families:
            try:
                resolved = resolve_family(family)
            except KeyError:
                unknown.append(family)
                continue
            if resolved in runs:
                covered += 1
            else:
                missing.append(resolved)
        checks["adapter_coverage"] = {
            "expected": len(families), "covered": covered,
            "unknown": unknown, "missing": missing,
        }

    def _check_provenance_fidelity(self, con, generation_id, checks) -> None:
        from personal_knowledge.core.conversation_events import (
            FidelityProfile,
        )

        unprovenanced = 0
        event_count = 0
        for r in con.execute(
            "SELECT artifact_id, native_locator FROM ce_events "
            "WHERE generation_id=?", (generation_id,)
        ):
            event_count += 1
            if not r["artifact_id"] or not r["native_locator"]:
                unprovenanced += 1
        checks["provenance"] = {"events": event_count,
                                "unprovenanced": unprovenanced}
        invalid = 0
        session_count = 0
        for r in con.execute(
            "SELECT fidelity_json FROM ce_sessions WHERE generation_id=?",
            (generation_id,),
        ):
            session_count += 1
            try:
                FidelityProfile.from_dict(
                    json.loads(r["fidelity_json"])
                )
            except (ValueError, KeyError, TypeError):
                invalid += 1
        checks["fidelity"] = {"sessions": session_count, "invalid": invalid}

    # ------------------------------------------------------------- activation

    def activate(
        self,
        generation_id: str,
        *,
        source_manifest_id: str,
        expected_dataset_digest: str,
        expected_adapter_families: tuple[str, ...],
        hooks: ActivationHooks | None = None,
    ) -> dict:
        """Validate then atomically commit the generation as the active authority.

        Any failure restores the exact prior authority/projection/version state
        and raises :class:`GenerationActivationError` (fail closed).
        """
        hooks = hooks or ActivationHooks()
        gate = self.validate(
            generation_id,
            source_manifest_id=source_manifest_id,
            expected_dataset_digest=expected_dataset_digest,
            expected_adapter_families=expected_adapter_families,
        )
        if not gate["ok"]:
            self._repo.record_attempt_log(
                generation_id, "failure", gate["failure"]
            )
            raise GenerationActivationError(
                f"activation blocked: {gate['failure']}",
                generation_id=generation_id, reason=gate["failure"],
            )
        return self._commit(generation_id, hooks=hooks)

    def rollback_to(
        self, generation_id: str, *, hooks: ActivationHooks | None = None
    ) -> dict:
        """Restore an existing generation as the active authority.

        Rebuilds its projection and version/watermark/fingerprint binding;
        a failure restores the prior state and raises (fail closed).
        """
        hooks = hooks or ActivationHooks()
        con = sqlite3.connect(str(self.db))
        try:
            exists = con.execute(
                "SELECT 1 FROM ce_event_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
        finally:
            con.close()
        if not exists:
            self._repo.record_attempt_log(
                generation_id, "failure", "rollback_target_absent"
            )
            raise GenerationActivationError(
                f"rollback target absent: {generation_id}",
                generation_id=generation_id, reason="rollback_target_absent",
            )
        return self._commit(generation_id, hooks=hooks)

    def deactivate(self) -> dict:
        """Restore the pre-v2 state when no prior generation existed.

        Staged event generations remain recoverable, while the active pointer,
        v2 compatibility rows and activation bindings are removed together.
        Pre-existing legacy canonical rows are preserved by the projection
        cleanup contract.
        """
        prior = self._repo.authority_generation_id()
        con = sqlite3.connect(str(self.db))
        try:
            con.execute("BEGIN")
            clear_compatibility_projection(con)
            con.execute("UPDATE ce_generation_authority SET active=0 WHERE active=1")
            con.execute("DELETE FROM ce_activation_bindings")
            con.commit()
        except Exception as exc:  # noqa: BLE001 - fail closed with exact rollback
            con.rollback()
            raise GenerationActivationError(
                f"deactivation failed and prior state restored: {exc}",
                generation_id=prior or "none",
                reason=f"deactivation_failed:{type(exc).__name__}",
            ) from exc
        finally:
            con.close()
        return {"prior_generation_id": prior, "active_generation_id": None}

    # ---------------------------------------------------------- commit block

    def _commit(self, generation_id: str, *, hooks: ActivationHooks) -> dict:
        prior = self._repo.authority_generation_id()
        try:
            report = self._build_projection(generation_id, hooks)
            if hooks.consumer_parity is not None:
                parity = hooks.consumer_parity()
                if not parity.get("ok"):
                    reason = str(parity.get("reason") or "consumer_parity_failed")
                    self._repo.record_attempt_log(
                        generation_id, "failure", f"consumer_parity:{reason}"
                    )
                    raise GenerationActivationError(
                        f"consumer parity blocked activation: {reason}",
                        generation_id=generation_id, reason=reason,
                    )
        except GenerationActivationError:
            raise
        except Exception as exc:  # noqa: BLE001 - projection build fails closed
            self._repo.record_attempt_log(
                generation_id, "failure", f"projection_build:{type(exc).__name__}"
            )
            raise GenerationActivationError(
                f"projection build failed: {exc}", generation_id=generation_id
            ) from exc

        con = sqlite3.connect(str(self.db))
        try:
            con.execute("BEGIN IMMEDIATE")
            prior_row = con.execute(
                "SELECT generation_id FROM ce_generation_authority WHERE active=1"
            ).fetchone()
            prior = prior_row[0] if prior_row else None
            self._run_commit(con, generation_id, report, hooks)
            from personal_knowledge.application.conversation.generation_delta import record_generation_delta
            delta_id = record_generation_delta(con, generation_id, prior)
            con.commit()
        except GenerationActivationError:
            con.rollback()
            self._repo.record_attempt_log(
                generation_id, "failure", "consumer_parity"
            )
            raise
        except Exception as exc:  # noqa: BLE001 - restore prior state, fail closed
            con.rollback()
            self._repo.record_attempt_log(
                generation_id, "failure",
                f"commit_restored:{type(exc).__name__}",
            )
            raise GenerationActivationError(
                f"activation failed and prior state restored: {exc}",
                generation_id=generation_id,
                reason=f"commit_failed:{type(exc).__name__}",
            ) from exc
        finally:
            con.close()
        self._repo.record_attempt_log(generation_id, "success")
        return {
            "generation_id": generation_id,
            "prior_generation_id": prior,
            "projection_digest": report.fingerprint.digest,
            "delta_id": delta_id,
        }

    def _build_projection(
        self, generation_id: str, hooks: ActivationHooks
    ) -> CompatibilityProjectionReport:
        if hooks.projection_builder is not None:
            report = hooks.projection_builder()
        else:
            report = build_compatibility_projection(self.db, generation_id)
        if not isinstance(report, CompatibilityProjectionReport):
            raise TypeError(
                "projection_builder must return CompatibilityProjectionReport"
            )
        if report.generation_id != generation_id:
            raise CompatibilityProjectionError(
                f"projection for {report.generation_id} does not match "
                f"requested generation {generation_id}"
            )
        return report

    def _run_commit(
        self, con: sqlite3.Connection, generation_id: str,
        report: CompatibilityProjectionReport, hooks: ActivationHooks,
    ) -> None:
        authority = hooks.authority_writer or (
            lambda c: self._write_authority(c, generation_id)
        )
        authority(con)
        projection = hooks.projection_writer or self._write_projection
        projection(con, report)
        binder = hooks.version_binder or (
            lambda c, r: self._bind_versions(c, generation_id, r)
        )
        binder(con, report)

    def _write_authority(self, con: sqlite3.Connection, generation_id: str) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        # exactly one active row: demote every prior authority row first
        con.execute("UPDATE ce_generation_authority SET active=0 WHERE active=1")
        con.execute(
            "INSERT OR REPLACE INTO ce_generation_authority "
            "(generation_id, active, updated_at) VALUES (?,1,?)",
            (generation_id, now),
        )

    def _write_projection(
        self, con: sqlite3.Connection, report: CompatibilityProjectionReport
    ) -> None:
        clear_compatibility_projection(con)
        write_compatibility_projection(con, report)

    def _bind_versions(
        self, con: sqlite3.Connection, generation_id: str,
        report: CompatibilityProjectionReport,
    ) -> None:
        digest = report.fingerprint.digest
        self._repo.write_bindings(con, generation_id, {
            "projection_version": f"v2#{generation_id}",
            "projection_watermark": digest,
            "projection_fingerprint": digest,
        })


__all__ = [
    "ActivationHooks",
    "GenerationActivationError",
    "GenerationLifecycle",
]
