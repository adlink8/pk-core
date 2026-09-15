# Memory Candidate Extraction Schema v1

## JSON Schema

```json
{
  "prompt_version": "memory_candidate_extraction/v1",
  "model": "gpt-4.1-mini",
  "temperature": 0.2,
  "llm_status": "live_api_key_present | fallback:no_api_key | blocked:no_live_llm",
  "bundle_id": "meb:20260701:0001",
  "candidate_claims": [
    {
      "candidate_id": "mcc:20260701:0001",
      "candidate_claim": "用户偏好用 PowerShell 做本机排查，而不是先切到 WSL。",
      "memory_type": "preference | fact | project | habit | capability | tooling",
      "subject": "user | assistant | project | system",
      "extraction_status": "proposed | downgrade | reject | needs_human_review",
      "long_term_value_reason": "一句中文说明这个 claim 为什么可能值得进入长期记忆候选层。",
      "one_time_task_risk": "none | low | medium | high",
      "duplicate_check_hint": "可为空字符串；允许引用旧 memory_id 作为检查提示，但不能当作证据来源。",
      "conflict_check_hint": "可为空字符串；允许引用旧 memory_id 作为检查提示，但不能当作证据来源。",
      "evidence_refs": [
        "memory_evidence_bundles:bundle_id/meb:20260701:0001",
        "conversation_turns:turn_id/t_88"
      ],
      "source_refs": [
        "conversation_turns:turn_id/t_88"
      ],
      "event_ids": [
        "evt_088"
      ],
      "session_ids": [
        "session_009"
      ],
      "turn_ids": [
        "t_88"
      ],
      "confidence": 0.81,
      "risk_flags": [
        "too_task_specific",
        "single_session_only"
      ],
      "needs_human_review": false
    }
  ]
}
```

## Required Fields

- `prompt_version`
- `model`
- `temperature`
- `llm_status`
- `bundle_id`
- `candidate_claims`

For each claim:

- `candidate_id`
- `candidate_claim`
- `memory_type`
- `subject`
- `extraction_status`
- `long_term_value_reason`
- `one_time_task_risk`
- `duplicate_check_hint`
- `conflict_check_hint`
- `evidence_refs`
- `source_refs`
- `confidence`
- `risk_flags`
- `needs_human_review`

## Contract Rules

- `candidate_claim` 必须由输入 evidence bundle 支持，不能仅由旧 `memory_items` 支持。
- `duplicate_check_hint` 和 `conflict_check_hint` 可以提到旧 memory id，但这些 id 不得出现在 `evidence_refs` 或 `source_refs` 里作为候选证据。
- `evidence_refs`、`source_refs`、`event_ids`、`session_ids`、`turn_ids` 只能引用输入中已提供的值。
- `extraction_status=proposed` 时，`candidate_claim`、`evidence_refs`、`source_refs` 必须非空，且 `one_time_task_risk` 不能是 `high`。
- `extraction_status=reject` 时，允许 `confidence=0.0`，并应在 `risk_flags` 中写明拒绝原因。
- `needs_human_review=true` 时，`extraction_status` 必须是 `needs_human_review` 或 `downgrade`。
- 所有输出都是候选 claim，不得声明为最终长期 memory item。
