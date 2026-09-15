# Codebase Concerns

**Analysis Date:** 2026-08-08

**Focus:** Over-Coupling（过度耦合专项扫描）

扫描范围：`src/personal_knowledge/`（D/S/R/A 数据管线 + REST/MCP 服务）、`apps/personal_decision_cockpit/src/`（前端）、`tools/`、`ops/`、`integration/`。跳过 `data/`、`archive/`、`docs/`、`var/`、`.env*`。

优先级：**High** = 必须拆分；**Medium** = 建议拆分；**Low** = 可暂缓。

---

## 过度耦合（Over-Coupling）

### OC-1 上帝文件：`src/personal_knowledge/services/api_server.py`（1233 行）【High】

**证据**
- 单个 `Handler(BaseHTTPRequestHandler)` 类横跨第 511–1233 行（约 720 行）：`do_GET`（第 564–1048 行，约 485 行）内联路由 60+ 个端点，`do_POST`（第 1050–1180 行）另含写路由。
- 第 97–139 行同时 import：`retrieval.unified_search as backend`（数据检索）、4 个 intelligence 服务（`IntelligenceService` / `DecisionFeedbackService` / `ProactiveIntelligenceService`，第 100–102 行）、10+ 个 services 层模块（`decision_intelligence_reads` / `orchestration_service` / `pi_domain_gateway` / `warehouse_mutations` / `semantic_maintenance_tools` / `retrieval_maintenance_tools` / `snapshot_release_tools` / `pi_runtime_projection` / `pi_operation_projection` / `agent_contract` / `ui_projection` / `topic_projection`，第 103–136 行）、`wiki.materialization.WikiMaterializer`（第 139 行）。
- 同一文件混合职责：静态文件托管（`/app` 静态资源，第 591–613 行）、健康检查（第 615 行）、知识索引状态（第 835 行）、Google assertions（第 840 行）、data CRUD/聚合/导出/时间线（第 869–968 行）、记忆图谱（第 978–1012 行）、评审台页面装配（第 637 行）、以及 7 个 `*_rest_contract` 适配函数（第 422–509 行）。

**耦合表现**：HTTP 协议层（路由、参数解析、错误封包）与 7 个业务域（intelligence / decision / proactive / agent-read / orchestration / ui-projection / topic / wiki / data）的适配逻辑全部堆在一个类里。任何新端点都修改这 485 行的方法体；任何服务契约变化都同时触碰 api_server 与对应 service。

**影响**：改动扩散——新增一个域接口需要同时改 api_server、对应 service、前端 hooks、schemas.ts 四处；该文件成为「每个人都要碰」的合并冲突热点；单函数 485 行无法单测路由分支。

**建议拆分**
- 拆出一个纯路由表模块（`src/personal_knowledge/services/routes.py`）：path → (method, handler_fn) 映射，`Handler` 只做 HTTP 编解码与分派。
- 每个业务域拆独立 handler 模块：`src/personal_knowledge/services/http/handlers/{intelligence,decision,proactive,agent,orchestration,ui,topic,data,wiki,kernel}.py`，各自持有 rest_contract 与参数映射。
- `/app` 静态托管、`/health`、`/stats` 拆到 `http/handlers/meta.py`。

---

### OC-2 上帝文件：`src/personal_knowledge/services/mcp_server.py`（1263 行）【High】

**证据**
- `ALL_TOOLS` 工具 schema 列表占第 187–680 行（约 500 行 JSON Schema 内联）。
- `handle_call_tool` 巨型 if/elif 分派占第 1007–1243 行（约 240 行），内联 40+ 个工具的调用逻辑。
- 第 84 行 import `retrieval.unified_search as backend`，但唯一的语义检索工具 `search_semantic`（第 1021 行）走 `_search_semantic_via_api`（第 102–116 行）——通过 HTTP 循环回调用常驻 REST API，而其余约 30 个工具全部直连 `backend.*`。

