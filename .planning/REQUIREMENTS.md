# Requirements: v2.0 Pi Personal Intelligence Kernel

**Defined:** 2026-08-04
**Core Value:** 以长期个人数据为内部状态、以外部社会环境为外部状态，在隐私安全、证据可回查和不确定性可解释的前提下，为用户提供可验证、可反馈、可持续校准的个人决策支持。

## v2.0 Requirements

### Kernel Runtime and Event Loop

- [ ] **KERNEL-01**: Pi 随本地产品服务受控启动和停止，成为唯一主 AI Session 与事件循环；legacy Agent 只作为 feature-flag 控制的回滚实现存在。
- [ ] **KERNEL-02**: 用户请求、canonical 数据 Delta、显式调度任务和恢复事件统一进入版本化事件协议，并保留 source、authority、snapshot、correlation、causation 与 idempotency identity。
- [ ] **KERNEL-03**: durable task 支持单次 claim、并发背压、cancel、resume、replay、超时和 `outcome_unknown` 恢复；重放不得重复模型副作用或推进权威水位。

### Model and Provider Control

- [ ] **MODEL-01**: 所有 AI 模型调用统一经过 Pi provider/model adapter，使用注入式认证、显式模型路由、token/cost budget、usage reconciliation 和安全错误契约。
- [ ] **MODEL-02**: 本地与云模型路由支持 allowlisted provider/host、rate limit、timeout、cancel、重试上限和 provider outcome-unknown；禁止隐式读取宿主认证或自动 fallback 到未批准模型。

### Tools, Skills and Resources

- [ ] **TOOL-01**: Pi 只能调用显式注册、schema-validated、最小权限的 Python Domain Tools；所有 authority 写入继续经过既有 Approval、checksum、sequence 和 lifecycle gate。
- [ ] **TOOL-02**: 生产配置禁用 coding built-ins、ambient `.pi`、全局 settings/auth/skills、extension/package 自动发现以及未批准的 filesystem/process/network 能力。
- [ ] **SKILL-01**: 建立版本化、可审计的 Skill/Tool/Event registry；Skill 选择、加载、冲突处理和升级均可确定性复现并绑定 evidence contract。

### Data Authority and Session Isolation

- [ ] **DATA-01**: Python 确定性核心继续独占事实、证据、水位、evaluation、promotion、active pointer、rollback 和正式生命周期规则；Pi 不得直连 authority stores 或绕过规则，但可通过显式 Domain Tools 发起受控、事务化、可回滚的数据操作。
- [ ] **DATA-02**: Agent 输出只能进入隔离的 Session/Candidate staging；未经 schema、evidence、privacy、evaluation 和 promotion gate，不得进入正式库存、active knowledge 或检索。
- [ ] **SESSION-01**: Pi Session 使用独立持久化和 schema version，支持 resume/fork/recovery、保留期、隐私清理、敏感字段脱敏及 Candidate/authority 数据库隔离。

### Project Capability OS Extension

- [ ] **CAP-01**: 建立 Project Capability Registry 作为 REST、MCP 和 Pi SDK Kernel 的 Tool/Skill/Event 单一契约来源；descriptor、schema、版本、checksum 和退役状态可确定性生成和核对。
- [ ] **CAP-02**: 每项能力声明 production/operator profile、privacy ceiling、authority class、side-effect class、budget、timeout、idempotency、confirmation 和 receipt contract；未知、漂移或权限升级请求 fail-closed。
- [ ] **PTOOL-01**: 将现有知识、检索、状态、External、决策、行动、证据、数据质量、Wiki 和系统健康能力收敛为稳定 namespaced Domain Tools，不直接暴露内部函数、脚本、数据库或重复的 REST/MCP 实现。
- [ ] **PTOOL-02**: 正式写 Tool 固定执行 `plan → dry-run → exact preview/checksum → confirm/policy → idempotent execute → invariant verify → receipt → compensate/rollback`，重复调用不得产生重复副作用。

### Controlled Warehouse Operations

- [ ] **WARE-01**: Pi 可通过参数化 Tool 检查 schema、统计、血缘、水位、新鲜度、完整性、异常和失败批次；禁止任意 SQL、任意路径和无限制结果集。
- [ ] **WARE-02**: Pi 可执行 source discovery、validate、quarantine、incremental import、canonical reconcile/deduplicate/link/correction；raw history 不可变，canonical 纠错使用 append-only 补偿记录并验证前后指纹。
- [ ] **WARE-03**: Pi 可执行 L1/L2 抽取、Candidate repair、冲突检测、backfill、索引构建、reconcile 和 evaluation；未通过 schema/evidence/privacy/eval gate 的产物不得进入 active inventory/retrieval。
- [ ] **WARE-04**: Pi 可 prepare/activate/rollback Serving Snapshot；active pointer 变更必须原子、snapshot-bound、用户可见且可精确恢复，任何验证失败保持原 active 版本。
- [ ] **SEC-03**: production/operator profile 均禁止任意 SQL、任意 filesystem/process/network/callable、直接 DELETE/TRUNCATE、未批准 schema migration、secret/body 日志和 gate bypass；底仓负向测试不得改变 authority fingerprint。

