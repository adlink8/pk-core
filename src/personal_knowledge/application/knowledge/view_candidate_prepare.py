"""Phase 62-06: view/policy/evidence-bound candidate prepare ledger.

Phase 62 CONTEXT D-24/D-26/D-30/D-31: candidate preparation is keyed by the
active event generation, view builder version, extraction policy digest,
semantic prompt/schema version and evidence-event digest. This module owns
ONLY the prepare/ledger seam:

  - :class:`CandidateRunKey` — the versioned identity of one prepare run
  - :class:`ViewEstimate` / :class:`CandidateRun` — deterministic estimate
    records (calls/tokens/cost per view type and family)
  - :class:`CandidateRunRepository` — an additive ``ce_candidate_*`` ledger
    that writes estimates/status only and never calls a provider
  - legacy message-level runs are classified ``superseded_policy`` /
    non-executable through an append-only audit transition; their 24,487
    ledger rows/caches are never deleted (D-30)

Hard rules:
  - Preparation and estimation are fully deterministic and zero-paid; the
    production provider is never invoked (D-31).
  - A run approaches extraction only when every version component matches and
    its evidence resolves in the active event repository.
  - Actual execution is blocked pending a separate user cost-approval /
    representative-pilot checkpoint (D-31).
  - No conversation body is ever written to the ledger; estimates store
    aggregate counts and evidence event handles only (D-27).

No I/O outside the caller-provided DB path; no network, no provider calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from personal_knowledge.application.conversation.extraction_policy import (
    DEFAULT_POLICY,
    SchedulingOutput,
)
from personal_knowledge.application.conversation.extraction_views import (
    ViewBuildResult,
    ViewType,
)
from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.application.knowledge.candidate_manifest import (
    MANIFEST_DDL,
    MANIFEST_TABLES,
    ManifestIntegrityError,
    ManifestMissingError,
    ManifestSchemaError,
    build_manifest,
    list_candidate_rows,
    manifest_payload,
    save_manifest,
)

LEGACY_MESSAGE_RUN_PREFIX = "ir_"
VIEW_POLICY_RUN_PREFIX = "vc_"

# Deterministic default model estimate used until a real paid pilot is
# approved (D-31). Never wired to a provider.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FAMILY = "unknown"

_MODEL_ESTIMATES: dict[str, dict] = {
    "gemini-3.5-flash-lite": {
        "input_tokens_per_call": 4096,
        "output_tokens_per_call": 1024,
        "usd_per_million_input_tokens": 0.30,
        "usd_per_million_output_tokens": 1.50,
    },
}

# Append-only transition marker for old message-level prepare runs (D-30).
_LEGACY_SUPERSEDED_TRANSITION = "classified_superseded"

CANDIDATE_TABLES: tuple[str, ...] = (
    "ce_candidate_runs",
    "ce_candidate_estimates",
    "ce_candidate_audit",
    *MANIFEST_TABLES,
)

_CANDIDATE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ce_candidate_runs (
        run_id                  TEXT PRIMARY KEY,
        kind                    TEXT NOT NULL,
        status                  TEXT NOT NULL,
        active_generation_id    TEXT NOT NULL,
        view_builder_version    TEXT NOT NULL,
        policy_digest           TEXT NOT NULL,
        semantic_prompt_version TEXT NOT NULL,
        semantic_schema_version TEXT NOT NULL,
        evidence_event_digest   TEXT NOT NULL,
        evidence_refs_json      TEXT NOT NULL,
        family                  TEXT NOT NULL,
        view_count              INTEGER NOT NULL,
        candidate_count         INTEGER NOT NULL,
        estimated_calls         INTEGER NOT NULL,
        estimated_tokens        INTEGER NOT NULL,
        estimated_cost_usd      REAL NOT NULL,
        created_at              TEXT NOT NULL,
        UNIQUE (active_generation_id, view_builder_version, policy_digest,
                semantic_prompt_version, semantic_schema_version,
                evidence_event_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_candidate_estimates (
        run_id              TEXT NOT NULL,
        view_type           TEXT NOT NULL,
        family              TEXT NOT NULL,
        candidate_count     INTEGER NOT NULL,
        estimated_calls     INTEGER NOT NULL,
        estimated_tokens    INTEGER NOT NULL,
        estimated_cost_usd  REAL NOT NULL,
        PRIMARY KEY (run_id, view_type, family),
        FOREIGN KEY (run_id) REFERENCES ce_candidate_runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ce_candidate_audit (
        audit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id         TEXT NOT NULL,
        kind           TEXT NOT NULL,
        transition     TEXT NOT NULL,
        status         TEXT NOT NULL,
        non_executable INTEGER NOT NULL DEFAULT 0,
        reason         TEXT NOT NULL,
        recorded_at    TEXT NOT NULL
    )
    """,
)