**耦合表现**：同一文件三重复职混叠（tool schema 定义 + 5 个 `*_tool_contract` 适配器（第 891–999 行）+ 巨型分派）。更严重的是「同一服务两种调用路径」：`search_semantic` 依赖 REST 服务 :8000 存活，其余工具在进程内直连——MCP 与 REST 之间存在隐藏运行时耦合（MCP 挂掉伴随 REST 挂掉的假象，或 REST 挂了 MCP 的 search_semantic 单独失败）。

**影响**：MCP 与 REST 双表面各自维护一套分派/参数映射（见 OC-11）；`search_semantic` 的 HTTP 回环使 MCP 无法独立部署/测试，并产生同一请求两次序列化开销。

**建议拆分**
- 工具 schema 定义拆到 `src/personal_knowledge/mcp/tool_definitions.py`。
- 分派按域拆到 `src/personal_knowledge/mcp/handlers/{intelligence,decision,proactive,agent,orchestration,data}.py`。
- 删除 `_search_semantic_via_api`，`search_semantic` 与其他工具一致直连 `backend.search_knowledge_units`（api_server 第 1152 行已示范同款调用）。

---

### OC-3 上帝模块：`src/personal_knowledge/application/knowledge/refresh_knowledge_units.py`（2040 行）【High】

**证据**：单文件 25 个顶层函数承载 4 类不相关职责——
- 刷新编排：`refresh`（第 172 行）、`_build_incremental_pipeline_commands`（第 222 行）、`run`（第 295 行）
- delta 构建：`_materialize_delta_run`（第 497 行）、`prepare_delta`（第 681 行）、`execute_run`（第 809 行）、`build_incremental_candidate`（第 1005 行）、`prepare_production_delta`（第 1630 行）
- journal/watermark 账本持久化：`ensure_journal_schema`（第 1204 行）、`get_committed_watermark`（第 1215 行）、`advance_watermark`（第 1236 行）、`check_watermark_advance_preconditions`（第 1253 行）、`prepare_incremental_journal`（第 1332 行）、`commit_incremental_journal`（第 1394 行）、`rollback_incremental_journal`（第 1469 行）
- 测试与 CLI：`run_sandbox_ku08_e2e`（第 1522 行）、`main`（第 1906 行）

**耦合表现**：`pk-ku`（`src/personal_knowledge/application/ku.py` 第 504/524/907 行）直接依赖此文件的多个入口；watermark 账本是独立持久化子域（schema、读写、回滚），与 delta 构建算法之间只有弱数据关系，却共享同一文件。

**影响**：watermark/journal 任何表结构变更（`ensure_journal_schema`）与 delta 构建算法变更互相污染 git 历史；该文件任何编辑都可能踩到无关功能；无法单独为账本层写测试夹具。

**建议拆分**
- 账本层拆到 `src/personal_knowledge/application/knowledge/journal.py`（schema/watermark/commit/rollback，约第 1204–1520 行搬移）。
- delta 构建拆到 `src/personal_knowledge/application/knowledge/delta_build.py`。
- 保留 `refresh_knowledge_units.py` 仅做编排入口 + `main`。

---

### OC-4 上帝函数：`src/personal_knowledge/retrieval/semantic_search.py::search_knowledge_units`（第 472–942 行，约 470 行）【High】

**证据**：单函数内硬编码 6 层 fallback——
- knowledge_unit（第 638–711 行）→ canonical_messages（第 769–787 行）→ conversation_turns（第 790–820 行）→ non_dialogue_raw / personal_events（第 823–854 行）→ legacy_pad（第 857–886 行）→ legacy_personal_events（第 738–764 行）
- 同函数内还包含：serving snapshot 解析（第 530–552 行）、per-layer 遥测（第 520–528 行）、去重（第 714–736 行）、`annotate_candidate_support` 支持度注解（第 675/723 行）、EvidenceResolver 装配（第 617–622/904–932 行）、版本读取（第 688–703 行）。

