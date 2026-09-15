# Memory Experiment Judge Eval Rubric

## Comparison Principles

优先保留:
- 有明确 source traceability。
- 能表达稳定偏好、持续工具使用、长期能力、长期项目上下文。
- 对后续检索或注入有复用价值。

优先降级或只留分析层:
- 只是一次性任务、一次性报错、一次性 follow-up。
- 关系存在，但更像过程分析线索，不适合作长期 memory claim。
- 证据路径弱、只靠主题相近、无法清楚回溯。

优先合并:
- 新 graph edge 明确补充了旧 memory 的语义上下文。
- 旧 memory 本身有长期价值，但 graph edge 让其证据链更完整。

优先 delete candidate:
- 旧 memory 明显是早期规则实验噪声，且新图谱/上下文不支持其长期价值。
- evidence_count 很低，source traceability 弱，检索复用价值低。

## Action Guidance

- `keep`: 旧 memory 继续保留，但本 Wave 不直接写回任何表。
- `merge_candidate`: 旧 memory 和 graph signal 互补，适合进入后续 promotion candidate 层。
- `downgrade`: 保留审计价值，但不应继续作为高优先级长期 memory。
- `delete_candidate`: 可标记为后续人工确认删除候选。
- `promote_candidate`: graph edge 具备长期信号，适合进入 Wave 3 promotion candidate 层。
- `analysis_only`: graph edge 有分析价值，但不适合晋级长期 memory。
- `review_only`: 风险较高，后续只能人工复核。

## Scoring Hints

- `long_term_value_score`
  - 8-10: 明显稳定、跨会话可复用、证据链清楚
  - 5-7: 有一定长期价值，但仍需和另一层对照
  - 0-4: 更像一次性上下文或弱证据分析线索

- `dimension_scores.*`
  - 0-1: 很弱 / 很低
  - 2-3: 中等
  - 4-5: 很强 / 很高

