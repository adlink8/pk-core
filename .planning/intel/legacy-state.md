---
project_name: "个人数据分析项目"
current_milestone: "Phase 13-14 - Refactor, AgentView Integration, Training-style RAG"
current_phase: 14
current_wave: 4
current_task: "Phase 14 Wave 0-4 完成，Wave 3/5-6 待执行"
status: "in_progress"
last_updated: "2026-07-10T17:30:00+08:00"
blockers: []
metrics:
  total_phases: 15
  completed_phases: 13   # 01-13 完成
  active_phases: [8, 13.5, 14]
  llm_model: "claude-fable-5"
  phase14_target_model: "gpt-5.6-luna"
  quality_normal_rate: 1.00
---

# 项目状态

## 阶段进度

| Phase | 名称 | 状态 | 说明 |
|-------|------|------|------|
| 01 | incremental_import_pipeline | ✅ 完成 | 增量导入流水线 |
| 02 | agent_data_ingestion | ✅ 完成 | Agent 数据入库 |
| 03 | integrated_architecture | ✅ 完成 | 统合架构 |
| 04 | memory_layer_upgrade | ✅ 完成 | 记忆层升级 |
| 05 | ponytail_project_optimization | ✅ 完成 | Ponytail 项目优化 |
| 05 | memory_layer_hardening | ✅ 完成 | 记忆层加固 |
| 06 | deep_memory_graph_mining | ✅ 完成 | 深度记忆图挖掘 |
| 07 | agent_conversation_normalization_mem0_spike | ✅ 完成 | 对话规范化（Wave 8 全通过）|
| 08 | memory_experiment_consolidation | 🔄 进行中 | 记忆实验汇总与去复杂化 |
| 09 | llm_semantic_candidate_pipeline | ✅ 完成 | LLM 语义候选生成 |
| 10 | llm_memory_relation_graph | ✅ 完成 | LLM 记忆关系图 |
| 11 | openai_mcp_apps_sdk_widget | ✅ 完成 | OpenAI MCP + Apps SDK |
| 12 | data_access_interfaces | ✅ 完成 | 数据访问接口层 |
| **13** | **codebase_refactoring** | **✅ 完成** | 代码库基础层重构（Wave 4-5 verified 2026-07-10）|
| **13.5** | **agentsview_session_integration** | **✅ 完成（含收尾）** | AgentView 安全快照、legacy 去重、canonical conversation store、消费者迁移、canonical 激活、回滚演练、GSD 文档收口 |
| **14** | **knowledge_unit_layer** | **🔄 Wave 0-4 完成 / Wave 3,5-6 待执行** | Training-style RAG（eval + schema + extraction + baseline + candidate index + A/B gate PASS，Recall@5 0.50→0.85）|

## Phase 13 执行状态（2026-07-05）

### 已完成

- Wave 1：扫描报告生成（`integration/analysis/refactoring/phase13_verification.md`）
- Wave 2：`integration/scripts/core/project_paths.py` + `core/__init__.py` 创建
- Wave 3：`build_integrated_system.py` 全量迁移（10 个重复定义删除，ROOT 路径修复）
- Wave 3（附）：`build_deep_profiles.py` 本地 `norm()` 删除，改为 `from common import norm`

### 待执行

- Wave 4：批量迁移其余 ~11 个脚本（`build_memory_store.py` 等）
- Wave 5：全链路验证（`pytest tests/test_memory_contracts.py` + `run_pipeline.py --dry-run`）

## Phase 13.5 规划（2026-07-10 新建）

新上游：`$HOME\.agentsview\sessions.db`。该库是正在写入的 WAL SQLite，约 513 MB，包含多 Agent 会话、消息、工具和父子/subagent 关系。

目标：

```text
AgentView live DB（只读快照） + legacy agent_data.sqlite
  → privacy-safe agentsview_normalized.sqlite
  → lineage dedup / source crosswalk
  → agent_conversations.sqlite（下游唯一 canonical conversation 入口）
  → shadow parity / cutover / rollback
```

硬约束：不导入 thinking、PII、tool input/result 明文；secret/excluded/deleted session 正文不可进入摘要、向量或 knowledge unit。

## Phase 13.5 执行状态（2026-07-10）

### 已完成（Wave 1-2）

- **Wave 1.1 — AgentView source adapter**：`integration/scripts/source_adapters/agentsview.py`
  - 只读连接（`mode=ro` + `PRAGMA query_only=ON`），SQLite backup API 一致快照
  - schema gate：7 张 required 表 + 关键列校验，缺表/缺列/integrity≠ok 时 pre-flight abort
  - 测试：`tests/test_agentsview_source_adapter.py`（6 passed）
