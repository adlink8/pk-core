# Memory Decomplexity and Deletion Plan

- phase: 08
- wave: 5
- scope: plan only
- generated_at: 2026-07-01T15:40:00+08:00

## Position

本阶段只生成去复杂化/删除计划，不删除、不改名、不禁用任何现有文件或入口，不修改数据库。

去复杂化的目的，是把旧规则记忆机制和新对话图谱机制合并到一条更小、更清楚的 pipeline；不是因为某条旧记忆结果输给某条新图谱结果。

## Phase 09 Addendum

Phase 09 已把 active candidate pipeline 收敛到下面这条链：

```text
script coarse recall
-> LLM graph candidate proposal
-> deterministic evidence gate
-> LLM judgment
-> accepted graph edges
-> memory_evidence_bundles
-> LLM memory candidate extraction
-> weighted promotion gate
-> human review / auto-approved apply
```

- 旧的 `legacy_evidence_candidate` 现在只允许出现在历史说明、迁移说明或测试断言里，不再出现在 active generation path。
- 结构化 evidence 不能直接进入 `memory_promotion_candidates`，必须先进入 `memory_evidence_bundles`，再由 LLM extraction 生成 `llm_memory_candidate`。
- `memory_items` / `memory_links` / `memory_relations` 仍是长期兼容查询面，但只作为 duplicate/conflict check target，不再作为新 promotion candidate 的来源。
- 无 live LLM 时，`graph candidate proposal` / `memory candidate extraction` / `repair loop` 都是 blocked audit path，不伪造候选。

## Protected Surfaces

以下对象本阶段不能删除，后续阶段也必须先迁移 reader 再处理：

| Surface | Why protected |
| --- | --- |
| `integration/scripts/run_pipeline.py` | 仍引用旧 memory writers、`build_memory_graph.py`、`build_vector_store.py`、profile 生成和可选 `conversation_turns` 回流。 |
| `integration/scripts/unified_search.py` | 当前消费 `memory_items` / `memory_links` / `memory_relations` / `personal_events` / `conversation_turns`，并支撑 CLI / REST / MCP 契约测试。 |
| `memory_items` / `memory_links` / `memory_relations` | 仍是长期记忆兼容层，不等于最终权威，但目前是公开查询面。 |
| Phase 08 新机制脚本 | `audit_memory_experiments.py`、`analyze_memory_mechanisms.py`、`build_memory_promotion_candidates.py`、`evaluate_memory_promotion_candidates.py`、`apply_memory_promotions.py` 是目标机制的一部分。 |
| Phase 09 新机制脚本 | `build_graph_relation_candidates_v2.py`、`build_memory_evidence_bundles.py`、`extract_memory_candidates_from_bundles.py`、`repair_memory_promotion_candidates.py` 负责 proposal / bundle / repair 审计链。 |
| `conversation_turns` / `graph_relation_*` | 当前对话压缩、向量召回、LLM 判边、evidence gate 的审计链。 |

## Classification Counts

### Scripts

| Category | Count |
| --- | ---: |
| keep | 24 |
| keep_but_rename | 7 |
| deprecated | 1 |
| archive_only | 3 |
| remove_candidate | 0 |

### Key Artifacts

| Category | Count |
| --- | ---: |
| keep | 30 |
| keep_but_rename | 2 |
| deprecated | 0 |
| archive_only | 5 |
| remove_candidate | 1 |

## Script Classification