class CandidatePrepareError(RuntimeError):
    """A candidate prepare/ledger operation violated the prepare contract."""


class LegacyRunSupersededError(CandidatePrepareError):
    """The old message-level prepare queue semantics are superseded (D-30)."""


class VersionMismatchError(CandidatePrepareError):
    """A run no longer matches the current generation/view/policy/gate contract."""


class UnresolvedEvidenceError(CandidatePrepareError):
    """Candidate evidence references events that are not in the repository."""


class ViewExtractionBlockedError(CandidatePrepareError):
    """Paid extraction is blocked pending a separate approval/pilot checkpoint."""


@dataclass(frozen=True)
class CandidateRunKey:
    """The versioned identity of one view-policy prepare run.

    Changing any component produces a different run id and a different queue
    (D-22/D-24). Evidence digest binds the exact evidence-event set.
    """

    active_generation_id: str
    view_builder_version: str
    policy_digest: str
    semantic_prompt_version: str
    semantic_schema_version: str
    evidence_event_digest: str

    def components(self) -> tuple[str, ...]:
        return (
            self.active_generation_id,
            self.view_builder_version,
            self.policy_digest,
            self.semantic_prompt_version,
            self.semantic_schema_version,
            self.evidence_event_digest,
        )


@dataclass(frozen=True)
class ViewEstimate:
    """Deterministic estimate for one view type / family (no body logging)."""

    view_type: str
    family: str
    candidate_count: int
    estimated_calls: int
    estimated_tokens: int
    estimated_cost_usd: float


@dataclass(frozen=True)
class CandidateRun:
    """One prepared view-policy run (estimates/ledger only, never paid)."""

    run_id: str
    kind: str
    status: str
    key: CandidateRunKey
    family: str
    view_count: int
    candidate_count: int
    estimated_calls: int
    estimated_tokens: int
    estimated_cost_usd: float
    created_at: str


def is_legacy_run_id(run_id: str) -> bool:
    """True for the old message-level prepare run namespace (``ir_*``)."""
    return run_id.startswith(LEGACY_MESSAGE_RUN_PREFIX)


def is_view_run_id(run_id: str) -> bool:
    """True for the view-policy candidate run namespace (``vc_*``)."""
    return run_id.startswith(VIEW_POLICY_RUN_PREFIX)


def make_candidate_run_id(key: CandidateRunKey) -> str:
    payload = "|".join(key.components())
    return VIEW_POLICY_RUN_PREFIX + hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def evidence_set_digest(event_refs: Iterable[str]) -> str:
    ordered = tuple(sorted(set(event_refs)))
    return hashlib.sha256(
        "|".join(ordered).encode("utf-8")
    ).hexdigest()


def _model_estimate(model: str) -> dict:
    try:
        return _MODEL_ESTIMATES[model]
    except KeyError as exc:
        raise CandidatePrepareError(
            f"unknown deterministic model estimate: {model!r}"
        ) from exc


def estimate_views(
    scheduled: SchedulingOutput,
    family: str = DEFAULT_FAMILY,
    model: str = DEFAULT_MODEL,
) -> tuple[ViewEstimate, ...]:
    """Deterministic per-view-type/family estimate (calls/tokens/cost).

    Pure local arithmetic — no provider, no body logging.
    """
    cfg = _model_estimate(model)
    input_tokens = int(cfg["input_tokens_per_call"])
    output_tokens = int(cfg["output_tokens_per_call"])
    tokens_per_call = input_tokens + output_tokens
    cost_per_call = (
        input_tokens * float(cfg["usd_per_million_input_tokens"])
        + output_tokens * float(cfg["usd_per_million_output_tokens"])
    ) / 1_000_000.0

    counts: dict[str, int] = {}
    for candidate in scheduled.candidates:
        view_type = (
            candidate.view_type.value
            if isinstance(candidate.view_type, ViewType)
            else str(candidate.view_type)
        )
        counts[view_type] = counts.get(view_type, 0) + 1

    estimates: list[ViewEstimate] = []
    for view_type in sorted(counts):
        count = counts[view_type]
        calls = count
        tokens = count * tokens_per_call
        cost = count * cost_per_call
        estimates.append(
            ViewEstimate(
                view_type=view_type,
                family=family,
                candidate_count=count,
                estimated_calls=calls,
                estimated_tokens=tokens,
                estimated_cost_usd=cost,
            )
        )
    return tuple(estimates)


