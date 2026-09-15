# Memory Promotion Judge Schema v1

## JSON Schema

```json
{
  "promotion_id": "mpc:abcd1234",
  "promotion_status": "approved | review_required | rejected",
  "memory_type": "preference | fact | project | habit | capability | tooling",
  "canonical_claim": "Stable, reusable claim suitable for long-term memory if approved.",
  "merge_or_replace_target": {
    "action": "none | merge | replace",
    "memory_id": null,
    "reason": "Why this target is or is not used."
  },
  "risk_flags": [
    "no_live_llm",
    "upstream_needs_live_llm_review",
    "one_time_task",
    "evidence_unresolved",
    "duplicate_candidate",
    "conflict_candidate"
  ],
  "human_review_required": true,
  "reason": "Short explanation of the gate result."
}
```

## Required Fields

- `promotion_status`
- `memory_type`
- `canonical_claim`
- `merge_or_replace_target`
- `risk_flags`
- `human_review_required`

## Status Semantics

- `approved`: eligible for controlled apply only when `human_review_required=false`.
- `review_required`: evidence may be useful but a human must decide.
- `rejected`: not suitable for long-term memory under this gate.
