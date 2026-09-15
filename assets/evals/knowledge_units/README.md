# Knowledge Unit Eval Datasets

Phase 14 Wave 0 的评估数据集。用于 dev tuning、frozen-test A/B、merge 评估和 hard-negative 校验。

## Schema

每个 case 是一行 JSONL：

```json
{
  "id": "dev-001",
  "split": "dev|frozen_test|merge_positive|hard_negative",
  "query": "用户用什么 shell？",
  "gold_evidence_refs": ["cm|..."],
  "allowed_unit_types": ["preference", "personal_fact"],
  "expected_abstain": false,
  "expected_conflict": false,
  "group": "preference|project_decision|capability|time_conflict|deprecated|no_answer|assistant_only|subagent_only|secret_ineligible|cross_source_dup"
}
```

## Splits

| Split | 数量 | 用途 | 修改规则 |
|-------|------|------|----------|
| dev | 20 | 调优 prompt/参数 | 可自由修改 |
| frozen_test | 20 | A/B gate | 确认后不再由 pipeline 修改 |
| merge_positive | 20 | canonical merge recall | 验证应合并的 pair |
| hard_negative | 20 | merge false-positive | 验证不应合并的 pair |

## 场景覆盖

- **preference**：用户偏好（shell、语言、输出格式）
- **project_decision**：项目架构/技术选型决策
- **capability**：用户技能/能力使用
- **time_conflict**：同一 subject 不同时间矛盾结论
- **deprecated**：旧结论已被替代
- **no_answer**：无足够证据，应 abstain
- **assistant_only**：只有 assistant 内容，不能证明用户事实
- **subagent_only**：只有 subagent 内容，不能证明用户事实
- **secret_ineligible**：secret-bearing session，不可检索
- **cross_source_dup**：跨 source 重复，应折叠

## 文件

- `dev_queries.private.jsonl` — dev 查询（本地，不入 Git）
- `frozen_test_queries.private.jsonl` — frozen test 查询（本地，不入 Git）
- `merge_positive_pairs.private.jsonl` — 应合并的 pair（本地）
- `hard_negative_pairs.private.jsonl` — 不应合并的 pair（本地）
- `synthetic_cases.jsonl` — synthetic test cases（入 Git，供 CI 使用）
- `holdout_15_02.synthetic.jsonl` — Phase 15-02 独立 holdout（google / paraphrase / no_answer / privacy；**不改 frozen**）
- `comprehensive_v1.synthetic.jsonl` — Phase 17 CI 全场景壳（150 cases；无私人正文）
- `eval_v1.yaml` / `eval_policy_v1.yaml` — 评测 manifest 与 promote gate 策略
- `answer_rubric_v1.md` — 回答评分 rubric
- 私有 full suite：`var/runtime/private_evals/comprehensive_v1.private.jsonl`（gitignore）

### Phase 17 单入口

```powershell
python src/personal_knowledge/evaluation/run_knowledge_eval.py --config assets/evals/knowledge_units/eval_v1.yaml --full --render --gate --dry-run
python src/personal_knowledge/evaluation/run_knowledge_eval.py --config assets/evals/knowledge_units/eval_v1.yaml --retrieval-only --render --gate
python src/personal_knowledge/evaluation/reconcile_l2_lineage.py --check
python -m pytest -q tests/test_knowledge_eval_*.py
```

报告：`var/reports/analysis/evaluations/<run>/report.html`（禁止写 Desktop）

### Holdout 15-02

与 frozen 分离：frozen 仍是 gold-evidence 回归；holdout 测泛化与隐私边界。

```powershell
python tools/forensics/phase15_02_holdout_eval.py
python tools/forensics/phase15_02_holdout_eval.py --offline-smoke
```

报告：`integration/analysis/ai_context/phase15_02_holdout_eval.json`

## 泄漏检查

按 subject/source/time group 检查 dev 与 frozen_test 之间无泄漏。frozen test 一旦确认不再由 pipeline 自动修改。

## 重建方法

```powershell
# 从 canonical store 重建真实 datasets
python -m personal_knowledge.evaluation.build_private_suite --write
```