| Category | Scripts |
| --- | --- |
| keep | `run_pipeline.py`, `build_vector_store.py`, `build_context_doc.py`, `build_profile_from_memory.py`, `search_vectors.py`, `unified_search.py`, `evaluate_memory_depth.py`, `mine_deep_memory_graph.py`, `build_deep_memory_profile.py`, `memory_governance.py`, `audit_memory_experiments.py`, `analyze_memory_mechanisms.py`, `build_memory_promotion_candidates.py`, `evaluate_memory_promotion_candidates.py`, `apply_memory_promotions.py`, `build_conversation_summary.py`, `build_conversation_vector_store.py`, `evaluate_vector_collections.py`, `evaluate_vector_retrieval.py`, `build_graph_relation_candidates.py`, `judge_graph_relations.py`, `evaluate_graph_relation_judgments.py`, `build_conversation_graph.py`, `query_conversation_graph.py` |
| keep_but_rename | `build_memory_store.py`, `build_capability_memory.py`, `build_context_memory.py`, `build_preference_memory.py`, `visualize_conversation_graph.py`, `build_triple_store.py`, `query_graph.py` |
| deprecated | `build_memory_graph.py` |
| archive_only | `build_conversation_segments.py`, `build_mem0_candidate_memory.py`, `compare_memory_experiments.py` |
| remove_candidate | None |

## Artifact Classification

| Category | Artifacts |
| --- | --- |
| keep | `memory_items`, `memory_links`, `memory_relations`, `personal_events`, `conversation_turns`, `conversation_sessions`, `conversation_turns_summary`, `graph_relation_candidates`, `graph_relation_judgments`, `g_turn/g_session/g_topic/g_tool/e_relation`, `integration/db/conversation_graph.duckdb`, `conversation_summaries.json/md`, `conversation_quality_report.json/md`, `vector_collection_health.json/md`, `vector_retrieval_eval_report.json/md`, `graph_relation_candidates_report.json`, `graph_relation_judgments_report.json`, `graph_relation_eval_report.json/md`, `memory_promotion_candidates`, `memory_promotion_candidates_preview.json/md`, `memory_promotion_report.json/md`, `memory_mechanism_matrix.json/md`, `memory_pipeline_target_design.md`, `memory_experiment_inventory.json/md`, `person_profile.md`, `person_profile_v2.md`, `memory_depth_readiness.md`, `deep_memory_mining.json/md`, `deep_memory_insights.json/md`, `deep_memory_profile.md` |
| keep_but_rename | `memory_graph.html`, `conversation_graph.html` |
| archive_only | `mem0_candidate_memories.json`, `mem0_candidate_evaluation.md`, `conversation_segments.json`, `prompt_eval_results.json`, `memory_experiment_comparison.json/md` |
| remove_candidate | `graph_relation_review_queue` |

## Priority Candidates

### 1. Old pseudo graph path in `build_triple_store.py`

- category: remove_candidate, but only for the embedded `duckdb_pseudo_graph_path` component.
- current_owner: `build_triple_store.py`.
- current_readers: none found for the disabled pseudo graph component.
- replacement_path: `build_conversation_graph.py --write`, `query_conversation_graph.py --smoke`, `visualize_conversation_graph.py`.
- why_safe_or_not_safe: the component already raises and points to `build_conversation_graph.py`; remove only after checking no `--only duckdb` callers remain.
- risk: low.
- required_pre_delete_checks: `rg "build_triple_store.py.*duckdb|--only duckdb" README.md integration tests .gsd`; dry-run SQLite path; rerun conversation graph checks in the later deletion phase.
- reversible_path: recover from git history; no live data should depend on the pseudo graph path.

### 2. `graph_relation_review_queue`

- category: remove_candidate.
- current_owner: `evaluate_graph_relation_judgments.py`.
- current_readers: none in inventory.
- replacement_path: `graph_relation_eval_report.json/md` plus `graph_relation_judgments.gate_status`.
- why_safe_or_not_safe: safe candidate because it has no automated reader, but not safe to drop until the writer stops creating it.
- risk: medium.
- required_pre_delete_checks: search all refs; prove the eval report has every review field; update writer in a later task; run graph judgment tests.
- reversible_path: recreate from `graph_relation_judgments` joined with `graph_relation_candidates` where `gate_status='review'`.

### 3. mem0 candidate path

