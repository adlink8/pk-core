# Gate Repair Loop Schema v1

## JSON Schema

```json
{
  "prompt_version": "gate_repair_loop/v1",
  "model": "gpt-4.1-mini",
  "temperature": 0.0,
  "llm_status": "live_api_key_present | fallback:no_api_key | blocked:no_live_llm",
  "candidate_kind": "graph_candidate | memory_candidate",
  "candidate_id": "grcp:20260701:0001",
  "repair_action": "repair | downgrade | reject",
  "repaired_status": "proposed | downgrade | reject | needs_human_review",
  "kept_evidence_refs": [
    "conversation_turns:turn_id/t_12"
  ],
  "kept_source_refs": [
    "conversation_turns:turn_id/t_12"
  ],
  "event_ids": [
    "evt_012"
  ],
  "session_ids": [
    "session_001"
  ],
  "turn_ids": [
    "t_12"
  ],
  "repaired_fields": {
    "proposed_relation_type": "follow_up",
    "canonical_claim": "保守改写后的候选 claim",
    "risk_flags": [
      "weak_evidence"
    ],
    "needs_human_review": true
  },
  "unresolved_gate_reasons": [
    "direction_ambiguous"
  ],
  "repair_reason": "一句中文说明为什么只能 repair、downgrade 或 reject，以及是否仍需人工复核。"
}
```

## Required Fields

- `prompt_version`
- `model`
- `temperature`
- `llm_status`
- `candidate_kind`
- `candidate_id`
- `repair_action`
- `repaired_status`
- `kept_evidence_refs`
- `kept_source_refs`
- `repaired_fields`
- `unresolved_gate_reasons`
- `repair_reason`

## Contract Rules

- `kept_evidence_refs` 和 `kept_source_refs` 只能来自输入已给出的 refs。
- `event_ids`、`session_ids`、`turn_ids` 只能引用输入中已提供的值。
- `repair_action=repair` 时，只允许保守收缩、纠错、删减，不允许新增证据。
- `repair_action=downgrade` 时，`repaired_status` 必须是 `downgrade` 或 `needs_human_review`。
- `repair_action=reject` 时，`repaired_status` 必须是 `reject`。
- `repaired_fields` 只能覆盖当前候选已有字段的保守版本；不能引入新结构绕过 gate。
- 如果 hard fail 仍未解决，必须体现在 `unresolved_gate_reasons` 中，不能伪装成完全修复。
