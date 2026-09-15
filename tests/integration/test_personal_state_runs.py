from __future__ import annotations

from dataclasses import replace
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import (
    SCHEMA_SQL,
)
from personal_knowledge.application.knowledge.lifecycle_events import (
    ensure_lifecycle_schema,
)
from personal_knowledge.intelligence.runs import (
    PersonalStateValidationError,
    plan_run,
    publish_run,
    validate_run,
)
from personal_knowledge.intelligence.schema import EvidenceReference, StateAssertion


class StubResolver:
    def __init__(
        self,
        *,
        status: str = "ok",
        eligible: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.eligible = eligible
        self.metadata = metadata or {"privacy_class": "R4", "fixture": "stable"}

    def resolve(self, ref: str, **_: Any) -> dict[str, Any]:
        return {
            "ref": ref,
            "artifact_type": "knowledge_unit",
            "status": self.status,
            "eligible": self.eligible,
            "metadata": self.metadata,
            "evidence_refs": [],
            "content": None,
        }


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "personal-state-runs.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    ensure_lifecycle_schema(con)
    con.execute(
        "INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)",
        ("a.personal_change", "A", "personal_change_analysis", "R4", "a-hash", "now"),
    )
    con.execute(
        "INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)",
        ("s.knowledge_unit", "S", "canonical_knowledge", "R4", "s-hash", "now"),
    )
    for version_id in ("av1", "av2"):
        con.execute(
            "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                "s.knowledge_unit",
                f"version-{version_id}",
                f"checksum-{version_id}",
                "sqlite_table",
                "canonical_knowledge_units",
                "validated",
                "R4",
                None,
                None,
                "{}",
                "now",
            ),
        )
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        ("ss1", "{}", "snapshot-hash-1", "validated", "gate", "now", "now"),
    )
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        ("ss2", "{}", "snapshot-hash-2", "validated", "gate", "now", "now"),
    )
    con.execute(
        "INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)",
        ("ss1", "canonical_knowledge", "av1"),
    )
    con.execute(
        "INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)",
        ("ss2", "canonical_knowledge", "av2"),
    )
    con.execute(
        "UPDATE serving_authority SET active_snapshot_id='ss1',activated_at='now' "
        "WHERE singleton_id=1"
    )
    con.commit()
    con.close()
    return db_path


def _assertion(
    *,
    ref: str = "ku1",
    artifact_version_id: str = "av1",
    predicate: str = "complete_target",
    value: Any = "D",
) -> StateAssertion:
    return StateAssertion(
        assertion_kind="goal",
        provenance_class="observation",
        subject="user",
        domain="work",
        scope="personal",
        predicate=predicate,
        value=value,
        valid_from="2026-07-17T00:00:00Z",
        observed_at="2026-07-17T00:00:00Z",
        confidence=0.9,
        uncertainty="explicit user goal",
        evidence=(
            EvidenceReference(
                ref=ref,
                artifact_type="knowledge_unit",
                serving_role="canonical_knowledge",
                artifact_version_id=artifact_version_id,
                privacy_class="R4",
            ),
        ),
    )


def _counts(db_path: Path) -> dict[str, int | str | None]:
    con = sqlite3.connect(db_path)
    try:
        return {
            "runs": con.execute("SELECT COUNT(*) FROM personal_state_runs").fetchone()[0],
            "assertions": con.execute(
                "SELECT COUNT(*) FROM personal_state_assertions"
            ).fetchone()[0],
            "evidence": con.execute(
                "SELECT COUNT(*) FROM personal_state_evidence"
            ).fetchone()[0],
            "snapshot_events": con.execute(
                "SELECT COUNT(*) FROM serving_snapshot_events"
            ).fetchone()[0],
            "lifecycle_events": con.execute(
                "SELECT COUNT(*) FROM knowledge_lifecycle_events"
            ).fetchone()[0],
            "active_snapshot": con.execute(
                "SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1"
            ).fetchone()[0],
        }
    finally:
        con.close()


