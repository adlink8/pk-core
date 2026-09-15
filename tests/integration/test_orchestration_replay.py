from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from personal_knowledge.intelligence.analysis.schema import checksum
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding, DecisionContextPolicy,
)
from personal_knowledge.intelligence.orchestration import (
    OrchestrationError, OrchestrationService, apply_schema,
    execute_confirmed_generation,
)
from personal_knowledge.intelligence.orchestration.generation import reserve_generation


NOW = "2026-07-19T02:00:00Z"
ACTOR = hashlib.sha256(b"actor-generation-test").hexdigest()
SECRET = b"phase-33-generation-confirmation-secret-long"


def _binding(*_args, **_kwargs):
    draft = DecisionContextBinding(
        "ss_1", "a" * 64, "exs_1", "b" * 64,
        DecisionContextPolicy("global", 86400), NOW, "",
    )
    return replace(draft, binding_hash=checksum(draft.core()))


def _validate(value, *_args, **_kwargs):
    typed = value if isinstance(value, DecisionContextBinding) else DecisionContextBinding.from_dict(value)
    assert checksum(typed.core()) == typed.binding_hash
    return {"binding": typed.to_dict()}


def _service(tmp_path: Path):
    db = tmp_path / "orchestration.sqlite"
    apply_schema(db)
    service = OrchestrationService(
        db_path=db, personal_db=tmp_path / "personal.sqlite",
        external_db=tmp_path / "external.sqlite", confirmation_secret=SECRET,
        binding_factory=_binding, binding_validator=_validate,
    )
    prepared = service.prepare(
        goal="Choose local verification", constraints=("No external action",),
        weights={"confidence": 0.8}, actor_identity_hash=ACTOR, now=NOW,
    )
    confirmed = service.confirm(
        prepared, confirmation_token=service.issue_confirmation(prepared),
        idempotency_key="confirm", now=NOW,
    )
    preview = service.preview_transition(
        confirmed.session_id, "generate", {"personal_evidence": [], "external_evidence": []},
        actor_identity_hash=ACTOR, expected_sequence=1, now=NOW,
    )
    return service, preview


def test_completed_generation_calls_runner_once_and_replays(tmp_path: Path):
    service, preview = _service(tmp_path)
    token = service.issue_confirmation(preview)
    calls = 0

    def runner(_manifest, _payload, _confirmation, _now):
        nonlocal calls
        calls += 1
        return {
            "status": "success",
            "references": {
                "run_id": "dar_test", "candidate_id": "dac_test",
                "run_checksum": "c" * 64, "candidate_checksum": "d" * 64,
            },
        }

    first = execute_confirmed_generation(
        service, preview, confirmation_token=token, idempotency_key="generate",
        runner=runner, now=NOW,
    )
    replay = execute_confirmed_generation(
        service, preview, confirmation_token=token, idempotency_key="generate",
        runner=runner, now=NOW,
    )
    assert calls == 1
    assert first.event_id == replay.event_id
    assert replay.replayed is True
    assert service.resume(preview.session_id, now=NOW)["state"] == "generated"


def test_reserved_replay_fails_closed_without_calling_runner(tmp_path: Path):
    service, preview = _service(tmp_path)
    token = service.issue_confirmation(preview)
    reserved = reserve_generation(
        service, preview, confirmation_token=token,
        idempotency_key="uncertain", now=NOW,
    )
    assert reserved["new"] is True
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return {}

    with pytest.raises(OrchestrationError, match="provider_outcome_unknown"):
        execute_confirmed_generation(
            service, preview, confirmation_token=token,
            idempotency_key="uncertain", runner=runner, now=NOW,
        )
    assert calls == 0


def test_consumed_token_reuse_after_transition_fails_closed_without_runner_call(tmp_path: Path):
    """Phase 38-03（D-38-05/DEC-03）：generate 完成后复用同 token + 新幂等键重发，
    状态机已推进 → 稳定 typed 拒绝（illegal_transition），runner 不被二次调用，事件链不变。"""
    service, preview = _service(tmp_path)
    token = service.issue_confirmation(preview)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return {
            "status": "success",
            "references": {
                "run_id": "dar_test", "candidate_id": "dac_test",
                "run_checksum": "c" * 64, "candidate_checksum": "d" * 64,
            },
        }

    first = execute_confirmed_generation(
        service, preview, confirmation_token=token, idempotency_key="gen-consume",
        runner=runner, now=NOW,
    )
    assert calls == 1
    with pytest.raises(OrchestrationError, match="illegal_transition"):
        execute_confirmed_generation(
            service, preview, confirmation_token=token, idempotency_key="gen-consume-2",
            runner=runner, now=NOW,
        )
    assert calls == 1  # 拒绝路径零 Provider 调用
    resumed = service.resume(preview.session_id, now=NOW)
    assert resumed["state"] == "generated"
    assert resumed["last_event_checksum"] == first.event_checksum  # 事件链无新增


def test_provider_unknown_leaves_resume_readonly_and_key_stable(tmp_path: Path):
    """Phase 38-03（D-38-07）：provider_outcome_unknown 后 resume 只读仍可用、
    状态不被伪推进；换幂等键也不能绕过 reserved fail-closed（不换键原则由服务端强制）。"""
    service, preview = _service(tmp_path)
    token = service.issue_confirmation(preview)
    reserved = reserve_generation(
        service, preview, confirmation_token=token, idempotency_key="uncertain", now=NOW,
    )
    assert reserved["new"] is True
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return {}

    with pytest.raises(OrchestrationError, match="provider_outcome_unknown"):
        execute_confirmed_generation(
            service, preview, confirmation_token=token,
            idempotency_key="uncertain", runner=runner, now=NOW,
        )
    # resume 只读恢复仍可用，状态停在 confirmed（不伪造 generated）
    resumed = service.resume(preview.session_id, now=NOW)
    assert resumed["state"] == "confirmed"
    assert calls == 0  # 全程零 Provider 调用


def test_generation_abstention_is_terminal_without_state_transition(tmp_path: Path):
    service, preview = _service(tmp_path)
    token = service.issue_confirmation(preview)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return {"status": "abstain", "reason_codes": ["evidence_missing"]}

    first = execute_confirmed_generation(
        service, preview, confirmation_token=token, idempotency_key="abstain",
        runner=runner, now=NOW,
    )
    replay = execute_confirmed_generation(
        service, preview, confirmation_token=token, idempotency_key="abstain",
        runner=runner, now=NOW,
    )
    assert calls == 1
    assert first["status"] == "abstain"
    assert replay["replayed"] is True
    assert service.resume(preview.session_id, now=NOW)["state"] == "confirmed"
