# Graph Relation Judge Eval Rubric

## Gate Rules

接受前提:
- relation_type != no_relation
- relation_type 在白名单内
- confidence >= 0.75
- evidence_refs 非空，且能对应 candidate 的 source_refs
- 同一 pair 不存在多个高置信强关系冲突

进入 review_queue:
- confidence 在 0.55-0.75
- evidence_refs 不足或对应不上
- risk_flags 非空
- 同 pair 存在多个关系类型冲突

直接拒绝:
- relation_type = no_relation
- confidence < 0.55
- relation_type 不在白名单