- **Wave 1.2 — import inventory**：`Agent/structured/scripts/import_agentsview_sessions.py`
  - 默认 `--dry-run`，产 privacy-safe inventory 报告（JSON + MD）
  - pre-flight gate：integrity / 外键孤儿 / ordinal 重复
  - 真实库基线：620 sessions、57731 messages、35680 tool_calls、3 secret sessions、403 legacy hash 重叠
  - 报告：`integration/analysis/ai_context/agentsview_import_inventory.{json,md}`
- **Wave 2.1/2.2 — normalized snapshot**：`integration/scripts/build_agentsview_normalized.py`
  - 6 表 normalized schema（import_runs/sessions/messages/tool_events/usage_events/source_tombstones）
  - 字段白名单：thinking_text/input_json/result_content/邮箱 永不复制
  - secret session 正文完全不写；本地二次 secret 扫描（openai-key/google-key/bearer/github-pat/aws/private-key/email）命中时正文隔离，只记规则名
  - system/sidechain/subagent evidence_scope 标记；tombstone 传播
  - staging + `os.replace` 原子发布；幂等 dataset_hash
  - 测试：`tests/test_agentsview_normalization.py`（10 passed）
  - 真实库 dry-run：gate_passed=True，local 扫描命中 77 邮箱 / 26 bearer / 4 openai-key（正文均未落库）

### 待执行（Wave 3-5）

- ~~Wave 3：legacy `agent_data.sqlite` 去重 + canonical conversation store 发布~~ ✅
- ~~Wave 4：下游 shadow read + parity + cutover~~ ✅
- ~~Wave 5：pipeline 接入 + fallback/rollback~~ ✅

### Wave 3-5 执行结果（2026-07-10）

**Wave 3 — Canonical conversation store**：`build_canonical_agent_conversations.py`
- file_hash 精确匹配 crosswalk：legacy `source_files.sha256` ↔ AgentView `sessions.file_hash`（路径后缀匹配，避免 basename 碰撞）
- legacy session_id 去重（831 行 → 281 distinct）
- 623 canonical sessions（278 merged + 342 AV-only + 3 legacy-only）
- merged session 的 AV 空壳时 fallback 到 legacy message（ineligible session 除外，隐私 gate）
- 6 表 canonical schema + lineage 表 + session_relations
- 0 duplicate links, 0 duplicate (csid,ordinal,source)
- 测试：6 passed

**Wave 4 — Repository + cutover**：`conversation_repository.py` + `evaluate_agent_conversation_cutover.py`
- 统一会话查询收口，显式 legacy|canonical 模式，禁止静默双计数
- tool output 默认 `[tool output omitted]`
- canonical turn 携带 source_ref + source_session_ref
- cutover parity：277/278 match（99.64%），1 个解析差异（AV/legacy 粒度不同）
- secret searchable = 0，AV-only 全有 lineage，canonical 覆盖 ≥ legacy
- **GATE: PASS**
- 测试：9 + 6 = 15 passed

**Wave 5 — Pipeline 接入 + rollback**：`run_pipeline.py --agentsview` + `rollback_agent_conversation_source.py`
- pipeline 新增 `--agentsview [--agentsview-write]` 可选前置阶段（snapshot → normalized → canonical 串行）
- `build_agentsview_normalized.py` 新增 CLI 入口
- rollback：`--to legacy|canonical`、`--to-backup <name>`、`--list-backups`，默认 dry-run
- source 指针文件 `integration/db/conversation_source.txt`，原子切换 + JSONL 操作日志
- 固定流程测试：shadow → promote → rollback → legacy → restore
- 测试：6 passed

**Phase 13.5 总计**：43 测试全通过，源库 mtime 全程未变（只读约束验证通过）。

## Phase 14 规划（2026-07-10 修订）

理论框架：**RAG = 对个人历史的二次训练**。向量库是检索特征空间，不是存储桶。

目标：在 Phase 13.5 的 canonical conversation evidence 与向量库之间建立 evaluation-first 知识蒸馏闭环：

```
canonical conversations + unified_events
  → eligible evidence bundles
  → versioned extraction staging
  → knowledge_units / canonical_knowledge_units
  → candidate Chroma collection
  → frozen-test A/B + atomic promote
  → unified search + canary + feedback + incremental refresh
```