class CandidateRunRepository:
    """Additive ``ce_candidate_*`` ledger for view-policy prepare runs."""

    def __init__(self, db: Path) -> None:
        self.db = Path(db)

    # ------------------------------------------------------------ schema

    def create_schema(self) -> None:
        con = connect_rw(self.db)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            for statement in _CANDIDATE_DDL:
                con.execute(statement)
            for statement in MANIFEST_DDL:
                con.execute(statement)
            con.commit()
        finally:
            con.close()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            con = connect_rw(f"file:{self.db.as_posix()}?mode=ro", uri=True)
        else:
            con = connect_rw(self.db)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    # ------------------------------------------------ legacy supersession

    def classify_legacy_run(
        self,
        run_id: str,
        *,
        reason: str = "message-level prepare superseded by view-policy contract",
    ) -> None:
        """Record an append-only supersession transition for an old run.

        Never deletes the old run's ledger rows or caches (D-30); it only
        appends an audit transition that makes the run non-executable.
        """
        if not run_id:
            raise CandidatePrepareError("legacy run id must not be empty")
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO ce_candidate_audit "
                "(run_id, kind, transition, status, non_executable, reason, recorded_at) "
                "VALUES (?, 'legacy_message', ?, 'superseded_policy', 1, ?, ?)",
                (
                    run_id,
                    _LEGACY_SUPERSEDED_TRANSITION,
                    reason,
                    _now(),
                ),
            )
            con.commit()
        finally:
            con.close()

    def legacy_status(self, run_id: str) -> str | None:
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT status FROM ce_candidate_audit "
                "WHERE run_id=? AND kind='legacy_message' "
                "ORDER BY recorded_at, audit_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    # ------------------------------------------------------- prepare

    def prepare_view_run(
        self,
        run_key: CandidateRunKey,
        scheduled: SchedulingOutput,
        view_result: ViewBuildResult,
        *,
        event_repo: object | None = None,
        family: str | None = None,
    ) -> CandidateRun:
        """Write the estimates/ledger for one view-policy run (never paid)."""
        _assert_key_matches(run_key, scheduled, view_result)
        try:
            manifest = build_manifest(scheduled, view_result)
        except ValueError as exc:
            raise VersionMismatchError(str(exc)) from exc

        refs = tuple(
            sorted(
                {eid for c in scheduled.candidates for eid in c.evidence_event_refs}
            )
        )
        if event_repo is not None:
            self._assert_evidence_resolved(
                run_key.active_generation_id, refs, event_repo
            )

        resolved_family = family or _families_of(event_repo, run_key.active_generation_id)
        estimates = estimate_views(scheduled, resolved_family)
        total_calls = sum(e.estimated_calls for e in estimates)
        total_tokens = sum(e.estimated_tokens for e in estimates)
        total_cost = sum(e.estimated_cost_usd for e in estimates)
        run_id = make_candidate_run_id(run_key)
        now = _now()

        existing = _write_candidate_ledger(
            self, run_id, run_key, refs, resolved_family,
            len(view_result.views), len(scheduled.candidates),
            estimates, total_calls, total_tokens, total_cost, now, manifest,
        )
        if existing is not None:
            return existing

        return CandidateRun(
            run_id=run_id,
            kind="view_policy",
            status="blocked_pending_user_cost_approval",
            key=run_key,
            family=resolved_family,
            view_count=len(view_result.views),
            candidate_count=len(scheduled.candidates),
            estimated_calls=total_calls,
            estimated_tokens=total_tokens,
            estimated_cost_usd=total_cost,
            created_at=now,
        )

    def _assert_evidence_resolved(
        self, generation_id: str, refs: tuple[str, ...], event_repo: object
    ) -> None:
        resolver = getattr(event_repo, "has_events", None)
        if resolver is None:
            raise CandidatePrepareError(
                "event_repo must expose has_events(generation_id, event_ids)"
            )
        existing = resolver(generation_id, list(refs))
        missing = [ref for ref in refs if ref not in existing]
        if missing:
            raise UnresolvedEvidenceError(
                f"{len(missing)} candidate evidence event(s) unresolved in "
                f"generation {generation_id}"
            )

    # ------------------------------------------------------- reads

    def has_ledger(self) -> bool:
        """Probe existing schema without initializing or creating a database."""
        if not self.db.exists():
            return False
        con = self._connect(readonly=True)
        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('ce_candidate_runs', 'ce_candidate_estimates', 'ce_candidate_audit')"
            )}
            if tables and len(tables) != 3:
                raise CandidatePrepareError("candidate ledger schema is incomplete")
            return len(tables) == 3
        finally:
            con.close()

    def get_run(self, run_id: str) -> CandidateRun | None:
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT * FROM ce_candidate_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            return _hydrate_run(row)
        finally:
            con.close()

    def status(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise CandidatePrepareError(f"candidate run not found: {run_id}")
        return {
            "run_id": run.run_id,
            "kind": run.kind,
            "status": run.status,
            "active_generation_id": run.key.active_generation_id,
            "view_builder_version": run.key.view_builder_version,
            "policy_digest": run.key.policy_digest,
            "semantic_prompt_version": run.key.semantic_prompt_version,
            "semantic_schema_version": run.key.semantic_schema_version,
            "evidence_event_digest": run.key.evidence_event_digest,
            "family": run.family,
            "view_count": run.view_count,
            "candidate_count": run.candidate_count,
            "estimated_calls": run.estimated_calls,
            "estimated_tokens": run.estimated_tokens,
            "estimated_cost_usd": run.estimated_cost_usd,
            "non_executable": True,
        }

    def estimates(self, run_id: str) -> list[ViewEstimate]:
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "SELECT view_type, family, candidate_count, estimated_calls, "
                "estimated_tokens, estimated_cost_usd FROM ce_candidate_estimates "
                "WHERE run_id=? ORDER BY view_type, family",
                (run_id,),
            ).fetchall()
            return [
                ViewEstimate(
                    view_type=r["view_type"],
                    family=r["family"],
                    candidate_count=r["candidate_count"],
                    estimated_calls=r["estimated_calls"],
                    estimated_tokens=r["estimated_tokens"],
                    estimated_cost_usd=r["estimated_cost_usd"],
                )
                for r in rows
            ]
        finally:
            con.close()

    # ------------------------------------------- executable guards

    def check_run_executable(
        self,
        run_id: str,
        *,
        current_key: CandidateRunKey | None = None,
        event_repo: object | None = None,
    ) -> None:
        """Validate a run may approach extraction (identity + evidence).

        Raises :class:`LegacyRunSupersededError` for legacy message-level runs,
        :class:`VersionMismatchError` on any version drift, and
        :class:`UnresolvedEvidenceError` when evidence is missing.
        """
        if is_legacy_run_id(run_id):
            raise LegacyRunSupersededError(
                f"legacy message-level prepare run {run_id} is superseded and "
                "non-executable under the view-policy contract (D-30)"
            )
        run = self.get_run(run_id)
        if run is None:
            raise CandidatePrepareError(f"candidate run not found: {run_id}")
        if current_key is not None:
            _assert_key_current(run.key, current_key)
        if event_repo is not None:
            refs = tuple(self._evidence_refs(run_id))
            self._assert_evidence_resolved(
                run.key.active_generation_id, refs, event_repo
            )

    def assert_extraction_authorized(self, run_id: str) -> None:
        """No command path may spend provider quota in Phase 62 (D-31).

        Legacy runs are superseded; view-policy runs are blocked until a
        separate user cost-approval / representative-pilot checkpoint.
        """
        if is_legacy_run_id(run_id):
            raise LegacyRunSupersededError(
                f"legacy message-level run {run_id} is superseded and "
                "non-executable; paid extraction requires a separate "
                "approval/pilot requirement (D-30)"
            )
        if is_view_run_id(run_id):
            run = self.get_run(run_id)
            if run is None:
                raise CandidatePrepareError(
                    f"candidate run not found: {run_id}"
                )
            raise ViewExtractionBlockedError(
                f"view-policy run {run_id} cannot execute: no paid extraction "
                "is authorized in Phase 62. A separate user cost approval and "
                "representative LLM pilot are required before any extract."
            )
        raise CandidatePrepareError(
            f"run {run_id} is not a recognized incremental or view-policy run"
        )

    # ------------------------------------------------- test/debug helpers

    def _evidence_refs(self, run_id: str) -> list[str]:
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "SELECT evidence_refs_json FROM ce_candidate_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not row:
                raise CandidatePrepareError(f"candidate run not found: {run_id}")
            return list(json.loads(row[0]))
        finally:
            con.close()

    def list_candidates(self, run_id: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
        """Return a bounded, body-free candidate mapping for a prepared run."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise CandidatePrepareError("limit must be between 1 and 100")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise CandidatePrepareError("offset must be >= 0")
        run = self.get_run(run_id)
        if run is None:
            raise CandidatePrepareError(f"candidate run not found: {run_id}")
        con = self._connect(readonly=True)
        try:
            try:
                rows = list_candidate_rows(con, run_id, limit=limit, offset=offset)
            except (ManifestMissingError, ManifestSchemaError) as exc:
                raise CandidatePrepareError(str(exc)) from exc
            return rows
        finally:
            con.close()

    def _run_rows(self) -> list[sqlite3.Row]:
        con = self._connect(readonly=True)
        try:
            return con.execute(
                "SELECT * FROM ce_candidate_runs ORDER BY run_id"
            ).fetchall()
        finally:
            con.close()

    def _audit_rows(self, run_id: str) -> list[sqlite3.Row]:
        con = self._connect(readonly=True)
        try:
            return con.execute(
                "SELECT * FROM ce_candidate_audit WHERE run_id=? ORDER BY audit_id",
                (run_id,),
            ).fetchall()
        finally:
            con.close()


# ------------------------------------------------------------- helpers

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_candidate_ledger(
    repo: CandidateRunRepository,
    run_id: str,
    run_key: CandidateRunKey,
    refs: tuple[str, ...],
    family: str,
    view_count: int,
    candidate_count: int,
    estimates: tuple[ViewEstimate, ...],
    total_calls: int,
    total_tokens: int,
    total_cost: float,
    now: str,
    manifest: dict,
) -> CandidateRun | None:
    """Atomically persist one view-policy run row, its estimates and audit."""
    con = repo._connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        existing_row = con.execute(
            "SELECT * FROM ce_candidate_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing_row is not None:
            try:
                stored = manifest_payload(con, run_id)
            except (ManifestIntegrityError, ManifestSchemaError) as exc:
                con.rollback()
                raise CandidatePrepareError(str(exc)) from exc
            if stored is None:
                con.rollback()
                raise CandidatePrepareError(
                    f"candidate run {run_id} has no candidate manifest; refusing overwrite"
                )
            if stored != manifest:
                con.rollback()
                raise VersionMismatchError(
                    f"candidate manifest mismatch for existing run {run_id}"
                )
            con.commit()
            return _hydrate_run(existing_row)
        con.execute(
            "INSERT INTO ce_candidate_runs "
            "(run_id, kind, status, active_generation_id, view_builder_version, "
            " policy_digest, semantic_prompt_version, semantic_schema_version, "
            " evidence_event_digest, evidence_refs_json, family, view_count, "
            " candidate_count, estimated_calls, estimated_tokens, "
            " estimated_cost_usd, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "view_policy",
                "blocked_pending_user_cost_approval",
                run_key.active_generation_id,
                run_key.view_builder_version,
                run_key.policy_digest,
                run_key.semantic_prompt_version,
                run_key.semantic_schema_version,
                run_key.evidence_event_digest,
                json.dumps(list(refs), sort_keys=True),
                family,
                view_count,
                candidate_count,
                total_calls,
                total_tokens,
                total_cost,
                now,
            ),
        )
        for estimate in estimates:
            con.execute(
                "INSERT INTO ce_candidate_estimates "
                "(run_id, view_type, family, candidate_count, estimated_calls, "
                " estimated_tokens, estimated_cost_usd) VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    estimate.view_type,
                    estimate.family,
                    estimate.candidate_count,
                    estimate.estimated_calls,
                    estimate.estimated_tokens,
                    estimate.estimated_cost_usd,
                ),
                )
        save_manifest(con, run_id, manifest, now)
        _record_view_audit(con, run_id, now)
        con.commit()
        return None
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise CandidatePrepareError(
            f"prepare_view_run failed (candidate ledger constraint): {exc}"
        ) from exc
    finally:
        con.close()


def _record_view_audit(con: sqlite3.Connection, run_id: str, now: str) -> None:
    con.execute(
        "INSERT INTO ce_candidate_audit "
        "(run_id, kind, transition, status, non_executable, reason, recorded_at) "
        "VALUES (?, 'view_policy', 'created', "
        "'blocked_pending_user_cost_approval', 1, "
        "'no paid extraction authorized until separate approval/pilot', ?)",
        (run_id, now),
    )


def _assert_key_matches(
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
    view_result: ViewBuildResult,
) -> None:
    if view_result.generation_id != run_key.active_generation_id:
        raise VersionMismatchError(
            f"generation mismatch: run key is {run_key.active_generation_id}, "
            f"view result is {view_result.generation_id}"
        )
    if view_result.builder_version != run_key.view_builder_version:
        raise VersionMismatchError(
            f"view builder version mismatch: run key is "
            f"{run_key.view_builder_version}, result is "
            f"{view_result.builder_version}"
        )
    if scheduled.policy_digest != run_key.policy_digest:
        raise VersionMismatchError(
            "policy digest mismatch between run key and scheduled queue"
        )
    refs = {eid for c in scheduled.candidates for eid in c.evidence_event_refs}
    actual_digest = evidence_set_digest(refs)
    if actual_digest != run_key.evidence_event_digest:
        raise VersionMismatchError(
            "evidence event digest mismatch between run key and scheduled queue"
        )


def _assert_key_current(stored: CandidateRunKey, current: CandidateRunKey) -> None:
    checks = (
        ("generation", "active_generation_id"),
        ("view builder version", "view_builder_version"),
        ("policy digest", "policy_digest"),
        ("semantic prompt version", "semantic_prompt_version"),
        ("semantic schema version", "semantic_schema_version"),
        ("evidence event digest", "evidence_event_digest"),
    )
    for label, attr in checks:
        stored_value = getattr(stored, attr)
        current_value = getattr(current, attr)
        if stored_value != current_value:
            raise VersionMismatchError(
                f"{label} mismatch: run is {stored_value!r}, current is "
                f"{current_value!r}"
            )


def _families_of(event_repo: object | None, generation_id: str) -> str:
    if event_repo is None:
        return DEFAULT_FAMILY
    resolver = getattr(event_repo, "adapter_families", None)
    if resolver is None:
        return DEFAULT_FAMILY
    families = tuple(resolver(generation_id))
    if not families:
        return DEFAULT_FAMILY
    return families[0]


def _hydrate_run(row: sqlite3.Row) -> CandidateRun:
    key = CandidateRunKey(
        active_generation_id=row["active_generation_id"],
        view_builder_version=row["view_builder_version"],
        policy_digest=row["policy_digest"],
        semantic_prompt_version=row["semantic_prompt_version"],
        semantic_schema_version=row["semantic_schema_version"],
        evidence_event_digest=row["evidence_event_digest"],
    )
    return CandidateRun(
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        key=key,
        family=row["family"],
        view_count=row["view_count"],
        candidate_count=row["candidate_count"],
        estimated_calls=row["estimated_calls"],
        estimated_tokens=row["estimated_tokens"],
        estimated_cost_usd=row["estimated_cost_usd"],
        created_at=row["created_at"],
    )


__all__ = [
    "CANDIDATE_TABLES",
    "CandidatePrepareError",
    "CandidateRun",
    "CandidateRunKey",
    "CandidateRunRepository",
    "DEFAULT_FAMILY",
    "DEFAULT_MODEL",
    "LEGACY_MESSAGE_RUN_PREFIX",
    "LegacyRunSupersededError",
    "UnresolvedEvidenceError",
    "VIEW_POLICY_RUN_PREFIX",
    "VersionMismatchError",
    "ViewEstimate",
    "ViewExtractionBlockedError",
    "estimate_views",
    "evidence_set_digest",
    "inspect_candidate_state",
    "is_legacy_run_id",
    "is_view_run_id",
    "load_event_graph",
    "make_candidate_run_id",
    "prepare_view_candidates",
    "view_run_status",
]


# ------------------------------------------------------------ orchestration
# Zero-paid event/view-aware inspect + prepare commands for `pk-ku` (Task 3).
# These never call a provider; the semantic gate runs with the deterministic
# ReplayJudge so every output is fully replayable (D-31).

class _LedgerEvidenceResolver:
    """Read-only evidence resolver backed by EventRepository (no writes)."""

    def __init__(self, db: Path) -> None:
        from personal_knowledge.application.conversation.event_repository import (
            EventRepository,
        )

        self._db = Path(db)
        self._repo = EventRepository(self._db)

    def has_events(self, generation_id: str, event_ids) -> set[str]:
        existing = {r["event_id"] for r in self._repo.iter_events(generation_id)}
        return {e for e in event_ids if e in existing}

    def adapter_families(self, generation_id: str) -> list[str]:
        con = sqlite3.connect(
            f"file:{self._db.as_posix()}?mode=ro", uri=True
        )
        con.row_factory = sqlite3.Row
        try:
            return [
                str(r["family"])
                for r in con.execute(
                    "SELECT DISTINCT family FROM ce_adapter_runs "
                    "WHERE generation_id=? ORDER BY family",
                    (generation_id,),
                )
            ]
        finally:
            con.close()


def load_event_graph(db: Path, generation_id: str):
    """Reconstruct an :class:`EventGraph` from a staged event generation.

    Reads only the caller-provided DB in read-only mode. Session records are
    derived from the events when the repository stores no session rows, which
    keeps the builders' session fallback deterministic.
    """
    from personal_knowledge.application.conversation.event_repository import (
        EventRepository,
    )
    from personal_knowledge.application.conversation.extraction_views import (
        EventGraph,
    )
    from personal_knowledge.core.conversation_events import (
        EventKind,
        EventRelation,
        FidelityProfile,
        Provenance,
        RelationKind,
        TypedEvent,
    )

    repo = EventRepository(db)
    events: list[TypedEvent] = []
    for row in repo.iter_events(generation_id):
        events.append(
            TypedEvent(
                event_id=row["event_id"],
                session_id=row["session_id"],
                kind=EventKind(row["kind"]),
                provenance=Provenance(
                    artifact_id=row["artifact_id"],
                    artifact_hash="",
                    native_locator=row["native_locator"],
                    native_session_id=row["session_id"],
                    native_event_id=row["native_event_id"],
                    contract_version=row["contract_version"],
                ),
                fidelity=FidelityProfile.from_dict(
                    json.loads(row["fidelity_json"])
                ),
                occurred_at=row["occurred_at"],
                ordinal=row["ordinal"],
                native_payload_ref=row["native_payload_ref"],
                content=row["content"],
                summary=row["summary"],
            )
        )
    relations = [
        EventRelation(
            r["relation_id"],
            r["source_event_id"],
            r["target_event_id"],
            RelationKind(r["relation_kind"]),
        )
        for r in repo.iter_relations(generation_id)
    ]
    return EventGraph(
        generation_id=generation_id,
        events=tuple(events),
        relations=tuple(relations),
    )


def _active_generation_id(db: Path) -> str | None:
    from personal_knowledge.application.conversation.event_repository import (
        EventRepository,
    )

    return EventRepository(db).authority_generation_id()


def _replay_admission_summary(db: Path, graph, result) -> dict:
    """Run the deterministic admission gate over the scheduled candidates.

    Uses only ReplayJudge, so coverage/rejection counts are fully replayable.
    """
    from personal_knowledge.evaluation.conversation.semantic_admission import (
        ReplayJudge,
        SemanticGate,
        SemanticVerdict,
    )

    index = {e.event_id: e for e in graph.events}
    allowed_events = set(index)
    gate = SemanticGate(ReplayJudge({}))
    views = {v.view_id: v for v in result.views}
    counts = {verdict: 0 for verdict in SemanticVerdict}
    reason_counts: dict[str, int] = {}
    invoked = 0
    for candidate in _scheduled_candidates_sorted(result):
        view = views.get(candidate.derived_from_view)
        if view is None:
            continue
        decision = gate.evaluate(
            view,
            index,
            allowed_event_ids=allowed_events,
            allowed_generation_ids={result.generation_id},
        )
        counts[decision.verdict] = counts.get(decision.verdict, 0) + 1
        reason_counts[decision.reason_code] = (
            reason_counts.get(decision.reason_code, 0) + 1
        )
        invoked += 1
    return {
        "candidates_evaluated": invoked,
        "admitted": counts.get(SemanticVerdict.ADMIT, 0),
        "rejected": counts.get(SemanticVerdict.REJECT, 0),
        "abstained": counts.get(SemanticVerdict.ABSTAIN, 0),
        "reasons": dict(sorted(reason_counts.items())),
        "judge": "replay-v1",
        "paid_calls": 0,
    }


def _scheduled_candidates_sorted(result):
    from personal_knowledge.application.conversation.extraction_policy import (
        schedule_candidates,
    )

    return schedule_candidates(DEFAULT_POLICY, result, "2026-08-12T00:00:00Z").candidates


def prepare_view_candidates(
    conversation_db: Path,
    *,
    semantic_prompt_version: str = "semantic-v1",
    semantic_schema_version: str = "schema-v1",
    model: str = DEFAULT_MODEL,
) -> dict:
    """Zero-paid view/policy/evidence-bound prepare for the active generation.

    Builds views, schedules candidates, runs the deterministic admission replay
    and writes the estimates/ledger. Returns a structured status; never calls a
    provider.
    """
    from personal_knowledge.application.conversation.extraction_views import (
        build_all_views,
    )

    generation_id = _active_generation_id(conversation_db)
    if not generation_id:
        return {"ok": False, "error": "no active event generation", "write": False}
    graph = load_event_graph(conversation_db, generation_id)
    result = build_all_views(graph)
    scheduled = _schedule(generation_id, result)
    if not scheduled.candidates:
        return {
            "ok": False,
            "error": "no scheduled candidates for the active generation",
            "write": False,
        }
    refs = tuple(
        sorted(
            {eid for c in scheduled.candidates for eid in c.evidence_event_refs}
        )
    )
    key = CandidateRunKey(
        active_generation_id=generation_id,
        view_builder_version=result.builder_version,
        policy_digest=scheduled.policy_digest,
        semantic_prompt_version=semantic_prompt_version,
        semantic_schema_version=semantic_schema_version,
        evidence_event_digest=evidence_set_digest(refs),
    )
    repository = CandidateRunRepository(conversation_db)
    repository.create_schema()
    run = repository.prepare_view_run(
        key, scheduled, result, event_repo=_LedgerEvidenceResolver(conversation_db)
    )
    replay = _replay_admission_summary(conversation_db, graph, result)
    return {
        "ok": True,
        "write": True,
        "run_id": run.run_id,
        "status": run.status,
        "active_generation_id": generation_id,
        "view_builder_version": result.builder_version,
        "policy_digest": scheduled.policy_digest,
        "policy_view_count": len(result.views),
        "candidate_count": len(scheduled.candidates),
        "estimated_calls": run.estimated_calls,
        "estimated_tokens": run.estimated_tokens,
        "estimated_cost_usd": run.estimated_cost_usd,
        "semantic_gate": replay,
        "blocked_pending_user_cost_approval": True,
    }


def _schedule(generation_id: str, result):
    from personal_knowledge.application.conversation.extraction_policy import (
        schedule_candidates,
    )

    return schedule_candidates(DEFAULT_POLICY, result, "2026-08-12T00:00:00Z")


def inspect_candidate_state(conversation_db: Path) -> dict:
    """Read-only event/view-aware inspect (never writes, never pays)."""
    from personal_knowledge.application.conversation.extraction_views import (
        build_all_views,
    )

    generation_id = _active_generation_id(conversation_db)
    state: dict = {
        "active_generation_id": generation_id,
        "policy_view_count": 0,
        "deterministic_exclusions": {},
        "semantic_gate_replay": {"candidates_evaluated": 0, "paid_calls": 0},
        "pending_estimates": [],
        "blocked_pending_user_cost_approval": True,
        "legacy_superseded_audit_count": 0,
    }
    if not generation_id:
        return state
    graph = load_event_graph(conversation_db, generation_id)
    result = build_all_views(graph)
    state["policy_view_count"] = len(result.views)
    state["semantic_gate_replay"] = _replay_admission_summary(
        conversation_db, graph, result
    )
    state["deterministic_exclusions"] = _deterministic_exclusions(graph, result)
    repository = CandidateRunRepository(conversation_db)
    if not repository.has_ledger():
        return state
    state["pending_estimates"] = [
        repository.status(row["run_id"]) for row in repository._run_rows()
    ]
    state["legacy_superseded_audit_count"] = _legacy_audit_count(repository)
    return state


def _deterministic_exclusions(graph, result) -> dict:
    """Count evidence/lineage exclusions that never reach a judge."""
    from personal_knowledge.evaluation.conversation.semantic_admission import (
        ReplayJudge,
        SemanticGate,
    )

    index = {e.event_id: e for e in graph.events}
    allowed_events = set(index)
    gate = SemanticGate(ReplayJudge({}))
    counts: dict[str, int] = {}
    for view in result.views:
        decision = gate.evaluate(
            view,
            index,
            allowed_event_ids=allowed_events,
            allowed_generation_ids={graph.generation_id},
        )
        if decision.reason_code.startswith("reject:"):
            counts[decision.reason_code] = counts.get(decision.reason_code, 0) + 1
    return dict(sorted(counts.items()))


def _legacy_audit_count(repository: CandidateRunRepository) -> int:
    con = repository._connect(readonly=True)
    try:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM ce_candidate_audit "
            "WHERE kind='legacy_message'"
        ).fetchone()
        return row["n"] if row else 0
    finally:
        con.close()


def view_run_status(conversation_db: Path, run_id: str) -> dict:
    """Ledger status for one view-policy run (read-only, never pays)."""
    repository = CandidateRunRepository(conversation_db)
    has_ledger = repository.has_ledger()
    if is_legacy_run_id(run_id):
        status = repository.legacy_status(run_id) if has_ledger else None
        return {
            "run_id": run_id,
            "kind": "legacy_message",
            "status": status or "legacy_unclassified",
            "non_executable": True,
            "note": "message-level prepare queue semantics superseded (D-30); "
                    "audit history preserved",
        }
    run = repository.get_run(run_id) if has_ledger else None
    if run is None:
        return {
            "run_id": run_id,
            "kind": "unknown",
            "status": "not_found",
            "non_executable": True,
        }
    return repository.status(run_id)


def _assert_legacy_not_superseded(db: Path, run_id: str) -> bool:
    """True when a run id is a legacy run classified as superseded.

    Read-only probe: returns False when the candidate ledger table is absent
    so a CLI guard can never create candidate tables on a live authority DB.
    """
    if not is_legacy_run_id(run_id):
        return False
    try:
        db_path = Path(db)
        if not db_path.exists():
            return False
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT status FROM ce_candidate_audit "
                "WHERE run_id=? AND kind='legacy_message' "
                "ORDER BY recorded_at, audit_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return bool(row and row[0] == "superseded_policy")
        except sqlite3.OperationalError:
            return False
        finally:
            con.close()
    except (sqlite3.Error, OSError, TypeError):
        return False
