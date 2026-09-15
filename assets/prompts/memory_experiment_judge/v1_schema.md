# Memory Experiment Judge Schema v1

## JSON Schema
```json
{
  "record_kind": "old_memory_sample | accepted_graph_edge | review_queue_sample",
  "old_memory_id": "string | null",
  "new_candidate_id": "string | null",
  "judgment": "retain_old_memory | merge_candidate | downgrade_old_memory | delete_candidate | graph_promotion_candidate | analysis_only | review_only | no_clear_match",
  "long_term_value_score": 0,
  "duplicate_status": "distinct | overlaps_old_memory | duplicate_old_memory | duplicate_graph_signal | no_clear_match | potential_duplicate",
  "conflict_status": "no_conflict | conflicts_old_memory | conflicts_graph_context | temporal_or_scope_conflict | insufficient_evidence",
  "recommended_action": "keep | merge_candidate | downgrade | delete_candidate | promote_candidate | analysis_only | review_only",
  "dimension_scores": {
    "evidence_coverage": 0,
    "source_traceability": 0,
    "relation_depth": 0,
    "noise_risk": 0,
    "long_term_usefulness": 0,
    "retrieval_usefulness": 0,
    "duplicate_overlap": 0,
    "conflict_risk": 0
  },
  "evidence_refs": ["allowed-ref-1", "allowed-ref-2"],
  "reason": "中文说明。要说清楚为什么保留 / 降级 / 合并 / 只留分析层。",
  "risk_flags": ["one_off_task", "evidence_weak"]
}
```

## Field Notes

- `record_kind`: 焦点对象类型，不是上下文类型。
- `old_memory_id`: 旧 memory 焦点时必填；graph edge 焦点时可为最相关旧 memory，也可为 `null`。
- `new_candidate_id`: graph edge / review sample 焦点时必填；旧 memory 焦点时可为最相关 graph candidate，也可为 `null`。
- `noise_risk`: 0=低噪声，5=高噪声。
- `duplicate_overlap`: 0=无重叠，5=高度重复。
- `conflict_risk`: 0=无冲突风险，5=高冲突风险。