Wave 顺序：Wave 0（基线与 frozen test）→ Wave 1（schema/checkpoint）→ Wave 2（严格提炼）→ Wave 3（hard-negative canonical）→ Wave 4（candidate index/A-B）→ Wave 5（接口/canary）→ Wave 6（增量与生命周期）。

## Phase 14 执行状态（2026-07-10）

### 已完成（Wave 0-2）

- **Wave 0.1 — eval datasets**：20 dev + 20 frozen + 20 merge-positive + 20 hard-negative，泄漏检查 0。synthetic cases 覆盖 10 种场景。测试 8 passed。
- **Wave 0.2 — raw baseline**：deferred（Chroma HTTP 服务未运行，需要启动后记录 baseline）
- **Wave 1.1 — schema 迁移**：`migrate_add_knowledge_unit_tables.py` 在 `personal_system.sqlite` 新增 6 表（knowledge_build_runs/knowledge_units/knowledge_unit_evidence/canonical_knowledge_units/canonical_unit_members/knowledge_index_versions），CHECK 约束 + UNIQUE + 外键。pydantic 加入 requirements.txt。测试 6 passed。
- **Wave 1.2 — run manifest + staging**：`knowledge_unit_pipeline.py`（RunManifest + StagingPublisher），staging → gate → promote / abort，checkpoint rollback，table reconciliation。幂等重跑安全（begin_staging 清旧 units）。测试 7 passed。
- **Wave 2.1 — extraction**：`build_knowledge_units.py` + `prompts/knowledge_unit_extractor/v1_main.md` + Pydantic schema（extra=forbid）。Vertex AI Gemini 3.5 Flash（gcloud token，无限流）。system-reminder 预处理剥离。JSON 清洗（括号深度匹配）。测试 14 passed。
- **Wave 2.2 — extraction gate**：真实 20 条 evidence → 24 knowledge units（gate PASS，schema 100%，evidence 24/24，0 无证据）。类型：personal_fact 12 / project_decision 4 / preference 4 / capability 3 / tool_usage 1。frozen test gold evidence 补充抽取 20 条 → 33 units。
- **Wave 0.2 — raw baseline**（Chroma 启动后补做）：`personal_events` collection (10,996 items)，Recall@5=0.50，MRR@5=0.4625，p95=17.7ms。
- **Wave 4.1 — candidate vector store**：`build_knowledge_unit_vector_store.py`，33 units → Chroma `knowledge_units_a89ebe470357`，exact reconcile gate PASS（missing=0, orphan=0, duplicate=0）。
- **Wave 4.2 — frozen-test A/B + promote**：`evaluate_knowledge_unit_rag.py` + `promote_knowledge_index.py` + `rollback_knowledge_checkpoint.py`。
  - **Candidate Recall@5=0.85 vs baseline 0.50（+35），MRR@5=0.7392 vs 0.4625（+27.7），deprecated/secret hit=0，p95=15.4ms**
  - Launch gate PASS；promote/rollback/re-promote 全流程验证通过
  - active pointer: `knowledge_units_a89ebe470357`
  - 测试 5 passed

### 待执行（Wave 3-6）

- Wave 3：canonicalization（候选聚类 + merge proposal + hard-negative gate）
- Wave 4：candidate Chroma index + frozen-test A/B + atomic promote
- Wave 5：retrieval interface + canary + feedback
- Wave 6：incremental refresh + lifecycle

### LLM 预测试结果（55 条样本）

- Vertex AI 稳定（0 429 限流），schema 54/54 有效
- 系统注入过滤：预处理层剥离 + LLM prompt 规则双保险
- evidence 支撑：关键词片段匹配（≥10 字连续片段），24/24 通过
- 抽取质量：preference/project_decision/personal_fact/capability/tool_usage 全覆盖

## 规范说明

- `.gsd/phases/NN_name/` — 阶段计划（PLAN/CONTEXT/CONTEXT）
- `.planning/codebase/` — 架构文档（STACK/ARCHITECTURE/CONVENTIONS 等）
- `.planning/STATE.md` — 本文件，跨会话状态追踪

历史 Phase 07 细节已从本权威状态文件移除，避免恢复流程误读旧状态；执行记录保留在 `.gsd/phases/07_agent_conversation_normalization_mem0_spike/`。

本项目继续使用混合 GSD 结构：`.gsd/phases/` 存阶段产物，`.planning/codebase/` 存架构映射，`STATE.md` 只保留一个当前权威状态区。顶层 `PROJECT.md/ROADMAP.md/REQUIREMENTS.md` 暂不补建，避免与既有结构冲突。
