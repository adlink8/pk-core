from personal_knowledge.retrieval.relevance import decide_evidence_support


def _candidate(**overrides):
    value = {
        "subject": "语言偏好",
        "answer": "用户默认使用中文沟通",
        "lifecycle": "current",
        "source_message_ref": "cm|1",
    }
    value.update(overrides)
    return value


def test_supported_requires_grounding_and_eligible_evidence() -> None:
    decision = decide_evidence_support(
        "用户的语言偏好是什么",
        _candidate(),
        resolve=lambda ref: {"ref": ref, "status": "ok", "eligible": True},
    )
    assert decision.state == "supported"
    assert decision.evidence_refs == ("cm|1",)
    assert decision.reason_codes == ("eligible_evidence", "query_candidate_grounded")


def test_ineligible_and_missing_evidence_fail_closed() -> None:
    ineligible = decide_evidence_support(
        "语言偏好", _candidate(), resolve=lambda ref: {"status": "ineligible"}
    )
    missing = decide_evidence_support(
        "语言偏好", _candidate(), resolve=lambda ref: {"status": "missing"}
    )
    assert ineligible.state == "unsupported"
    assert ineligible.reason_codes == ("evidence_ineligible",)
    assert missing.state == "unsupported"
    assert missing.reason_codes == ("evidence_missing",)


def test_lifecycle_and_privacy_veto_are_deterministic() -> None:
    lifecycle = decide_evidence_support("语言偏好", _candidate(lifecycle="superseded"))
    privacy = decide_evidence_support("语言偏好", _candidate(privacy_tier="secret"))
    assert lifecycle.reason_codes == ("lifecycle_not_current",)
    assert privacy.reason_codes == ("privacy_or_provenance_veto",)


def test_unresolved_legacy_candidate_is_uncertain_not_falsely_supported() -> None:
    decision = decide_evidence_support(
        "语言偏好",
        {"title": "语言偏好", "content": "使用中文"},
    )
    assert decision.state == "uncertain"
    assert "evidence_reference_absent" in decision.reason_codes


def test_resolved_but_ungrounded_candidate_is_unsupported() -> None:
    decision = decide_evidence_support(
        "完全不同的主题", _candidate(), resolve=lambda ref: {"status": "ok"}
    )
    assert decision.state == "unsupported"
    assert decision.reason_codes == ("query_candidate_ungrounded",)

    generic = decide_evidence_support(
        "no private content",
        _candidate(answer="object has no attribute"),
        resolve=lambda ref: {"status": "ok"},
    )
    assert generic.state == "unsupported"


def test_sensitive_value_request_is_vetoed_before_retrieval() -> None:
    for query in ("用户护照号码是多少", "部署密钥是什么", "银行卡和支付账户明细"):
        decision = decide_evidence_support(
            query, _candidate(), resolve=lambda ref: {"status": "ok"}
        )
        assert decision.state == "unsupported"
        assert decision.reason_codes == ("sensitive_value_request",)


def test_expected_labels_cannot_change_runtime_decision() -> None:
    resolver = lambda ref: {"status": "ok", "eligible": True}
    a = decide_evidence_support("语言偏好", _candidate(expected_abstain=True), resolve=resolver)
    b = decide_evidence_support("语言偏好", _candidate(expected_abstain=False), resolve=resolver)
    assert a == b


def test_explicit_evidence_literal_condition_fails_when_source_lacks_literal() -> None:
    query = "语言偏好是什么？仅当证据逐字包含校验码 DEV-NO-EVIDENCE-ABC123 时回答。"
    decision = decide_evidence_support(
        query,
        _candidate(),
        resolve=lambda ref: {"status": "ok", "eligible": True, "content": "用户默认使用中文"},
    )
    assert decision.state == "unsupported"
    assert decision.reason_codes == ("required_literal_absent",)


def test_explicit_evidence_literal_condition_passes_when_source_contains_literal() -> None:
    query = "语言偏好是什么？仅当证据逐字包含 VERIFIED-ABC123 时回答。"
    decision = decide_evidence_support(
        query,
        _candidate(),
        resolve=lambda ref: {"status": "ok", "eligible": True, "content": "VERIFIED-ABC123 用户默认使用中文"},
    )
    assert decision.state == "supported"