def test_plan_is_read_only_and_publish_is_atomic_idempotent(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    resolver = StubResolver()
    before = _counts(db_path)

    first = plan_run(
        db_path,
        [_assertion()],
        producer_version="producer-v1",
        input_manifest={"source": "fixture-v1"},
        resolver=resolver,
    )
    replay = plan_run(
        db_path,
        [_assertion()],
        producer_version="producer-v1",
        input_manifest={"source": "fixture-v1"},
        resolver=resolver,
    )
    assert first == replay
    assert first.input_manifest["snapshot_members"]["canonical_knowledge"][
        "artifact_version_id"
    ] == "av1"
    assert publish_run(db_path, first, write=False, resolver=resolver)["written"] is False
    assert _counts(db_path) == before

    written = publish_run(db_path, first, write=True, resolver=resolver)
    existing = publish_run(db_path, replay, write=True, resolver=resolver)
    assert written["written"] is True and written["existing"] is False
    assert existing["written"] is False and existing["existing"] is True
    after = _counts(db_path)
    assert after["runs"] == after["assertions"] == after["evidence"] == 1
    assert after["active_snapshot"] == before["active_snapshot"] == "ss1"
    assert after["snapshot_events"] == before["snapshot_events"]
    assert after["lifecycle_events"] == before["lifecycle_events"]


def test_snapshot_or_producer_change_creates_distinct_identity(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    resolver = StubResolver()
    base = plan_run(
        db_path,
        [_assertion()],
        producer_version="producer-v1",
        input_manifest={"source": "fixture"},
        resolver=resolver,
    )
    other_producer = plan_run(
        db_path,
        [_assertion()],
        producer_version="producer-v2",
        input_manifest={"source": "fixture"},
        resolver=resolver,
    )
    other_snapshot = plan_run(
        db_path,
        [_assertion(artifact_version_id="av2")],
        producer_version="producer-v1",
        input_manifest={"source": "fixture"},
        snapshot_id="ss2",
        resolver=resolver,
    )
    assert len({base.run_id, other_producer.run_id, other_snapshot.run_id}) == 3
    assert len(
        {
            base.assertions[0].assertion_id,
            other_producer.assertions[0].assertion_id,
            other_snapshot.assertions[0].assertion_id,
        }
    ) == 3
    assert other_snapshot.snapshot.snapshot_id == "ss2"
    for run in (base, other_producer, other_snapshot):
        assert publish_run(db_path, run, write=True, resolver=resolver)["written"] is True
    counts = _counts(db_path)
    assert counts["runs"] == counts["assertions"] == counts["evidence"] == 3


def test_registry_snapshot_and_privacy_fail_closed(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    with pytest.raises(PersonalStateValidationError, match="evidence_version_mismatch"):
        plan_run(
            db_path,
            [_assertion(artifact_version_id="av2")],
            producer_version="producer-v1",
            input_manifest={"source": "mixed"},
            resolver=StubResolver(),
        )

    with pytest.raises(PersonalStateValidationError, match="evidence_ineligible"):
        plan_run(
            db_path,
            [_assertion()],
            producer_version="producer-v1",
            input_manifest={"source": "blocked"},
            resolver=StubResolver(metadata={"privacy": "blocked"}),
        )
    with pytest.raises(PersonalStateValidationError, match="secret_payload"):
        plan_run(
            db_path,
            [_assertion()],
            producer_version="producer-v1",
            input_manifest={"source": "secret"},
            resolver=StubResolver(metadata={"note": "password=do-not-store"}),
        )

    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE artifact_registry_entries SET authority_role='wrong' "
        "WHERE registry_id='a.personal_change'"
    )
    con.commit()
    con.close()
    with pytest.raises(PersonalStateValidationError, match="registry_authority_mismatch"):
        plan_run(
            db_path,
            [_assertion()],
            producer_version="producer-v1",
            input_manifest={"source": "registry-drift"},
            resolver=StubResolver(),
        )
    assert _counts(db_path)["runs"] == 0


def test_injected_failure_rolls_back_complete_run(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    resolver = StubResolver()
    run = plan_run(
        db_path,
        [
            _assertion(ref="ku1", predicate="goal", value="D"),
            _assertion(ref="ku2", predicate="constraint", value="privacy"),
        ],
        producer_version="producer-v1",
        input_manifest={"source": "two-assertions"},
        resolver=resolver,
    )
    before = _counts(db_path)
    with pytest.raises(RuntimeError, match="injected personal-state publication failure"):
        publish_run(
            db_path,
            run,
            write=True,
            resolver=resolver,
            inject_failure_after=1,
        )
    assert _counts(db_path) == before


def test_validate_run_rejects_tampered_assertion_content(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    resolver = StubResolver()
    run = plan_run(
        db_path,
        [_assertion()],
        producer_version="producer-v1",
        input_manifest={"source": "fixture"},
        resolver=resolver,
    )
    tampered_assertion = replace(run.assertions[0], predicate="silently_changed")
    tampered_run = replace(run, assertions=(tampered_assertion,))
    with pytest.raises(PersonalStateValidationError, match="assertion_id_mismatch"):
        validate_run(db_path, tampered_run, resolver=resolver)