### Project Workflow Skills

- [ ] **PSKILL-01**: 首批项目 Skills 覆盖 daily brief、knowledge research、decision support、project planning、outcome reflection、knowledge maintenance、warehouse health、failed-batch recovery、retrieval rebuild、snapshot release 和 system diagnosis。
- [ ] **PSKILL-02**: 每个 Skill 使用版本/checksum、purpose、input/output schema、allowed_tools、状态机、预算、停止条件、privacy ceiling 和 recovery contract；选择结果为零或一个，冲突/漂移/过期时 abstain。
- [ ] **PSKILL-03**: 每个 Skill 有 deterministic fixture、tool-sequence assertion、forbidden-call assertion、fault/replay 测试和产品场景 rubric；生成内容仍为 Candidate，Agent 共识不构成事实或 promotion 授权。

### Kernel Control and Operations Plane

- [ ] **OPS-02**: Cockpit、ledger 和 telemetry 能区分 Kernel Task/Session、Skill、Domain Tool、底仓事务、Python authority 和 Provider 状态，并提供 cancel/resume/outcome-unknown/compensation 的无正文诊断与受控恢复入口。

### Extended Evaluation and Activation

- [ ] **EVAL-03**: 完成 Tool 选择、Skill 流程、production profile、底仓事务、Kernel 控制面、故障/重放/补偿和最小上下文泄露的自动化与人工 UAT；所有关键写路径验证 authority invariants。
- [ ] **ACT-03**: 只有 Phase 53 真实 paired baseline 转为 accepted、Phase 55–59 验证全部通过且用户明确授权后，才可重新执行 `shadow → canary → primary`；最终 primary 下 Pi SDK Kernel 是唯一生产 AI 协调者，legacy 仅为受控回滚实现。

### Product Integration and Operations

- [ ] **UI-01**: Decision Cockpit 使用安全 SSE/event projection 展示 Pi 进度、模型状态、Tool 状态、证据、cancel、resume 和 degraded recovery；浏览器不获得新的 authority 写入能力。
- [ ] **OPS-01**: supervisor、health/readiness、structured telemetry 和 task ledger 能区分 Pi、Python Domain API、Provider、Session Store 与 UI 故障，并提供无个人正文的诊断。

### Security, Evaluation and Activation

- [ ] **SEC-01**: 所有 `@earendil-works/pi-*` 包精确锁定版本、tarball integrity 和 dependency tree，禁用 lifecycle scripts；High/Critical 供应链风险清零或以审计批准的隔离补丁闭合后才能进入 production acceptance。
- [ ] **SEC-02**: filesystem、process、network、credential、log 和 package capability 按 deny-by-default/allowlist 执行；越权尝试必须 fail-closed 且不污染 Session、Candidate 或正式知识。
- [ ] **EVAL-01**: 在相同真实 cohort、模型和预算下完成 Pi/legacy 的质量、成本、延迟、可靠性、恢复性和工具选择基线，禁止用 synthetic Provider 代替 primary 激活证据。
- [ ] **EVAL-02**: HTTP/进程故障注入、隐私测试、浏览器 UAT、cancel/replay、并发和零未授权写入验证全部通过并产生可复核证据。
- [ ] **ACT-01**: 激活顺序固定为 `shadow → canary → primary`，每阶段都有进入/退出条件、用户可见状态、自动停止条件和 exact rollback 演练。
- [ ] **ACT-02**: Primary 激活后所有 AI 工作流由 Pi 驱动，禁止并行影子 Agent 控制面；任何 gate 失败均保持或恢复 legacy，且不推进 watermark、promotion 或 active pointer。

## v1.5 Requirements — accepted predecessor

v1.5 Phase 44–47 已完成 P0 只读实现和授权 UAT；扩域保持 DEFER。以下条目保留用于历史追踪。

### Personal Knowledge Wiki Projection