**耦合表现**：fallback 策略（顺序、跳过条件、snapshot 绑定）不是可组合的策略对象，而是写死在 `if policy == "legacy" ... else ...` 分支里（第 738/766 行）。新增一层或调整某层行为必须改这个 470 行函数，且每层共用 `_append_unique` / `_remaining` / `_raw_event_item` 闭包——任何一层改动都可能影响其它层的去重与排序语义。

**影响**：R 层核心检索契约无法独立演进；fallback 顺序调优（评测驱动）每次都要触碰检索核心；测试需要为 6 层交互构造超大夹具。

**建议拆分**
- 抽象 `RetrieverLayer` 接口（`retrieve(query, remaining) -> list[item]`），每层一个类：`src/personal_knowledge/retrieval/layers/{knowledge_unit,canonical_messages,conversation_turns,non_dialogue_raw,legacy_pad}.py`。
- `search_knowledge_units` 退化为策略装配器：按 `fallback_policy` 组装层链 + 统一遥测/去重/证据解析。
- fallback 顺序策略与 snapshot 绑定规则拆到 `src/personal_knowledge/retrieval/fallback_policy.py`，供评测脚本复用。

---

### OC-5 上帝类：`src/personal_knowledge/services/ui_projection.py::CockpitProjectionService`（第 424–1658 行，约 1234 行）【High】

**证据**
- 单类承载 11 个 projection 操作：overview（第 495 行）、system_status（第 638 行）、personal_state（第 770 行）、external_delta（第 905 行）、decision_queue（第 1035 行）、decision_workspace（第 1102 行）、actions_recent（第 1190 行）、proactive_summary（第 1302 行）、calibration_overview（第 1399 行）、evidence_resolve（第 1474 行）。
- 第 48–60 行顶层 import 4 个 intelligence 服务 + `retrieval.unified_search.get_knowledge_status` + `DecisionIntelligenceReadService`。
- 类内还混入与 projection 无关的横切逻辑：端口探测 `_port_up`（第 192 行）、supervisor 状态读取 `_SUPERVISOR_STATE_PATH`（第 72 行）、schema 词表常量（第 74–110 行）、16 个模块级格式化辅助函数。

**耦合表现**：UI 投影层把 5 个权威数据源的读取、格式化、空态、authority 状态汇总全部内聚在一个类里；`invoke` 通过字符串方法名反射（第 444–448 行）分派，与 OC-1 的 api_server 字符串路由双重耦合。

**影响**：任何新 UI 区块都要加进这个 1200 行类；UI 投影的格式化规则与权威数据读取深度绑定，无法独立测试某个区块；`api_server.ui_rest_contract`（第 490 行）与 MCP 都直接依赖它。

**建议拆分**
- 每类 projection 拆独立模块：`src/personal_knowledge/services/projection/{overview,system_status,personal_state,external_delta,decision_queue,decision_workspace,actions_recent,proactive_summary,calibration_overview,evidence_resolve}.py`，各模块暴露 `build(db, read_service, params) -> dict`。
- `CockpitProjectionService` 退化为纯注册表（operation → builder），删除字符串反射。

---

### OC-6 上帝类 + 职责混叠：`src/personal_knowledge/services/topic_projection.py`（890 行）【Medium】

**证据**：单文件混入 5 类职责——envelope 序列化（`make_wiki_envelope` 第 130 行）、TopicKey 解析与转义（第 55–96 行）、错误码映射（第 177/428–435 行）、`_LatestCommittedPersonalReader` 读取适配器（第 310–376 行）、`TopicProjectionService` 本体（第 378–890 行）。`TopicProjectionService` 的 `invoke`（第 859 行）同属字符串反射分派（见 OC-5）。

**影响**：wiki 投影的 key 解析 / envelope 格式 / 读取适配器三部分各自可独立演进却被绑在同一文件；wiki 包（`wiki/read_router.py` 第 11 行、`wiki/materialization.py` 第 10 行）顶层 import 本文件的 `TopicKey` / `opaque_topic_id` / `make_wiki_envelope`，使 services 层成为 wiki 包的结构依赖。

