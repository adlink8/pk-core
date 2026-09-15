# Memory Candidate Extraction Eval Rubric

## Hard Reject

- 直接把旧 `memory_items` 当成候选来源
- 结构化 evidence 未经过 bundle 就被直接当作 claim 证据
- 输出引用了输入中不存在的 `source_refs`、`event_id`、`session_id`、`turn_id`
- `extraction_status=proposed` 但没有足够证据
- 明显是一​​次性任务、临时修复、短期错误上下文，却被包装成长期候选
- 把候选 claim 写成已经入库的 memory item

## Downgrade

- claim 有价值，但表达还过于任务化、会话化、或过拟合当前案例
- 证据支持保守候选，不支持强晋升
- 长期价值与一次性风险并存

## Needs Human Review

- duplicate/conflict hints 指向潜在冲突
- 跨 session 泛化幅度较大
- 长期价值看起来成立，但证据还不够自动通过 gate

## Accept Into Promotion Gate

- 候选完全来源于 evidence bundle 及其允许的输入证据
- 能明确区分“候选 claim”与“长期 memory item”
- 能提供最小充分 `evidence_refs` 和 `source_refs`
- 没有借道旧 `memory_items` 充当证据
