# Memory Relation Proposal Schema v1

## JSON Schema

```json
{
  "prompt_version": "memory_relation_proposal/v1",
  "model": "gpt-5.4",
  "temperature": 0.2,
  "llm_status": "live_api_key_present | fallback:no_api_key | blocked:no_live_llm",
  "package_id": "mrpkg:20260702:0001",
  "candidate_proposals": [
    {
      "candidate_id": "mrcand:20260702:0001",
      "candidate_type": "semantic_relation_candidate | weak_memory_signal",
      "source_memory_id": "mem_001",
      "target_memory_id": "mem_002",
      "proposed_relation_type": "same_subject | related_topic | enables | uses_tool | embodies | conflicts_with | refines | supports | no_relation",
      "proposal_status": "proposed | downgrade | reject | needs_human_review",
      "confidence": 0.82,
      "why_candidate": "一句中文说明为什么这只是长期记忆关系候选。",
      "evidence_refs": [
        "memory_id:mem_001",
        "memory_field:mem_002:subject",
        "linked_ref:source-a:1"
      ],
      "source_refs": [
        "memory_id:mem_001",
        "memory_id:mem_002",
        "linked_ref:source-a:1"
      ],
      "risk_flags": [
        "weak_evidence",
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
- `source_memory_id`
- `target_memory_id`
- `proposed_relation_type`
- `proposal_status`
- `confidence`
- `why_candidate`
- `evidence_refs`
- `source_refs`
- `risk_flags`
- `needs_human_review`

## Contract Rules

- `evidence_refs`、`source_refs` 只能引用输入 package 已提供的 `allowed_refs`。
- `proposal_status=proposed` 时，`proposed_relation_type` 不能是 `no_relation`，并且 `confidence` 必须大于 0。
- `proposal_status=reject` 时，允许 `proposed_relation_type=no_relation`。
- `proposal_status=downgrade` 时，`candidate_type` 应优先为 `weak_memory_signal`。
- `needs_human_review=true` 时，`proposal_status` 必须是 `needs_human_review` 或 `downgrade`。
- 所有 proposal 都必须保持“候选而非事实”的语义，不得输出直接写入 `memory_relations` 的结论。