**建议拆分**
- key 解析与 envelope 格式拆到 `src/personal_knowledge/wiki/topic_key.py` 与 `wiki/envelope.py`（wiki 包内，天然归属）；reader 适配器拆到 `services/topic_projection_readers.py`；`TopicProjectionService` 只留投影编排。

---

### OC-7 services ↔ wiki 包级双向依赖（隐藏环）【Medium】

**证据**
- 正向：`services/topic_projection.py` 第 403 行 lazy import `wiki.read_router.WikiReadRouter`、第 469 行 lazy import `wiki.derived_store.ProjectionDependency`；`services/api_server.py` 第 139 行顶层 import `wiki.materialization.WikiMaterializer`。
- 反向：`wiki/read_router.py` 第 11–16 行顶层 import `services.topic_projection`；`wiki/materialization.py` 第 10 行顶层 import `services.topic_projection.TopicKey, opaque_topic_id`。
- 包级 import 边：`services -> wiki (1)` 与 `wiki -> services (2)` 同时存在。

**耦合表现**：`services` 包（HTTP/MCP 服务层）与 `wiki` 包（wiki 投影物化层）互相依赖。当前靠 topic_projection 侧的函数级延迟 import 掩盖，未在加载期爆炸；一旦任一侧把延迟 import 提升为顶层 import（或重排导入顺序）即成硬循环。概念上 wiki 的「物化验证」与 services 的「投影适配」归属错位——materialization 与 read_router 实际是 R 层检索投影，却放在 services 里且反向依赖 services。

**影响**：隐藏环使两包无法独立测试/打包；新增 wiki 特性时极易把 import 提为顶层而触发 ImportError；IDE/静态检查无法准确建模依赖。

**建议拆分**
- 把 `TopicKey`、`opaque_topic_id`、`make_wiki_envelope`、`WIKI_REASON_CODES` 下沉到 `src/personal_knowledge/wiki/topic_key.py`（wiki 包），read_router/materialization 改从 wiki 包内引用（消除对 services 的反向依赖）。
- `TopicProjectionService` 保留在 services 但对 wiki 只依赖 `wiki.derived_store`/`wiki.materialization` 的公开接口，方向收敛为 `services -> wiki` 单向。

---

### OC-8 R 层向上依赖 serving 快照（application↔retrieval 双向纠缠）【Medium】

**证据**
- 反向（R → A）：`src/personal_knowledge/retrieval/serving.py:8` `from personal_knowledge.application.serving.snapshots import get_active_snapshot`——R 层 serving 快照解析依赖 A 层 application 包的 serving 子包。
- 正向（S → R）：`application/knowledge/refresh_knowledge_units.py:1000` 与 `application/knowledge/doctor_ku.py:287` lazy import `retrieval.serving.ServingSnapshotResolver`；`application/knowledge/lifecycle_events.py:18` 顶层 import `retrieval.evidence.EvidenceResolver`。
- 包级 import 边：`retrieval -> application (1)` 与 `application -> retrieval (1)` 同时存在（当前唯一 R→A 边）。

**耦合表现**：「serving snapshot 解析」逻辑被拆在两包——解析入口在 `retrieval/serving.py`，快照权威读取在 `application/serving/snapshots.py`，两者互相引用（application 生产端读 snapshot 时要调 retrieval 的 resolver，retrieval 解析时又调 application 的 getter）。D/S/R/A 分层中 R 层不应依赖 A 层包结构。

**影响**：serving 快照契约变更同时波及 R 与 A 两侧；`application.knowledge` 构建流程与 `retrieval.serving` 形成加载期纠缠（`application.knowledge → retrieval.serving → application.serving`），新增快照字段需在两包同步修改。

**建议拆分**
- 把 `retrieval/serving.py::ServingSnapshotResolver` 与其依赖的 snapshot 读取合并到单一归属，如 `src/personal_knowledge/application/serving/resolver.py`（A 层 serving 子域），retrieval 侧仅保留薄引用或通过注入依赖。
- `EvidenceResolver`（`retrieval/evidence.py`）被 `application`、`intelligence`、`services` 三处引用（见 OC-9/OC-10），属于共享基础设施，应下沉到 `src/personal_knowledge/core/evidence.py` 或以「读取适配器」接口注入，消除 S/A 对 R 内部实现的直接 import。

