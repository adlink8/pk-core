# Graph Candidate Proposal Eval Rubric

## Hard Reject

- 输出引用了输入中不存在的 `source_refs`、`event_id`、`session_id`、`turn_id`
- 把粗召回线索直接当成事实关系
- `proposal_status=proposed` 但 `evidence_refs` 或 `source_refs` 为空
- `proposal_status=proposed` 但 `proposed_relation_type=no_relation`
- 文案把候选写成已确认事实

## Downgrade

- 有关系迹象，但证据仅支持弱信号
- 方向、主体或时序仍有歧义
- 更像“值得后续判边验证”的 pair，而不是高质量候选

## Needs Human Review

- 跨 session 语义桥接过强，证据不足以自动继续
- 存在冲突、歧义、敏感风险、或多种关系都能成立
- 模型无法在不编造证据的前提下给出稳定判断

## Accept Into Next Gate

- 每条 proposal 都可回源到输入 refs
- `why_candidate` 清楚说明“为什么只是候选，不是最终事实”
- `reject` / `downgrade` / `needs_human_review` 被当成正常输出分支，而不是失败兜底
- 风险标记与状态一致
