"""Read-only personal-model projection from governed candidate review state."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from personal_knowledge.application.conversation.candidate_review_contract import effective_review_events
from personal_knowledge.intelligence.schema import SnapshotBinding, checksum
from personal_knowledge.intelligence.state_projection import ProjectionError, normalize_candidates

class HarnessModelProjectionError(Exception):
    """Fail-closed projection derivation error with a stable machine code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


class HarnessModelProjectionProvider:
    """Plan 61-09: derive a versioned personal-model projection (HARNESS-07).

    The provider reads ONLY the bound review adapter/ledger (D-28) and every
    confirmed-accepted candidate is validated through
    ``state_projection.normalize_candidates`` -- the sole normalization/
    validation path (D-20/D-21/D-22) -- against its own immutable agentsview
    snapshot binding before a safe metadata-only envelope is derived. A
    candidate is projection input only when its effective review state is a
    confirmed accept/edit; drafts, ignored, undone, mixed-snapshot or private
    content is never projected. The envelope carries provenance_class:
    inference, version, scope, valid/observed time, confidence/uncertainty, the
    two typed freshness legs, support/conflict refs and counts, supersession,
    limitations and status -- without raw Evidence bodies, drafts, ignored
    Candidates or any canonical/promotion claim (T-61-PROJ-01). The provider
    never reads raw Evidence bodies, never references canonical/promotion/
    rollback/watermark/active-pointer/permission/value state and never writes
    anything (T-61-CANON-02).
    """

    # Serving-role -> typed evidence artifact mapping for the shared
    # normalization vocabulary (EVIDENCE_TYPES in intelligence/schema).
    _EVIDENCE_ARTIFACT_TYPE = {
        "source.agentsview": "turn",
        "agent.conversation.canonical": "canonical_message",
    }

    def __init__(self, *, review_adapter: Any, review_db: Path | str | None = None) -> None:
        self.review_adapter = review_adapter
        self.review_db = review_db

    # ------------------------------------------------------------------
    # Review ledger reads (read-only, connection-per-call)
    # ------------------------------------------------------------------

    def _review_feedback_rows(self) -> list[dict[str, Any]]:
        db = getattr(self.review_adapter, "db_path", None) or self.review_db
        if db is None:
            return []
        db_path = Path(db)
        if not db_path.exists():
            return []
        con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT rowid AS journal_sequence, feedback_id, candidate_id, action, version, "
                "referenced_feedback_id, disposition, recorded_at "
                "FROM candidate_review_feedback ORDER BY rowid"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    @staticmethod
    def _effective_review_state(candidate_id: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Replay the append-only review history to one fail-closed effective state.

        accept/edit confirms acceptance; ignore records an exclusion; undo
        reverts the referenced gesture so a confirmed accept/edit is no longer
        projection input. Unknown or malformed history fails closed (never
        accepted).
        """
        accepted = False
        ignored = False
        disposition: Any = None
        review: Mapping[str, Any] | None = None
        for row in effective_review_events(candidate_id, rows):
            action = str(row.get("action") or "")
            if action in ("accept", "edit"):
                accepted, ignored = True, False
                disposition = row.get("disposition")
                review = row
            elif action == "ignore":
                accepted, ignored = False, True
        return {
            "accepted": accepted,
            "ignored": ignored,
            "disposition": disposition,
            "review": review,
        }

    # ------------------------------------------------------------------
    # state_projection normalization/validation path
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_for(candidate: Mapping[str, Any]) -> SnapshotBinding:
        observation = candidate.get("observation")
        snapshot_ref = ""
        if isinstance(observation, Mapping):
            snapshot_ref = str(observation.get("snapshot") or "")
        if not snapshot_ref.startswith(("agentsview@", "conversation-generation@")):
            raise HarnessModelProjectionError(
                "snapshot_invalid", "accepted candidate must bind a supported source snapshot"
            )
        snapshot_hash = snapshot_ref.split("@", 1)[1]
        members: dict[str, dict[str, Any]] = {}
        for ev in candidate.get("evidence") or []:
            if not isinstance(ev, Mapping):
                continue
            role = str(ev.get("serving_role") or "")
            if not role:
                continue
            members[role] = {
                "artifact_version_id": str(ev.get("artifact_version_id") or ""),
                "privacy_class": str(ev.get("privacy_class") or "R2"),
            }
        return SnapshotBinding(
            snapshot_id=snapshot_ref,
            snapshot_hash=snapshot_hash,
            members=members,
        )

    def _normalize_input(self, candidate: Mapping[str, Any], snapshot: SnapshotBinding) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        for ev in candidate.get("evidence") or []:
            if not isinstance(ev, Mapping):
                continue
            row = dict(ev)
            row["snapshot_id"] = snapshot.snapshot_id
            row["snapshot_hash"] = snapshot.snapshot_hash
            role = str(ev.get("serving_role") or "")
            row["artifact_type"] = self._EVIDENCE_ARTIFACT_TYPE.get(role, "turn")
            evidence.append(row)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "assertion_kind": "goal",
            "provenance_class": "inference",
            "derivation": "synthesis",
            "subject": "user",
            "domain": "work",
            "scope": str(candidate.get("scope") or ""),
            "predicate": "derived_context",
            "value": {
                "conclusion_ref": str(candidate.get("candidate_id") or ""),
                "event_ref": str(candidate.get("event_id") or ""),
                "reflection_key": str(candidate.get("reflection_key") or ""),
            },
            "valid_from": str(candidate.get("valid_from") or ""),
            "valid_to": candidate.get("valid_to") or None,
            "observed_at": str(candidate.get("observed_at") or ""),
            "confidence": float(candidate.get("confidence") or 0.5),
            "uncertainty_reason": str(candidate.get("uncertainty") or "accepted review derivation"),
            "lifecycle": "current",
            "evidence": evidence,
        }

    def _validate_accepted(self, accepted: list[tuple[Mapping[str, Any], dict[str, Any]]]) -> None:
        """The only normalization/validation path for accepted review material.

        ``normalize_candidates`` enforces the 61-09 invariants: inference derives
        only from synthesis, draft/ignored/pending lifecycle values reject,
        mixed-snapshot evidence rejects and private/secret payloads reject.
        """
        for candidate, _state in accepted:
            snapshot = self._snapshot_for(candidate)
            normalize_candidates(
                [self._normalize_input(candidate, snapshot)],
                snapshot=snapshot,
            )

    @staticmethod
    def _is_temporally_eligible(candidate: Mapping[str, Any], *, now: datetime) -> bool:
        """Return whether a normalized candidate is visible at one UTC instant."""
        valid_from = datetime.fromisoformat(str(candidate["valid_from"]).replace("Z", "+00:00"))
        observed_at = datetime.fromisoformat(str(candidate["observed_at"]).replace("Z", "+00:00"))
        valid_to_value = candidate.get("valid_to")
        valid_to = (
            datetime.fromisoformat(str(valid_to_value).replace("Z", "+00:00"))
            if valid_to_value
            else None
        )
        return (
            observed_at <= now
            and valid_from <= now
            and (valid_to is None or now < valid_to)
        )

    # ------------------------------------------------------------------
    # Safe projection envelope
    # ------------------------------------------------------------------

    def _empty_projection(self, scope: str, limitation: str) -> dict[str, Any]:
        return {
            "projection_id": "projection_" + checksum({"scope": scope, "empty": True})[:24],
            "version": 0,
            "provenance_class": "inference",
            "scope": scope,
            "valid_from": None,
            "valid_to": None,
            "observed_at": None,
            "confidence": None,
            "uncertainty": ["unknown_no_evidence"],
            "freshness": {},
            "support_refs": [],
            "support_count": 0,
            "conflict_refs": [],
            "conflict_count": 0,
            "conflicts": [],
            "supersession": None,
            "limitations": [limitation],
            "status": "unknown",
        }

    @staticmethod
    def _projection_status(
        *,
        accepted: list[tuple[Mapping[str, Any], dict[str, Any]]],
        reviewed_not_accepted: bool,
        superseded_ids: list[str],
        primary: Mapping[str, Any],
    ) -> str:
        if not accepted:
            return "unknown"
        if reviewed_not_accepted:
            return "uncertain"
        if (primary.get("conflict_refs")) and not superseded_ids:
            return "conflict"
        return "uncertain" if float(primary.get("confidence") or 0.5) < 0.6 else "current"

    def get(
        self,
        *,
        scope: str,
        binding: Any = None,
        task_id: Any = None,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        """Derive one safe versioned projection for the approved scope.

        ``task_id``/``idempotency_key``/``binding`` are accepted identifiers of
        the caller only; the provider derives exclusively from the confirmed
        accepted review state bound to the adapter/ledger.
        """
        if not isinstance(scope, str) or not scope.strip():
            raise HarnessModelProjectionError("scope_required")
        if isinstance(binding, Mapping) and binding.get("scope") is not None and str(binding.get("scope")) != scope:
            raise HarnessModelProjectionError("binding_scope_mismatch")

        adapter = self.review_adapter
        resolver = getattr(adapter, "resolved_candidates", None)
        candidates = resolver() if callable(resolver) else getattr(adapter, "candidates", None)
        if adapter is None or not isinstance(candidates, Mapping) or not candidates:
            return self._empty_projection(scope, "no bound accepted review state is available")

        rows = self._review_feedback_rows()
        scope_candidates = [
            candidate for candidate in candidates.values()
            if isinstance(candidate, Mapping) and str(candidate.get("scope") or "") in {scope, "global"}
        ]
        if not scope_candidates:
            return self._empty_projection(scope, "scope has no reviewable accepted content")

        reviewed = [
            (candidate, self._effective_review_state(str(candidate.get("candidate_id") or ""), rows))
            for candidate in scope_candidates
        ]
        accepted = [
            (candidate, state) for candidate, state in reviewed
            if state["accepted"] and str(state.get("disposition") or "") != "defer_judgment"
        ]
        if not accepted:
            return self._empty_projection(scope, "scope has no confirmed accepted review content")

        # Normalize all accepted candidates before overlay selection, then apply
        # one shared UTC read instant so an ineligible local override cannot hide
        # an otherwise eligible global/default candidate.
        try:
            self._validate_accepted(accepted)
        except ProjectionError as exc:
            raise HarnessModelProjectionError(exc.code, exc.detail) from exc
        now = datetime.now(timezone.utc)
        accepted = [
            item for item in accepted
            if self._is_temporally_eligible(item[0], now=now)
        ]
        if not accepted:
            return self._empty_projection(scope, "scope has no temporally eligible accepted review content")

        reviewed_not_accepted = bool([
            item for item in reviewed
            if item[1]["ignored"] or (item[1]["review"] is not None and not item[1]["accepted"])
        ])

        # Accepted content is resolved in review order; the latest accepted
        # inference replaces the previous one (replace_existing or a plain later
        # accept), while keep_existing/coexist_by_context leave the current
        # content in place and only defer_judgment stays unprojected.
        accepted_sorted = sorted(
            accepted,
            key=lambda item: (
                0 if str(item[0].get("scope")) == "global" else 1,
                int((item[1].get("review") or {}).get("journal_sequence") or 0),
                str((item[1].get("review") or {}).get("recorded_at") or ""),
                int((item[1].get("review") or {}).get("version") or 0),
                str(item[0].get("candidate_id") or ""),
            ),
        )
        selected: dict[str, Mapping[str, Any]] = {}
        superseded_ids: list[str] = []
        for candidate, state in accepted_sorted:
            disposition = str(state.get("disposition") or "")
            if disposition == "defer_judgment":
                continue
            subject = str(candidate.get("subject") or "derived_context").strip().casefold()
            previous = selected.get(subject)
            if previous is None:
                selected[subject] = candidate
                continue
            if disposition == "keep_existing" or disposition == "coexist_by_context":
                continue
            superseded_ids.append(str(previous["candidate_id"]))
            selected[subject] = candidate
        primary = next(reversed(selected.values()), None)
        if primary is None:
            return self._empty_projection(scope, "scope has no projected accepted content")

        state_by_id = {str(candidate.get("candidate_id") or ""): state for candidate, state in accepted}
        primary_state = state_by_id[str(primary["candidate_id"])]
        disposition = primary_state.get("disposition")
        confidence = float(primary.get("confidence") or 0.5)
        status = self._projection_status(
            accepted=accepted,
            reviewed_not_accepted=reviewed_not_accepted,
            superseded_ids=superseded_ids,
            primary=primary,
        )

        support_refs = list(dict.fromkeys(str(ref) for item in selected.values() for ref in (item.get("support_refs") or [])))
        conflict_refs = [str(ref) for ref in (primary.get("conflict_refs") or [])]
        conflicts = [
            {"ref": ref, "disposition": disposition} if disposition else {"ref": ref}
            for ref in conflict_refs
        ]
        uncertainty = [f"source:{primary.get('uncertainty') or 'accepted review derivation'}"]
        if confidence < 0.6:
            uncertainty.append("low_confidence")
        if status == "conflict":
            uncertainty.append("unresolved_conflict")
        limitations = ["derived projection; not a personal fact or stable label"]
        if reviewed_not_accepted:
            limitations.append("scope review state is not uniformly confirmed accepted; projection is not current")
        if superseded_ids:
            limitations.append("replaced accepted inference(s) recorded in supersession")
        if status == "conflict":
            limitations.append("accepted content references conflicting evidence")
        if confidence < 0.6:
            limitations.append("inference confidence is low; treat as uncertain derived context")

        scope_ids = {str(candidate.get("candidate_id") or "") for candidate in scope_candidates}
        version = max([1, *(int(row.get("journal_sequence") or 0) + 1 for row in rows if str(row.get("candidate_id") or "") in scope_ids)])
        projection_id = "projection_" + checksum({
            "scope": scope,
            "version": version,
            "candidate_ids": sorted(str(candidate.get("candidate_id") or "") for candidate, _state in accepted),
            "primary_candidate_id": str(primary["candidate_id"]),
            "accepted_checksum": primary.get("candidate_checksum"),
            "review_feedback_id": (primary_state.get("review") or {}).get("feedback_id"),
            "valid_from": primary.get("valid_from"),
            "observed_at": primary.get("observed_at"),
            "support_refs": support_refs,
            "conflict_refs": conflict_refs,
        })[:24]
        freshness = primary.get("freshness")
        if not isinstance(freshness, Mapping):
            freshness = {}
        supersession: Any = None
        if superseded_ids:
            supersession = {
                "replaced_candidate_ids": sorted(superseded_ids),
                "current_candidate_id": str(primary["candidate_id"]),
            }

        result = {
            "projection_id": projection_id,
            "version": version,
            "provenance_class": "inference",
            "scope": scope,
            "valid_from": primary.get("valid_from"),
            "valid_to": primary.get("valid_to"),
            "observed_at": primary.get("observed_at"),
            "confidence": confidence,
            "uncertainty": uncertainty,
            "freshness": dict(freshness),
            "support_refs": support_refs,
            "support_count": len(support_refs),
            "conflict_refs": conflict_refs,
            "conflict_count": len(conflict_refs),
            "conflicts": conflicts,
            "supersession": supersession,
            "limitations": limitations,
            "status": status,
        }
        assertions = []
        for item in selected.values():
            conclusion = item.get("conclusion")
            refs = list(item.get("support_refs") or [])
            if isinstance(conclusion, str) and conclusion.strip() and refs:
                assertions.append({
                    "candidate_id": str(item["candidate_id"]), "conclusion": conclusion,
                    "subject": str(item.get("subject") or ""), "scope": scope,
                    "source_scope": str(item.get("scope") or ""), "support_refs": refs,
                    "valid_from": item.get("valid_from"), "valid_to": item.get("valid_to"),
                    "provenance_class": "inference",
                    "freshness": dict(item.get("freshness") or {}),
                })
        if assertions:
            result["assertions"] = assertions
        return result