---

### OC-9 `retrieval/memory.py` 隐藏依赖 `domains.graph.query_graph`（R 层越层 + 潜在断裂）【Medium】

**证据**：`src/personal_knowledge/retrieval/memory.py`（927 行）第 638、707、802 行三处函数内 `import personal_knowledge.domains.graph.query_graph as query_graph` 并调用 `find_node_by_subject`（第 642 行）。而 `domains/graph/query_graph.py` 是纯 re-export 门面（`import_module` + `sys.modules` 重绑定，见文件头 docstring），指向 `application/graph/query_graph.py`，且 docstring 声明该门面于 **2026-08-13** 清理窗口删除。

**耦合表现**：R 层（记忆检索）为做子图切片，直接抓取 S/A 层的图查询实现；且依赖一个即将删除的门面模块。三处重复 `import` 散落在不同函数内。

**影响**：2026-08-13 门面删除后该三处 import 直接 ImportError（记忆图谱检索、`/memory/graph`、`/memory/<subject>?neighbors=` 接口全挂）；R 层对图查询的依赖未经接口隔离，无法注入测试替身。

**建议拆分**
- 三处重复 import 收敛为 `retrieval/memory.py` 顶部一次：`from personal_knowledge.application.graph.query_graph import find_node_by_subject`（或先建 `retrieval/_graph_query.py` 薄适配层）。
- 更彻底：将「按 subject 取 N 跳子图」抽象为 `retrieval/memory_graph.py` 的公开函数，图实现通过参数注入，R 层不再直接感知 application 内部。

---

### OC-10 application ↔ intelligence ↔ evaluation ↔ external_context 包级双向依赖【Medium】

**证据**
- application → intelligence：`application/knowledge/build_knowledge_units.py:53`、`build_knowledge_units_prod.py:84` 顶层 import `intelligence.analysis.providers`（LLM provider 中枢，649 行，含 `OpenAICompatibleProvider` / `CodexCliProvider` 等 7 类）。
- intelligence → application：`intelligence/cli.py:214` lazy import `application.knowledge.lifecycle_events.lifecycle_status`；`intelligence/decision/cli.py:13-14` import `application.knowledge.lifecycle_events.ensure_lifecycle_schema` 与 `migrate_add_knowledge_unit_tables.SCHEMA_SQL`。
- application → evaluation：`application/knowledge/promote_knowledge_index.py:400` lazy import `evaluation.gate_knowledge_candidate.evaluate_gate, load_policy`；`application/knowledge/build_pilot_report.py:25`、`application/ku.py:669/684`。
- external_context ↔ intelligence：`external_context/doctor.py:13` import `intelligence.decision.context_binding`；`intelligence/decision/context_binding.py:11-16` 反向 import `external_context.{schema,service,snapshots}`。
- 包级 import 边：`application->intelligence(2)`+`intelligence->application(2)`；`application->evaluation(2)`+`evaluation->application(2)`；`external_context->intelligence(1)`+`intelligence->external_context(4)`。

**耦合表现**：S 层知识构建把「LLM provider 选择」放在 A 层 `intelligence/analysis/providers.py`，而 A 层 CLI 又依赖 S 层的 schema DDL（`SCHEMA_SQL` 的归属在 `migrate_add_knowledge_unit_tables.py`）。产品 promote 路径依赖 A 层评测 gate。四包两两互相引用。

**影响**：provider 接口变更影响 `build_knowledge_units*.py` 与 `intelligence/analysis` 两处；`SCHEMA_SQL` DDL 所有权不清，schema 迁移必须跨包对齐；`promote_knowledge_index`（S 层产品路径）与 `evaluation` gate（A 层）的耦合使「评审门禁」无法脱离产品路径独立演进。