- [x] **WIKI-01**: 提供确定性、snapshot-bound 的 `topic.list`、`topic.get`、`topic.backlinks` 只读投影，覆盖 Project、Goal、Decision，不新建个人事实 SSOT。
- [x] **WIKI-02**: 提供可导航的目录和主题页，分离 Fact、Observation、Inference、Forecast、Recommendation、History/Conflict、External 与 Evidence，不复制 Decision Workspace 写入流程。
- [x] **WIKI-03**: 投影可安全物化、失效和重建；stale/partial/missing 时诚实降级，Wiki 内容不得写回 KU、Chroma 或检索 SSOT。
- [x] **WIKI-04**: 通过小规模主题 cohort 的隐私、移动端、键盘、离线和证据下钻 UAT，并据证据决定后续扩域或继续延期。

### Invariants

- `docs/wiki/` 仍是开发/运维文档，不是个人 Wiki authority。
- Wiki 是 projection/materialized view，不是 SSOT 或可编辑事实记录。
- Personal 与 External、事实与推断、当前与历史必须显式分区。
- Backlinks 只接受服务器明确提供的 typed join；不得使用向量相似度或 LLM 猜测关系。
- stale/partial/unavailable 不得伪装为当前完整结果。

## v1.4 Requirements — accepted predecessor

v1.4 的 Phase 40 UAT 已由用户在 2026-07-28 确认通过；验收记录保留在 `phases/PDA-40-product-hardening-and-live-uat/40-UAT.md`。以下历史条目保留作追溯，不再作为当前执行范围。

### Projection and Secure Transport

- [ ] **CCK-01**: 用户可通过版本化的只读 Cockpit Projection 查看汇总数据；浏览器不直连 SQLite/Chroma，不创建影子 SSOT，也不改变 Serving Snapshot、Active Pointer、KU lifecycle、External authority 或 Calibration promotion。
- [x] **CCK-02**: 生产 Cockpit 使用 loopback same-origin `/app` 与 API；移除 wildcard CORS，开发期仅允许显式来源，所有 mutation route 拒绝跨 origin 请求且不产生写入。
- [ ] **CCK-03**: UI Projection 在 authority 不可用时返回可验证的 `partial`、freshness、snapshot bindings 和 safe limitations；异常文本、路径、PII、密钥、provider body 与 confirmation/HMAC 不出现在 DOM、console 或 API 错误。
- [ ] **CCK-04**: Cockpit 代码、Projection、契约测试和构建说明进入可审计版本基线；未通过对应验证的 WIP 不得在 README 或计划中标为已交付。

### Current State, External Context and Evidence

- [ ] **STATE-01**: 用户可在总览和个人状态中查看当前目标、约束、变化、风险、决策队列与数据新鲜度，并明确区分 Fact、Observation、Inference、Forecast、Recommendation、Confirmation、Conflict 与 Historical。
- [ ] **STATE-02**: 用户可查看独立 External Context 的来源、地区、有效期、lifecycle、冲突和限制；External Fact 不会被显示或写入为 Personal Fact。
- [ ] **STATE-03**: 每个可操作的状态或决策结论显示 authority/snapshot/freshness/evidence 信息；binding mismatch、stale、conflict、partial 或 evidence 不足时不得允许 prepare/confirm/execute。
- [ ] **EVID-01**: 用户可从当前状态、External 或决策对象只读下钻到稳定的证据标识和可用详情；MCP Widget 或关联 authority 不可用时显示非空降级状态和恢复说明，不将旧 Memory Graph 说成当前 Personal State 权威。

### Guarded Decision Workflow

- [ ] **DEC-01**: 用户可在 Decision Workspace 比较决策问题、目标、硬约束、风险预算、候选方案、不行动基线、成本、机会成本、假设、反面证据、停止条件、缺失信息与限制。
- [ ] **DEC-02**: 用户只能在低风险 `project` 域通过 `prepare → exact preview → explicit confirm → commit` 写入；预览清楚显示将创建的事件、不会执行的动作、checksum、sequence、idempotency key 和具体确认文案。
- [ ] **DEC-03**: 用户在重复 confirm 时看到同一事件的 exact replay；preview 过期、sequence/binding/integrity/confirmation/risk/runtime 错误和 provider outcome unknown 都走 typed recovery，无自动重试或更换 payload。

### Feedback, Proactive and Runtime Truthfulness

- [x] **FDB-01**: 用户可浏览 Recommendation → Decision → Action → Outcome → Effectiveness → Calibration 的完整 append-only 历史，且所有结果明确保留 `causal_claim=false` 与样本/限制说明。
- [x] **FDB-02**: 用户可查看 Proactive 候选、协调、用户控制历史和 Calibration 状态；未暴露的 REST 写入能力必须诚实禁用或说明，UI 不新增自动 promotion 或外部动作。
- [x] **RUN-01**: 用户可分别看到 REST、MCP、Tunnel、Chroma 与 authority freshness 的真实健康状态；Cockpit 只读展示，不启动、停止或重启任何服务。

