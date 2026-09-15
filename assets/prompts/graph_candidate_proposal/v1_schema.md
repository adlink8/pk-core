# Graph Candidate Proposal Schema v1

## JSON Schema

```json
{
  "prompt_version": "graph_candidate_proposal/v1",
  "model": "gpt-4.1-mini",
  "temperature": 0.2,
  "llm_status": "live_api_key_present | fallback:no_api_key | blocked:no_live_llm",
  "package_id": "grpkg:20260701:0001",
  "candidate_proposals": [
    {
      "candidate_id": "grcp:20260701:0001",
      "candidate_type": "semantic_relation_candidate | weak_semantic_signal",
      "source_node_id": "turn:session_001:12",
      "target_node_id": "turn:session_001:15",
      "proposed_relation_type": "same_problem | subproblem_of | follow_up | tool_used_for | preference_signal | contradiction | temporal_next | capability_signal | tooling_signal | no_relation",
      "proposal_status": "proposed | downgrade | reject | needs_human_review",
      "why_candidate": "一句中文说明为什么这个 pair 只适合作为候选、弱信号、拒绝或人工复核对象。",
      "evidence_refs": [
        "conversation_turns:turn_id/t_12",
        "conversation_turns:turn_id/t_15"
      ],
      "source_refs": [
        "conversation_turns:turn_id/t_12",
        "conversation_turns:turn_id/t_15"
      ],
      "event_ids": [
        "evt_001"
      ],
      "session_ids": [
        "session_001"
      ],
      "turn_ids": [
        "t_12",
        "t_15"
      ],
      "risk_flags": [
        "weak_evidence",
        "topic_only_similarity",
        "direction_ambiguous"
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
- `package_id`
- `candidate_proposals`

For each proposal:

- `candidate_id`
- `candidate_type`
- `source_node_id`
- `target_node_id`
- `proposed_relation_type`
- `proposal_status`
- `why_candidate`
- `evidence_refs`
- `source_refs`
- `risk_flags`
- `needs_human_review`

## Contract Rules

- `evidence_refs`、`source_refs`、`event_ids`、`session_ids`、`turn_ids` 只能引用输入中已提供的值。
- `proposal_status=proposed` 时，`proposed_relation_type` 不能是 `no_relation`，且 `evidence_refs`、`source_refs` 必须非空。
- `proposal_status=reject` 时，允许 `proposed_relation_type=no_relation`。
- `proposal_status=downgrade` 时，`candidate_type` 应优先为 `weak_semantic_signal`，并在 `risk_flags` 中说明降级原因。
- `needs_human_review=true` 时，`proposal_status` 必须是 `needs_human_review` 或 `downgrade`。
- 所有 proposal 都必须保留“候选而非事实”的语义，不得输出最终落库结论。