**建议拆分**
- LLM provider 中枢下沉为独立包 `src/personal_knowledge/core/providers/`（或 `intelligence/llm_providers.py`），S 层构建只依赖该包公开接口。
- `SCHEMA_SQL` 与 `ensure_lifecycle_schema` 迁到 `src/personal_knowledge/application/knowledge/schema_ddl.py`，intelligence CLI 改为引用该规范路径（消除 intelligence→application 的 DDL 依赖）。
- gate 策略（`evaluation/gate_knowledge_candidate.py`）保持独立，`promote_knowledge_index` 通过注入的 gate 回调而非直接 import 使用。

---

### OC-11 REST/MCP 双表面契约重复实现（dual-surface duplication）【Medium】

**证据**
- `api_server.py` 定义 7 个 `*_rest_contract`（第 422–509 行）：`intelligence/decision/proactive/agent_read/orchestration/ui/topic_rest_contract`。
- `mcp_server.py` 定义 5 个 `*_tool_contract`（第 891–999 行）：`intelligence/decision/proactive/agent_read/orchestration_tool_contract`，对同一批 service 重复做参数映射（如 `orchestration_tool_contract` 第 973–988 行对 operation 名称的 if/elif 映射与 `orchestration_rest_contract` 重复）。
- 两文件各自独立 import 相同的 10+ 个 service 模块（api_server 第 97–139 行、mcp_server 第 84–99 行）。

**耦合表现**：同一 service 契约存在两套平行适配代码，参数默认值/错误码/字段名在两处手工保持一致。`orchestration` 契约（`agent_session_prepare/confirm/preview/execute` 映射）已出现两处 `route_operation_mismatch` 判定（api_server 第 1124 行、mcp_server 第 982 行）——同一逻辑两处维护。

**影响**：契约字段增删需同步改两个文件；历史上已出现 MCP 与 REST 行为漂移风险（如 `search_semantic` 走 HTTP 回环而 REST 直连，见 OC-2）；双表面测试各自独立，缺一处共享契约测试。

**建议拆分**
- 抽出共享契约层：`src/personal_knowledge/services/contracts.py`（每域一个 `invoke(operation, params, db_path)` 纯函数，含参数默认值、错误码、`route_operation_mismatch` 判定）。
- REST 与 MCP 只保留表面适配（HTTP 编解码 / JSON-Schema 声明），内部统一调 contract 层。

---

### OC-12 前端：API 层混入业务规则，schema 单文件膨胀【Low】

**证据**
- `apps/personal_decision_cockpit/src/api/orchestration.ts`（356 行）持有客户端状态机业务规则：`TRANSITION_CHAIN`（第 123 行）、`canRetrySamePreview`（第 92 行）、preview→execute 序列强制（第 24–27 行注释）。
- 组件直接 import 该业务规则：`components/decision/DecisionStageStepper.tsx`（第 2 行 `TRANSITION_CHAIN`）、`components/feedback/TypedRecoveryPanel.tsx`（第 1 行 `canRetrySamePreview`）、`components/decision/NewSessionFlow.tsx`（第 1–11 行 import `api/orchestration` 多个符号）。
- `api/schemas.ts`（1033 行）单文件承载全部响应类型，被 `components/{action,decision,evidence,feedback,proactive,system}/**` 十余个组件直接引用。
- 组件还直接 import `api/client.ts` 的 `ApiError` 类型（`EvidenceDrawer.tsx` 第 2 行、`ProactiveCard.tsx` 第 3 行）。

**耦合表现**：UI 组件与「API 层」之间的边界被业务规则穿透——状态机转移链、重试判定属于领域规则，不应放在 HTTP client 层；schemas.ts 成为前端版上帝文件，任何响应结构调整都要改这一个文件并连带十余个组件。

**影响**：前端领域规则与传输层耦合，后端契约变更直接冲击组件层；schemas.ts 单文件 1033 行难以并行开发；`mockData.ts`（837 行）与 `schemas.test.ts`（832 行）为维持一致性付出额外维护成本。

