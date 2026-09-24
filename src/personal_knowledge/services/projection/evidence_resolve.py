"""evidence_resolve.get — 唯一的只读证据下钻入口(Phase 37:EVID-01)。

输入是三种类型化引用之一(personal_state / external_fact / decision),服务端先
校验 stable_id / snapshot_id / checksum(personal_state 另需完整 state key)结构,
再仅调度到既有 IntelligenceService.state.explain、
DecisionIntelligenceReadService.external.explain 或
DecisionFeedbackService.recommendations.get 三条只读路径之一——不接受任意资源
标识、路径或 URL。篡改/过期 binding、未知 subject、单 authority 故障与 evidence
不足一律返回可区分的 typed status,绝不回退到"最新记录"、绝不返回 sealed
assertion value / 原始正文 / provider body / confirmation-HMAC 材料;External
分支同样只返回与 external_delta.get 同构的 metadata(不含 raw value),保持与
列表投影同一隐私边界。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService

from ._shared import (
    _envelope,
    _error,
    _safe_failure_message,
    _utc_now,
)

# evidence_resolve.get 的固定 subject_type 词表、personal_state 引用所需 state key
# 字段与只读证据解析结果的固定 status 词表(Phase 37:EVID-01)。resolver 只接受这三种
# 类型化引用,一律先做 stable_id/snapshot_id/checksum(personal_state 另加完整 state
# key)结构校验,再仅调度到既有只读 explain/get 路径;不接受任意资源标识或路径。
_EVIDENCE_SUBJECT_TYPES = frozenset({"personal_state", "external_fact", "decision"})
_STATE_KEY_FIELDS = ("assertion_kind", "subject", "domain", "scope", "predicate")
_EVIDENCE_RESULT_STATUSES = frozenset({
    "ok", "mismatch", "expired", "abstain", "not_found", "authority_unavailable",
})


def build(
    db: Path | None,
    read_service: DecisionIntelligenceReadService | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    db = Path(db) if db else UNIFIED_DB
    read_service = read_service or DecisionIntelligenceReadService()
    return _evidence_resolve_get(db, read_service, **params)


def _evidence_resolve_get(
    db_path: Path,
    read_service: DecisionIntelligenceReadService,
    **params: Any,
) -> dict[str, Any]:
    subject_type = str(params.get("subject_type") or "").strip()
    stable_id = str(params.get("stable_id") or "").strip()
    snapshot_id = str(params.get("snapshot_id") or "").strip()
    checksum_value = str(params.get("checksum") or "").strip()
    if subject_type not in _EVIDENCE_SUBJECT_TYPES:
        return _error(
            "evidence_resolve.get", "invalid_input",
            "subject_type 必须是 personal_state/external_fact/decision 之一",
        )
    if not (stable_id and snapshot_id and checksum_value):
        return _error(
            "evidence_resolve.get", "invalid_input", "stable_id/snapshot_id/checksum 均为必填",
        )
    reference: dict[str, Any] = {
        "subject_type": subject_type, "stable_id": stable_id,
        "snapshot_id": snapshot_id, "checksum": checksum_value,
    }
    if subject_type == "personal_state":
        key = {field: str(params.get(field) or "").strip() for field in _STATE_KEY_FIELDS}
        if any(not value for value in key.values()):
            return _error(
                "evidence_resolve.get", "invalid_input",
                "personal_state 引用缺少完整 state key"
                "(assertion_kind/subject/domain/scope/predicate)",
            )
        reference.update(key)

    generated_at = _utc_now()
    resolvers: dict[str, Callable[
        [Path, DecisionIntelligenceReadService, Mapping[str, Any]],
        tuple[str, dict[str, Any] | None, list[str], list[str]],
    ]] = {
        "personal_state": _resolve_personal_state_evidence,
        "external_fact": _resolve_external_fact_evidence,
        "decision": _resolve_decision_evidence,
    }
    try:
        status, evidence_result, limitations, next_actions = resolvers[subject_type](
            db_path, read_service, reference,
        )
    except Exception:  # noqa: BLE001 — 单次证据解析失败必须被隔离,详情不进入公开响应
        status, evidence_result = "authority_unavailable", None
        next_actions = ["稍后重试,或返回状态/External/决策页重新进入下钻"]
        limitations = [_safe_failure_message(subject_type, "authority_read_failed")]
    authority_status = (
        "error" if status == "authority_unavailable"
        else "empty" if status == "not_found"
        else "ok"
    )
    data = {
        "status": status, "reference": reference,
        "result": evidence_result, "next_actions": next_actions,
    }
    snapshot_bindings = {
        "personal": snapshot_id if subject_type == "personal_state" else None,
        "external": snapshot_id if subject_type == "external_fact" else None,
        "serving": None,
    }
    freshness = {
        "personal_as_of": None, "knowledge_unit_count": None, "generated_at": generated_at,
    }
    return _envelope(
        "evidence_resolve.get", generated_at, data, {"evidence": authority_status}, limitations,
        snapshot_bindings, freshness,
    )


def _resolve_personal_state_evidence(
    db_path: Path,
    read_service: DecisionIntelligenceReadService,
    reference: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None, list[str], list[str]]:
    key_params = {field: reference[field] for field in _STATE_KEY_FIELDS}
    result = IntelligenceService(db_path).invoke(
        "state.explain", snapshot_id=reference["snapshot_id"], **key_params,
    )
    if not result.get("ok"):
        code = str((result.get("error") or {}).get("code") or "")
        if code == "state_key_missing":
            return "not_found", None, [], ["确认该断言在当前 snapshot 下是否仍存在"]
        if code in {"snapshot_missing", "snapshot_not_validated", "run_missing"}:
            # run_missing 同 _intelligence_data_or_raise 的既有口径(D-36-02):
            # "该 snapshot 尚无已提交 personal_state run" 是权威自身的空状态语义,
            # 不是读取异常;这里的引用必然指向一次曾经存在的断言,故归入
            # expired(绑定的 snapshot 语境已不可再被解释),而非伪造的 authority 故障
            return (
                "expired", None, [f"引用的 snapshot 已失效({code})"],
                ["刷新个人状态页后重新下钻"],
            )
        raise ValueError(code or "personal_evidence_read_failed")
    data = result["data"]
    if (
        data.get("current_assertion_id") != reference["stable_id"]
        or data.get("current_value_checksum") != reference["checksum"]
    ):
        return (
            "mismatch", None, ["当前断言已变化,stable_id/checksum 不再匹配"],
            ["刷新个人状态页后重新下钻"],
        )
    evidence = [
        {
            "ref": item.get("ref"), "artifact_type": item.get("artifact_type"),
            "status": item.get("status"), "eligible": item.get("eligible"),
            "privacy_class": item.get("privacy_class"),
        }
        for item in list(data.get("evidence") or [])
    ]
    payload = {
        "subject_type": "personal_state", "stable_id": reference["stable_id"],
        "snapshot_id": data.get("snapshot_id"), "checksum": data.get("current_value_checksum"),
        "key": data.get("key"), "record_lifecycle": data.get("state_status"),
        "provenance_class": data.get("provenance_class"), "confidence": data.get("confidence"),
        "as_of": data.get("as_of"), "evidence": evidence,
        "uncertainty": list(data.get("uncertainty") or []),
    }
    if data.get("abstained"):
        return "abstain", payload, [], ["evidence 暂不满足可用性判定,可稍后重试或改看其它断言"]
    return "ok", payload, [], []


def _resolve_external_fact_evidence(
    db_path: Path,
    read_service: DecisionIntelligenceReadService,
    reference: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None, list[str], list[str]]:
    result = read_service.invoke(
        "external.explain", resource_type="fact", resource_id=reference["stable_id"],
    )
    if not result.get("ok"):
        code = str((result.get("error") or {}).get("code") or "")
        if code == "fact_missing":
            return "not_found", None, [], ["确认 fact_id 是否仍存在"]
        if code in {"fact_not_in_active_snapshot", "fact_snapshot_drift"}:
            return (
                "expired", None, [f"fact 与当前 active External snapshot 不一致({code})"],
                ["刷新 External 页面后重新查看"],
            )
        raise ValueError(code or "external_evidence_read_failed")
    item = (result.get("data") or {}).get("item") or {}
    if (
        item.get("snapshot_id") != reference["snapshot_id"]
        or item.get("fact_checksum") != reference["checksum"]
    ):
        return (
            "mismatch", None, ["当前 External snapshot 或 fact checksum 已变化"],
            ["刷新 External 页面后重新下钻"],
        )
    payload = {
        "subject_type": "external_fact", "stable_id": item.get("fact_id"),
        "snapshot_id": item.get("snapshot_id"), "checksum": item.get("fact_checksum"),
        "subject": item.get("subject"), "predicate": item.get("predicate"),
        "region": item.get("region"), "valid_from": item.get("valid_from"),
        "valid_to": item.get("valid_to"), "source_quality": item.get("source_quality"),
        "fact_confidence": item.get("fact_confidence"), "lifecycle": item.get("lifecycle"),
    }
    limitations = list((result.get("data") or {}).get("limitations") or [])
    next_actions = list((result.get("data") or {}).get("next_actions") or [])
    return "ok", payload, limitations, next_actions


def _resolve_decision_evidence(
    db_path: Path,
    read_service: DecisionIntelligenceReadService,
    reference: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None, list[str], list[str]]:
    result = DecisionFeedbackService(db_path).invoke(
        "recommendations.get", recommendation_id=reference["stable_id"],
    )
    if not result.get("ok"):
        code = str((result.get("error") or {}).get("code") or "")
        if code == "recommendation_missing":
            return "not_found", None, [], ["确认 recommendation_id 是否仍存在"]
        raise ValueError(code or "decision_evidence_read_failed")
    item = result["data"]
    if (
        item.get("snapshot_id") != reference["snapshot_id"]
        or item.get("recommendation_checksum") != reference["checksum"]
    ):
        return (
            "mismatch", None, ["当前 recommendation snapshot 或 checksum 已变化"],
            ["刷新决策工作区后重新下钻"],
        )
    payload = {
        "subject_type": "decision", "stable_id": item.get("recommendation_id"),
        "snapshot_id": item.get("snapshot_id"), "checksum": item.get("recommendation_checksum"),
        "confirmation_state": item.get("confirmation_state"),
        "action_state": item.get("action_state"),
        "recommendation_kind": item.get("recommendation_kind"), "domain": item.get("domain"),
        "rationale_codes": item.get("rationale_codes"), "support": item.get("support"),
    }
    return "ok", payload, [], []
