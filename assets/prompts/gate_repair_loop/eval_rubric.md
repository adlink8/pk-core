# Gate Repair Loop Eval Rubric

## Hard Reject

- 新增了输入中不存在的 `source_refs`、`event_id`、`session_id`、`turn_id`
- 试图绕过 deterministic gate
- 把硬失败包装成可直接通过
- 通过补造证据、抬高置信度、删除关键失败原因来“修好”候选

## Valid Repair

- 仅基于已有 refs 做字段收缩或纠错
- 删除无法回源的 refs
- 把过强状态改成 `downgrade`、`needs_human_review` 或 `reject`
- 明确保留 unresolved reasons，供下一层继续审查

## Valid Downgrade

- 候选可能有部分价值，但不足以维持原状态
- 需要转入人工复核或弱候选层

## Valid Reject

- 缺证据
- 只靠 legacy memory 提示支撑
- 一次性任务风险过高
- 冲突/歧义无法在现有证据内解决