**建议拆分**
- 状态机规则拆到 `src/domain/orchestration.ts`（或 `features/orchestration/domain.ts`），组件与 hooks 都只依赖领域层；`api/` 层只保留 HTTP 客户端与类型。
- `schemas.ts` 按域拆分：`api/schemas/{intelligence,decision,proactive,agent,data,ui}.ts`，保留根 `schemas.ts` 做 re-export。

---

## Tech Debt

**`domains/*` 门面包依赖残留（2026-08-13 窗口相关）**
- `retrieval/memory.py` 第 638/707/802 行对 `domains.graph.query_graph` 的 lazy import 是现存的唯一「非门面消费者」风险点之一（旧记录已列 `retrieval/memory.py` 为 Phase 21 延迟项）；门面删除后必断，见 OC-9。其它 `domains.*` 门面模块本体见 `src/personal_knowledge/domains/{conversation,graph,knowledge,memory}/`，多数为 re-export 门面（`import_module` + `sys.modules` 重绑定）。

**`intelligence/analysis/providers.py`（649 行）LLM provider 中枢职责过多**
- 单文件同时定义 Protocol、`LegacyProviderAdapter`、`ReplayProvider`、`PiKernelProvider`、`OpenAICompatibleProvider`、`CodexCliProvider` 及 CLI 预检逻辑（`codex_cli_preflight` 第 407 行）。provider 注册/选择/预检耦合一体，新增 provider 需改中枢文件。建议拆 provider 类与注册表。

**`src/personal_knowledge/application/ku.py`（1060 行）CLI 入口依赖面过大**
- 第 504–907 行对 `application.knowledge.*` 十余个子模块逐个 lazy import 并透传 CLI 参数（`refresh` / `build_prod` / `canonical` / `publish` / `vector` / `promote` / `reconcile` / `lifecycle_events` / `history` / `doctor`）。ku.py 已成为「命令聚合器」，任何子命令参数变化都改它。建议按命令组拆分 `cli/` 子命令模块。

## Known Bugs

**MCP `search_semantic` 与 REST 的运行时一致性**
- 症状：`mcp_server.py` 第 1021 行 `search_semantic` 走 HTTP 回环（`_search_semantic_via_api`），其余工具进程内直连；REST 未启动时 MCP 的 `search_semantic` 单独失败而其它检索工具正常。
- 文件：`src/personal_knowledge/services/mcp_server.py`（第 102–116、1021 行）。
- 触发：MCP server 先于 REST server 启动，或 REST 进程崩溃后 MCP 存活。
- 修复：改为直连 `backend.search_knowledge_units`（同 api_server 第 1152 行）。

## Security Considerations

**未发现新的明文凭据存放问题**；既有 `privacy_guard`（`src/personal_knowledge/core/privacy_guard.py`）与 CORS/Origin gate（api_server 第 240/1064 行）对 REST 面有约束。注意：`services/mcp_server.py` 日志对 `confirmation_token/token/secret` 做 REDACTED（第 1015 行），但 `api_server.py` 未发现等价参数名红名单，若未来加 token 类参数需同步处理（非当前问题，仅提示）。

## Performance Bottlenecks

**MCP 语义检索双重序列化**
- 问题：`mcp_server.py::_search_semantic_via_api` 每次 `search_semantic` 调用经 HTTP 回环（JSON 序列化 + 网络栈 + 120s 超时），而进程内本可直接调用 `backend`（同一模块已 import 于第 84 行）。
- 文件：`src/personal_knowledge/services/mcp_server.py` 第 102–116 行。
- 影响：每次 MCP 语义检索多一次本地 HTTP 往返与负载均衡/代理栈开销（本机 127.0.0.1 场景毫秒级，但增加了故障面）。
- 改进路径：删除回环，直接调用 `backend.search_knowledge_units`。

## Fragile Areas

