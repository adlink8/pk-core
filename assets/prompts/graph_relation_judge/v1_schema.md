# Graph Relation Judge Schema v1

## JSON Schema
```json
{
  "candidate_id": "grc:semantic_candidate:abcd1234",
  "relation_type": "same_problem | subproblem_of | follow_up | tool_used_for | preference_signal | contradiction | temporal_next | no_relation",
  "confidence": 0.0,
  "evidence_refs": ["path:line", "path:line"],
  "reason": "一句中文说明为什么判成这个关系或为什么无关系",
  "risk_flags": ["evidence_weak", "topic_only_similarity"]
}
```