### Product Quality and Acceptance

- [ ] **UX-01**: Cockpit 在 320/768/1024/1440 宽度、键盘导航、可见焦点、Esc 抽屉关闭、reduced motion、200% 缩放、长中文与长 ID 情况下保持可读可用；图表有等价文字或表格信息。
- [ ] **UX-02**: REST 全离线、MCP Widget 不可用、Chroma 不可用或单 authority 不可用时，页面显示准确的 empty/partial/stale/offline/recovery 状态，不把缓存或空白页伪装为当前结果。
- [ ] **QA-01**: `npm run build`、前端 Vitest、UI Projection Python 契约、orchestration/replay/privacy 的相关测试均通过，并包含 DTO、状态分类、跨 origin 无写入、preview 篡改/过期与重复确认回归。
- [ ] **QA-02**: 完成并记录至少一次真实浏览器端到端 UAT，覆盖同源 read、低风险 prepare/confirm/exact replay、响应式/无障碍、服务降级、证据下钻与隐私检查；失败时有明确回滚或恢复记录。

## v1.5+ Requirements

### Personal Knowledge Wiki Projection

- **WIKI-01**: 基于活跃权威和 Snapshot 构建只读 Project、Goal、Decision 等主题页，不新建个人事实 SSOT。
- **WIKI-02**: 主题页展示来源、历史、关联决策和 backlinks，并由变化检测标记 stale、生成 Candidate、受控发布。
- **WIKI-03**: LLM 只能生成受证据约束的页面叙述候选；页面文案不会反馈写入 KU/Chroma 作为事实权威。

## Phase 61 Requirements — Conversation-first Desktop Harness

### Desktop Conversation Walking Skeleton

- [ ] **HARNESS-01**: A local Electron desktop shell opens directly into the last conversation and provides Codex-style new/recent conversation and project-scope navigation without requiring a browser product surface.
- [ ] **HARNESS-02**: The conversation path routes zero or one primary governed Skill through the actual Pi iterative model/Tool loop, scopes the Tool lease from the Skill manifest, and returns evidence, freshness, limitations, and expandable receipts.

### Governed Evidence Access

- [ ] **HARNESS-03**: A dedicated SQLite read-only Tool permits bounded SELECT/read-only CTE queries only against approved databases, views, and columns; unsafe statements, ATTACH, extension loading, write PRAGMA, unbounded results, and authority mutation fail closed.
- [ ] **HARNESS-04**: AgentView remains the cross-Agent aggregation source, and conversation answers expose both source-to-AgentView and AgentView-to-canonical freshness/backlog instead of presenting stale data as complete.

### Evidence-bound Reflection Loop

- [ ] **HARNESS-05**: A deterministic conversation-delta event produces a deduplicated reflection Candidate that preserves source evidence, inference status, confidence, time scope, conflicts, and receipt metadata; generated text and Agent agreement never become facts directly.
- [ ] **HARNESS-06**: The desktop conversation can review a Candidate through accept, edit, and ignore actions; acceptance uses the existing governed Candidate/canonical path and does not inherit authority from the Agent or Skill.
- [ ] **HARNESS-07**: Accepted content contributes to a versioned, derived personal-model projection that remains separable from immutable Evidence and can be retrieved in a later conversation with provenance, freshness, confidence, temporal validity, and contradiction disclosure.

### Safety and Verification

- [ ] **HARNESS-08**: Automated and desktop UAT prove renderer/IPC least privilege, no unrestricted SQL or body/credential leakage, no unauthorized authority change, deterministic trigger/dedup behavior, Tool receipt integrity, cancel/recovery behavior, and preservation of existing Phase 55–60 activation and rollback gates.

## Phase 62 Requirements — Multi-format Conversation Event Authority

### Native evidence capture and adapters

- [x] **CONV-01**: Every agent family currently observed in the live inventory has an explicit versioned adapter capability contract, native locator/schema gate, redacted fixture, and fail-closed unsupported/partial result; no family is silently flattened into a complete-looking transcript.
- [x] **CONV-02**: Allowlisted native conversation artifacts are captured as content-addressed immutable evidence before adaptation; live SQLite/WAL sources use online backup, and adjacent credential/account/token/auth tables and columns are never copied, queried, logged, or exposed.

### Typed event authority and fidelity