**`services/topic_projection.py` + `wiki/` 包的隐藏环（OC-7）**
- 文件：`src/personal_knowledge/services/topic_projection.py`、`src/personal_knowledge/wiki/{read_router,materialization,derived_store}.py`。
- 脆弱点：services↔wiki 相互顶层/lazy import 交替；任一侧改为顶层 import 即触发循环。
- 安全修改方式：新增代码一律不在此两包间加新交叉 import；改动 wiki 包时保持 topic_projection 侧的 lazy import 不变。
- 测试覆盖：`.planning/codebase/TESTING.md` 未见针对包级循环的 CI 门禁；建议加入 import 图静态检查（见下）。

**`retrieval/serving.py` ↔ `application/serving/snapshots.py`（OC-8）**
- 文件：`src/personal_knowledge/retrieval/serving.py`、`src/personal_knowledge/application/serving/snapshots.py`。
- 安全修改方式：快照字段读取集中改 `application/serving/snapshots.py`，`retrieval/serving.py` 只做解析；勿在 retrieval 侧新增对 application 其它子包的 import。

## Scaling Limits

**REST 单进程路由表（OC-1）**
- 当前容量：api_server 单 `do_GET` 内联 60+ 端点、约 485 行，仍可维护。
- 上限：端点数再翻倍或端点间需要独立鉴权/限流中间件时，单方法内联路由不可扩展。
- 扩展路径：按 OC-1 拆分路由表 + per-domain handler；需要中间件时引入 `http/handlers` 分组即可在组级别加装饰器。

## Dependencies at Risk

**`domains.*` 门面（2026-08-13 删除窗口）**
- 风险：`retrieval/memory.py` 第 638/707/802 行是现存最后一批非测试/非 shim 的 `domains.*` 消费者（旧 CONCERNS 已列其为 Phase 21 延迟项）。
- 影响：窗口到期删除门面后，`/memory/graph`、`/memory/<subject>?neighbors=`、记忆图谱相关工具全部 ImportError。
- 迁移方案：按 OC-9 先收敛为直接 import `application.graph.query_graph`（规范路径），再消除 R 层对图实现的直接依赖。

## Missing Critical Features

**包级依赖环静态检查缺失**
- 问题：本次扫描发现的 application↔intelligence、application↔evaluation、services↔wiki、external_context↔intelligence 双向边均靠「当前 import 恰好不触发」维持；仓库缺少 CI 级的包依赖方向断言。
- 阻塞：无法在 PR 阶段拦截新的反向依赖（如新代码在 retrieval 包加 `from personal_knowledge.application...`）。
- 建议：基于 `governance/policies/artifact_layers.yaml`（D/S/R/A 分层）加一个依赖方向检查脚本，白名单现有已确认的跨层边，新增跨层边必须显式登记。

## Test Coverage Gaps

**双表面契约测试缺失（OC-11）**
- 未测：REST `*_rest_contract` 与 MCP `*_tool_contract` 对同一 operation 的参数映射一致性（如 `route_operation_mismatch` 两处判定是否行为一致）。
- 文件：`src/personal_knowledge/services/api_server.py`、`src/personal_knowledge/services/mcp_server.py`。
- 风险：契约字段漂移在 MCP/REST 双表面间悄悄发生，前端按 REST 契约渲染、Agent 按 MCP 调用时行为不一致。
- 优先级：High（与 OC-11 同源）。

**retrieval/memory.py 图查询路径**
- 未测：`_bounded_memory_graph_ids`（第 630–650 行）对 `domains.graph.query_graph` 的依赖路径在门面删除后无迁移测试兜底。
- 风险：2026-08-13 后静默 ImportError。
- 优先级：High。

**前端状态机规则**
- 未测：`api/orchestration.ts` 的 `TRANSITION_CHAIN` / `canRetrySamePreview` 与后端 `orchestration_service` 转移序列的对照测试；`schemas.test.ts` 仅测类型形状，未测与后端 `agent_contract` 信封的实际兼容。
- 优先级：Medium。

---

*Concerns audit (over-coupling focus): 2026-08-08*