- category: archive_only for `build_conversation_segments.py`, `build_mem0_candidate_memory.py`, `conversation_segments.json`, `mem0_candidate_memories.json`, `mem0_candidate_evaluation.md`.
- current_owner: `build_conversation_segments.py`, `build_mem0_candidate_memory.py`.
- current_readers: README and `tests/test_agent_conversation_normalization.py`.
- replacement_path: `conversation_summaries.json` -> `conversation_turns` -> `build_memory_promotion_candidates.py` -> `memory_promotion_report.json/md`.
- why_safe_or_not_safe: not target pipeline input; not safe to remove until tests and README move to archive wording.
- risk: medium for scripts, low for outputs.
- required_pre_delete_checks: rewrite tests away from mem0 imports; remove README active commands; confirm promotion candidates all have evidence/source refs.
- reversible_path: keep audit copy or regenerate with existing scripts while still present.

### 4. old result-comparison path

- category: archive_only for `compare_memory_experiments.py` and `memory_experiment_comparison.json/md`.
- current_owner: `compare_memory_experiments.py`.
- current_readers: old comparison test and Wave 2 matrix as counterexample reference.
- replacement_path: `analyze_memory_mechanisms.py`, `memory_mechanism_matrix.json/md`, `memory_pipeline_target_design.md`.
- why_safe_or_not_safe: its old item-vs-edge framing is explicitly superseded; not safe to remove until tests/docs stop referencing it.
- risk: medium for script, low for artifacts.
- required_pre_delete_checks: retire or rewrite `tests/test_memory_experiment_comparison.py`; confirm matrix states old comparison is not governing output.
- reversible_path: recover from git history or rerun while archived.

### 5. `build_memory_graph.py`

- category: deprecated, not remove_candidate.
- current_owner: `build_memory_graph.py`, `run_pipeline.py` step 9.
- current_readers: `run_pipeline.py`, `build_profile_from_memory.py`, `evaluate_memory_depth.py`, `mine_deep_memory_graph.py`, `query_graph.py`, `unified_search.py`, README.
- replacement_path: reviewed promotion flow writes/maintains long-term relations only after evidence and human/approval gates.
- why_safe_or_not_safe: not safe to remove because active readers depend on `memory_relations`; safe only to mark as deprecated because rule-only semantic judgment is no longer the target mechanism.
- risk: high.
- required_pre_delete_checks: migrate `run_pipeline.py` step 9, `unified_search` neighbors, profile/deep-memory readers, README, and memory contract tests.
- reversible_path: keep script and regenerate `memory_relations` if migration fails.

## Graph Visualization and Report Paths

`query_graph.py` and `visualize_conversation_graph.py` are not direct duplicates:

- `query_graph.py` reads the legacy `memory_items` / `memory_relations` graph.
- `visualize_conversation_graph.py` reads accepted conversation edges from `conversation_graph.duckdb`.

The decomplexity issue is naming, not correctness. Later rename targets:

- `query_graph.py` -> `query_legacy_memory_graph.py`
- `memory_graph.html` -> `legacy_memory_graph.html`
- `conversation_graph.html` -> `conversation_graph_accepted_edges.html`

No rename is performed in this wave.

## Pre-delete Order

1. Keep `run_pipeline.py`, `unified_search.py`, current tables, and Phase 08 promotion scripts unchanged.
2. Remove only dead pseudo DuckDB graph code from `build_triple_store.py` in a later explicit task.
3. Replace `graph_relation_review_queue` with report/status reads, then drop the table in a separate migration.
4. Move mem0 and old comparison paths to archive docs after tests are rewritten.
5. Only after promotion flow owns long-term writes, decide whether legacy rule memory scripts are renamed compatibility writers or replaced by reviewed promotion outputs.

## Decision Rule

Any object with an active reader is never marked as direct remove. It is either `keep`, `keep_but_rename`, `deprecated`, or `archive_only` with migration checks.
