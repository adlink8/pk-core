from __future__ import annotations

import sqlite3

import pytest
import yaml

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.intelligence.decision.schema import (
    CognitionReference,
    DecisionSchemaError,
)


def test_decision_authority_is_independent_and_non_serving() -> None:
    policy = yaml.safe_load(open("governance/policies/artifact_layers.yaml", encoding="utf-8"))
    entries = {item["id"]: item for item in policy["artifacts"]}
    entry = entries["a.decision_feedback"]
    assert entry["layer"] == "A"
    assert entry["privacy"] == "R4"
    assert entry["evidence_parent"] == "a.personal_change"
    assert entry["producer"] == "intelligence.decision"
    assert entry["lifecycle"] == "immutable"
    assert entry["version_source"] == "run_manifest_checksum"
    assert entry["authority_role"] not in policy["required_serving_roles"]


def test_schema_keeps_recommendations_out_of_fact_ku_and_serving_authority() -> None:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    tables = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "decision_runs",
        "decision_recommendations",
        "decision_support_refs",
        "decision_confirmations",
        "decision_actions",
        "decision_outcomes",
        "decision_effectiveness",
        "decision_events",
    } <= tables
    columns = {
        row[1]
        for row in con.execute("PRAGMA table_info(decision_recommendations)")
    }
    assert not ({"fact", "knowledge_unit", "approved", "executed"} & columns)
    triggers = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_decision_%'"
        )
    }
    for table in (
        "runs", "recommendations", "support_refs", "confirmations", "actions",
        "outcomes", "effectiveness", "events",
    ):
        assert f"trg_decision_{table}_immutable_update" in triggers
        assert f"trg_decision_{table}_immutable_delete" in triggers
    con.executescript(SCHEMA_SQL)
    con.close()


def test_recommendation_and_confirmation_cannot_be_reclassified_as_phase25_truth() -> None:
    base = dict(
        authority_id="a.decision_feedback",
        record_id="rec_fixture",
        source_run_id="psr_fixture",
        source_run_checksum="a" * 64,
        source_publication_sequence=1,
        snapshot_id="ss_fixture",
        snapshot_hash="snapshot-hash",
        provenance_class="inference",
        evidence_status="eligible",
        uncertainty="",
        record_checksum="b" * 64,
    )
    for cognitive_type in ("recommendation", "user_confirmation"):
        with pytest.raises(DecisionSchemaError, match="invalid_cognitive_reference"):
            CognitionReference(cognitive_type=cognitive_type, **base)


def test_decision_write_modules_have_no_external_executor_surface() -> None:
    from personal_knowledge.intelligence.decision import recommendations, state_machine

    source = recommendations.__loader__.get_source(recommendations.__name__) + state_machine.__loader__.get_source(state_machine.__name__)
    for forbidden in ("import requests", "import httpx", "import subprocess", "def dispatch(", "def execute("):
        assert forbidden not in source


def test_effectiveness_is_always_an_observational_inference() -> None:
    from personal_knowledge.intelligence.decision import effectiveness

    source = effectiveness.__loader__.get_source(effectiveness.__name__)
    assert 'causal_claim=False' in source
    assert 'cognitive_type="inference"' in source
    for forbidden in ("counterfactual_effect", "causal_claim=True", "UPDATE decision_effectiveness"):
        assert forbidden not in source