- [x] **CONV-03**: The canonical v2 model preserves typed session, message, reasoning, tool, usage, compaction, boundary, branch/subagent, context, and unknown-native events plus first-class relations, stable native/source identity, artifact provenance, and resolvable references for safe unmodeled payloads.
- [x] **CONV-04**: Every adapter run, session, and event reports explicit source/structure/order/relation/content/compaction/identity fidelity; missing native artifacts, unknown fields, redaction, ambiguity, and unsupported versions remain `partial`, `unknown`, or `unavailable` rather than being reported complete.

### Canonical evolution and compatibility

- [x] **CONV-05**: The existing canonical database, `pk-sync conversations`, publication registry, watermark, rollback, and `ConversationRepository` seams are reused while v2 event generations are staged and atomically activated; current session/message/tool tables remain deterministic compatibility projections until all registered consumers pass contract parity.

### Replaceable extraction views and gates

- [x] **CONV-06**: Versioned Turn, NativeTrace, Episode, CompactionWindow, Session, Topic, and CrossSession views are rebuildable from canonical events and preserve view lineage plus stable evidence-event references; extraction priority is controlled by a versioned policy rather than adapter code or permanent trace-first semantics.
- [x] **CONV-07**: Candidate admission runs deterministic privacy/secret/injection/structure/evidence checks before an abstention-capable semantic-value gate, and a summary/view claim cannot pass without resolvable supporting events, contradiction handling, and recorded reject/abstain reasons.

### Validation, isolation, and cost control

- [x] **CONV-08**: Redacted per-family reference fixtures, replay/idempotency tests, schema-drift/privacy negative tests, compatibility parity, source-to-event coverage reports, and activation/rollback fault injection pass while old KU generations stay quarantined, active KU stays empty, old 24,487-call queues remain unexecuted, and paid provider calls remain zero without a separate user approval checkpoint.

## v2.0 Out of Scope

| Feature | Reason |
|---|---|
| 将 Pi Session、memory 或生成文本直接作为个人事实 | Pi 是 AI 控制面，不是事实 authority；正式知识仍必须经过 Candidate、evidence、evaluation 和 promotion。 |
| 让 Pi 绕过 Domain Tools、preview/confirm、evaluation 或事务规则直接推进 watermark、promotion 或 active pointer | Pi 可编排这些流程，但最终变更继续由 Python 确定性核心按受控 Tool 契约提交。 |
| 自动外部动作、自动替用户决策或自动策略 promotion | v2.0 改造运行时，不扩大用户授权和风险边界。 |
| ambient `.pi`、全局 auth/settings/skills 或社区 package 自动发现 | 无法满足本地个人数据系统的可审计和最小权限要求。 |
| 未经 shadow/canary/UAT 直接切换 primary | 会绕过真实质量、可靠性、隐私和回滚证据。 |
| 使用 `pi-web-ui` 版本错配组件替换现有 Cockpit | 当前 UI 继续消费安全事件投影；UI 包需独立版本兼容资格审查。 |

## Traceability

| Requirement | Phase | Status |
|---|---|---|
| SEC-01..02, TOOL-02 | Phase 48 | Pending |
| KERNEL-01..02 | Phase 49 | Pending |
| KERNEL-03, TOOL-01, DATA-01..02, SESSION-01 | Phase 50 | Pending |
| MODEL-01..02, SKILL-01 | Phase 51 | Pending |
| UI-01, OPS-01 | Phase 52 | Pending |
| EVAL-01..02, ACT-01 | Phase 53 | Pending |
| ACT-02 | Phase 54 | Pending |
| CAP-01..02, PTOOL-01 | Phase 55 | Verified 2026-08-05 |
| WARE-01..02, SEC-03 | Phase 56 | Verified 2026-08-05 |
| WARE-03..04, PTOOL-02 | Phase 57 | Verified 2026-08-05 |
| PSKILL-01..03 | Phase 58 | Verified 2026-08-05 |
| OPS-02 | Phase 59 | Verified 2026-08-05 |
| EVAL-03, ACT-03 | Phase 60 | EVAL-03 deterministic pass; ACT-03 blocked by Phase 53 revise and human checkpoint |
| HARNESS-01..08 | Phase 61 | Pending |
| CONV-01..08 | Phase 62 | Done (62-01..62-08) |

**Coverage:** v2.0 原 34 项 requirements 保持映射；Phase 61 的 HARNESS-01..08 与 Phase 62 的 CONV-01..08 均已完整映射。

---
*Requirements defined: 2026-08-04*
*Last updated: 2026-08-12 after defining the multi-format conversation event authority and replaceable extraction-view requirements*
