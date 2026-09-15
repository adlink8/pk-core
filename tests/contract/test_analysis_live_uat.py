from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_knowledge.intelligence.analysis.executor import ExecutionReceipt
from personal_knowledge.intelligence.analysis.live_uat import (
    LiveUatError, SPEC_KEYS, SPEC_VERSION, confirmation_phrase, load_live_spec,
    run_live_uat,
)
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding, DecisionContextPolicy,
)
from personal_knowledge.intelligence.analysis.schema import checksum


def _spec(tmp_path: Path) -> dict:
    paths = {}
    for name in ("personal", "external", "analysis"):
        path = tmp_path / f"{name}.sqlite"
        path.write_bytes(name.encode())
        paths[name] = str(path)
    evidence = lambda authority, record, snap, snap_hash: {
        "authority_id": authority, "record_type": "fact", "record_id": record,
        "record_checksum": "3" * 64, "snapshot_id": snap,
        "snapshot_hash": snap_hash,
    }
    spec = {
        "schema_version": SPEC_VERSION, "model": "gpt-5.4",
        "personal_db": paths["personal"], "external_db": paths["external"],
        "analysis_db": paths["analysis"], "goal": "choose rollout",
        "constraints": ["no external action"], "weights": {"safety": 1.0},
        "risk_budget": "low",
        "personal_evidence": [evidence("a.personal_change", "p1", "ps", "1" * 64)],
        "external_evidence": [evidence("s.external_fact", "e1", "es", "2" * 64)],
        "region": "global", "max_external_age_seconds": 86400,
        "temperature": 0.0, "max_output_tokens": 1000,
        "max_total_tokens": 2000, "timeout_seconds": 30.0,
    }
    assert set(spec) == SPEC_KEYS
    return spec


def test_live_uat_requires_exact_confirmation_before_preflight(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.live_uat.codex_cli_preflight",
        lambda model: pytest.fail("preflight must not run"),
    )
    with pytest.raises(LiveUatError, match="live_confirmation_mismatch"):
        run_live_uat(
            spec, confirmed_at="2026-07-18T12:00:00Z", confirmation_event_id="c1",
            confirmation="wrong", write=True, working_directory=tmp_path,
        )


def test_live_uat_executes_once_and_reports_source_isolation(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.live_uat.codex_cli_preflight",
        lambda model: {"ok": True, "credential_present": True, "findings": (),
                       "provider_calls": 0, "command_path": "codex"},
    )
    draft = DecisionContextBinding(
        "ps", "1" * 64, "es", "2" * 64,
        DecisionContextPolicy("global", 86400), "2026-07-18T12:00:00Z", "",
    )
    binding = replace(draft, binding_hash=checksum(draft.core()))
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.live_uat.create_decision_context_binding",
        lambda *args, **kwargs: binding,
    )
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.live_uat.build_confirmed_input",
        lambda **kwargs: SimpleNamespace(rendered_prompt="ascii prompt"),
    )
    class Provider:
        calls = 0
        def __init__(self, **kwargs): pass
    provider = Provider()
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.live_uat.execute_analysis",
        lambda **kwargs: (
            setattr(provider, "calls", 1) or
            ExecutionReceipt(
                status="candidate", stage="publish", reason_codes=(), attempts=1,
                request_checksum="4" * 64, response_checksum="5" * 64,
                run_id="r1", candidate_id="c1", written=True,
            )
        ),
    )
    report = run_live_uat(
        spec, confirmed_at="2026-07-18T12:00:00Z", confirmation_event_id="c1",
        confirmation=confirmation_phrase(spec), write=True, working_directory=tmp_path,
        provider_factory=lambda **kwargs: provider,
    )
    assert report["ok"] and report["provider_calls"] == 1
    assert report["personal_unchanged"] and report["external_unchanged"]
    assert not report["analysis_changed"] and report["actions_executed"] == 0


def test_frozen_live_request_is_strict_and_uses_only_bound_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    spec = load_live_spec(
        root / ".planning/phases/29-structured-llm-decision-analysis/29-LIVE-UAT-REQUEST.json"
    )
    assert spec["model"] == "gpt-5.4" and spec["risk_budget"] == "low"
    assert len(spec["personal_evidence"]) == 1 and len(spec["external_evidence"]) == 4
    assert all(item["snapshot_id"] == "exs_a7770b7d4e9e2727e359befc"
               for item in spec["external_evidence"])


def test_live_confirmation_binds_schema_lineage(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path)
    baseline = confirmation_phrase(spec)
    schema = tmp_path / "schema.json"
    source = Path(__file__).resolve().parents[2] / "assets/schemas/decision_analysis_response_v1.json"
    schema.write_bytes(source.read_bytes() + b"\n")
    monkeypatch.setattr(
        "personal_knowledge.intelligence.analysis.live_uat.DEFAULT_SCHEMA_PATH", schema,
    )
    assert confirmation_phrase(spec) != baseline
