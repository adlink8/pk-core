# Roadmap: 个人数据分析项目

## Milestones

- ✅ **v1.1 Knowledge Unit Evaluation & Product Hardening** — Phases 01–27, shipped 2026-07-18 ([archive](milestones/v1.1-ROADMAP.md))
- ✅ **v1.2 External Context & Low-risk Decision Intelligence Pilot** — Phases 28–31, shipped 2026-07-18 ([archive](milestones/v1.2-ROADMAP.md))
- ✅ **v1.3 Agent Productization** — Phases 32–35, shipped 2026-07-18 ([archive](milestones/v1.3-ROADMAP.md))
- ✅ **v1.4 Decision Cockpit UI** — Phases 36–40, UAT accepted 2026-07-28
- 🚧 **v1.4.1 Data Layer Remediation** — Phases 41–42, initiated 2026-07-26 from data-layer audit
- 🚧 **v1.5 Personal Knowledge Wiki Projection** — Phases 44–47, activated 2026-07-28
- 🚧 **v2.0 Pi Personal Intelligence Capability OS** — Phases 48–60, base roadmap approved 2026-08-04; capability/data/Skill scope expanded 2026-08-05

## v2.0 Goal

将 `@earendil-works/pi` 彻底嵌入为唯一主 AI Runtime，把现有项目能力工具化、稳定业务流程 Skill 化，并让 Pi 通过受控 Domain Tools 执行底仓维护；用户请求、数据 Delta、调度任务、模型调用、Skill/Tool 调度、Session 和流式交互统一进入事件驱动闭环。Pi 接管 AI 控制面与数据流程编排；Python 确定性核心继续独占事实、事务、一致性、证据、水位、evaluation、promotion、active pointer、rollback 和正式生命周期规则。

## v2.0 Phase Ordering

```text
48 Package qualification and runtime containment
→ 49 Kernel host and event lifecycle
→ 50 Durable task, Domain Tool bridge and session isolation
→ 51 Provider, Skill registry and full AI workflow migration
→ 52 Cockpit streaming, supervision and observability
→ 53 Real baseline, fault injection and UAT
→ 54 Primary activation and exact rollback
→ 55 Unified capability registry and project tool surface
→ 56 Controlled warehouse inspection, ingestion and canonical operations
→ 57 Semantic/retrieval maintenance and guarded release tools
→ 58 Project workflow Skills library
→ 59 Kernel control plane and runtime observability
→ 60 Whole-system UAT and final primary activation
```

Phase 48 是生产依赖入口的阻断门。High/Critical 供应链风险、ambient discovery 或越权能力未闭合时，后续阶段不得把 Pi 加入产品生产依赖或激活任何真实个人数据路径。

## v1.5 Goal

把 Project、Goal、Decision 的长期上下文做成确定性、只读、snapshot-bound 的 Wiki 投影；保留事实权威、证据、状态、External 与 Decision 的边界，不创建第二个事实库，不把页面内容写回 KU/Chroma。

## v1.5 Phase Ordering

```text
44 Topic authority and deterministic read projection
→ 45 Topic directory and evidence-backed pages
→ 46 Materialization, invalidation and Wiki-first fallback
→ 47 P0 hardening, cohort UAT and expansion decision
```

v1.5 的执行输入来自 `future-milestones/v1.5-personal-knowledge-wiki-projection/`；由于 v1.4.1 已占用历史 Phase 41–43，激活后的 Wiki 阶段映射为 44–47，避免阶段编号冲突。

## v1.4.1 Goal

收口 2026-07-25/26 数据层审计中确认的两个设计级问题：抽取疆域重定义（assistant 证据轨扶正、覆盖矩阵、eligible 口径统一）与会话级去重键架构。代码级 quickfix（F-01~F-14、C2）已先行落地，本里程碑处理需要设计决策的部分。

## v1.4 Goal

把已验证的 Personal State、External Context、Decision、Action/Outcome、Proactive、Evidence 与 Guarded Orchestration 组织为可日常使用的本地 Web 驾驶舱；所有读取保持 authority/snapshot/evidence 可追溯，所有浏览器写入仅限既有 `project + low` 的 `prepare → exact preview → explicit confirm → commit/replay` 受控路径。

本里程碑不创建新的事实 SSOT、不引入客户端个人数据缓存、不自动执行外部动作或 promotion。Personal Knowledge Wiki Projection 明确后置为 v1.5。

> **2026-07-22 实现对账：**Cockpit 与 Projection 的大部分代码已作为未提交 WIP 存在；
> Phase 36–40 是对既有实现的安全、证据、真值、运行时和真实浏览器验收收口，不是从零新建。
> 当前仍不得把 v1.4 声称为完成。逐项证据见
> [`.planning/audits/V1.4-IMPLEMENTATION-PLAN-RECONCILIATION-2026-07-22.md`](audits/V1.4-IMPLEMENTATION-PLAN-RECONCILIATION-2026-07-22.md)。

## Phase Ordering

```text
36 安全 Projection 与应用基线
→ 37 状态、External 与证据真值展示
→ 38 受控决策工作区与确认
→ 39 反馈、主动提醒与运行时真值
→ 40 产品硬化与真实浏览器 UAT
```

顺序依据是独立的失败与回滚边界：

1. 先收口 CORS、DTO、safe error 和只读 Projection，避免浏览器成为影子 authority 或跨域 mutation 面。
2. 再证明 UI 能诚实表达 Personal、External、partial、stale 与证据。
3. 仅在读取、快照和证据语义可信后，对既有低风险确认写入完成真值门与 fail-closed 加固；不在前置阶段人为关闭现有受控流程。
4. 反馈只能读取已确认的 append-only 决策链，不新增自动化。
5. 完整工作流出现后，才进行真实浏览器、故障、隐私和无障碍验收。

## Phases

### Phase 36: Secure Projection and Cockpit Baseline

**Goal:** 建立可审计、版本化、只读且同源安全的 Cockpit 基线，使浏览器只能消费服务端 Projection，不能绕过现有 authority 或受控编排。

**Requirements:** CCK-01, CCK-02, CCK-03, CCK-04

**Depends on:** v1.3 Agent Productization 的 REST、Guarded Orchestration 和 authority contracts
**Plans:** 4/4 plans executed — Phase 36 closed

**Success criteria:**

1. Cockpit 通过版本化 `decision_cockpit_projection_v1` 和相对同源 API 获取读取结果；浏览器不直连 SQLite/Chroma，不裁决 lifecycle，不修改任何 authority、Serving Snapshot 或 promotion。
2. 生产 `/app` 与 API 使用 loopback same-origin；移除 wildcard CORS，开发期仅允许显式来源；跨 origin mutation 被拒绝且确认无写入。
3. DTO、Zod schema、真实 response fixture 与 Python contract 一致；`partial`、freshness、snapshot bindings、limitations 和安全错误具有稳定、不泄露 PII、路径、密钥、provider body 或 confirmation/HMAC 的契约。
4. Cockpit、Projection、测试和构建说明进入可审计版本基线；构建、定向测试与 requirements traceability 能证明现有 WIP 未被提前宣称为已交付。

### Phase 37: Authority-aware State, External and Evidence

**Goal:** 把当前个人状态、独立 External Context 和证据下钻接入 Cockpit，并准确表达事实类型、时效、冲突、部分可用与恢复路径。

**Requirements:** STATE-01, STATE-02, STATE-03, EVID-01

**Depends on:** Phase 36
**Plans:** 3/3 plans executed — Phase 37 closed (verified 2026-07-27)

**Success criteria:**

1. Overview 与 Personal State 页面展示当前目标、约束、变化、风险、决策队列和新鲜度，并清晰区分 Fact、Observation、Inference、Forecast、Recommendation、Confirmation、Conflict 与 Historical。
2. External 页面展示独立来源、地区、有效期、lifecycle、冲突和限制；任何 External Fact 不被显示或写入为 Personal Fact。
3. 每个可操作状态和决策结论均可展示 authority、snapshot、freshness 与 evidence binding；binding mismatch、stale、conflict、partial 或证据不足时，UI 不允许进入 prepare/confirm/execute。
4. 用户可从状态、External 和决策对象下钻稳定证据标识及只读详情；MCP Widget 或关联 authority 不可用时显示非空 degraded/recovery 状态，且遗留 Memory Graph 明确仅为历史/诊断视图，而非当前 Personal State 权威。

### Phase 38: Guarded Decision Workspace

**Goal:** 在前两阶段已验证的 snapshot 与 evidence 语义之上，为既有低风险 `project` 决策提供可比较、可确认、可重放且 fail-closed 的浏览器工作区。

**Requirements:** DEC-01, DEC-02, DEC-03

**Depends on:** Phase 37
**Plans:** 3 documented; 0 executed

**Success criteria:**

1. Decision Workspace 可比较决策问题、目标、硬约束、风险预算、候选方案、不行动基线、成本、机会成本、假设、反面证据、停止条件、缺失信息与限制；不以单一“人生分数”替代解释。
2. 唯一允许的 UI 写入是既有 `project + low` 的 `prepare → exact preview → explicit confirm → commit`；预览准确呈现将创建的事件、不会执行的动作、checksum、sequence、idempotency key 和具体确认文案。
3. 重复 confirm 返回同一事件的 exact replay；preview 过期、sequence/binding/integrity/confirmation/risk/runtime 错误及 provider outcome unknown 全部采用 typed recovery，零自动重试、零 payload 替换、零未授权写入。

### Phase 39: Feedback, Proactive and Runtime Truthfulness

**Goal:** 让用户以只读、可复盘且不夸大因果的方式理解已有决策反馈、主动提醒和运行状态，而不为界面完整性新增写入权限、自动 promotion 或外部动作。

**Requirements:** FDB-01, FDB-02, RUN-01

**Depends on:** Phase 38
**Plans:** 4/4 plans complete

**Success criteria:**

1. Action/Outcome 页面可连续浏览 `Recommendation → Decision → Action → Outcome → Effectiveness → Calibration` 的 append-only 历史；所有结果显式保留 `causal_claim=false`、样本量和限制。
2. Proactive 与 Calibration 页面准确展示候选、协调、用户控制历史和校准状态；未暴露 REST 写入能力的操作必须禁用并解释，不能呈现假按钮，不增加自动 promotion 或外部动作。
3. System 页面分别呈现 REST、MCP、Tunnel、Chroma 与各 authority freshness；REST 成功不被包装为所有下游健康，Cockpit 不提供启动、停止或重启服务能力。
4. 页面与相关契约测试证明反馈和主动信息只读取既有 authority，不会写入 Personal/External 事实或改变 Calibration 策略。

### Phase 40: Product Hardening and Live UAT

**Goal:** 对完整 Cockpit 执行响应式、无障碍、降级、隐私和真实浏览器验收，形成可审计的发布/恢复证据，而非仅以组件测试代替产品可用性。

**Requirements:** UX-01, UX-02, QA-01, QA-02

**Depends on:** Phase 39
**Plans:** 3 documented; 0 executed

**Success criteria:**

1. Cockpit 在 320/768/1024/1440 宽度、键盘导航、可见焦点、Esc 抽屉关闭、reduced motion、200% 缩放、长中文和长 ID 条件下可读可用；图表有等价文本或表格信息。
2. REST 全离线、MCP Widget 不可用、Chroma 不可用或单 authority 不可用时，页面准确区分 empty、partial、stale、offline 和 recovery，不把缓存、空白或旧信息伪装为当前结论。
3. `npm run build`、前端 Vitest、UI Projection Python contracts 与 orchestration/replay/privacy 定向测试均通过；测试覆盖 DTO、状态分类、跨 origin 无写入、preview 篡改/过期和重复确认。
4. 完成并记录至少一次真实浏览器 UAT：覆盖同源 read、低风险 prepare/confirm/exact replay、响应式/无障碍、服务降级、证据下钻和隐私检查；失败路径具有明确恢复或回滚记录。

### Phase 41: Extraction Scope Redefinition (Assistant Track, Coverage, Eligibility)

**Goal:** 把知识抽取从"user-only 单轨"重定义为显式双轨（user 轨守用户画像、assistant 轨收知识资产），建立 source × role × pass 覆盖矩阵并统一 inspect/prepare/inventory 的 eligible 口径。

**Requirements:** EXT-01, EXT-02, EXT-03

**Depends on:** v1.4.1 数据层审计修复（F-01~F-14 已落地）；evidence_scope 双列语义（status/lifecycle 修复后）
**Plans:** 0 documented; 0 executed

**Success criteria:**

1. assistant 轨成为有名有姓的抽取路径：独立 prompt、独立 unit_type 集合（solution/decision_rationale/technical_conclusion 等）、evidence 锚 assistant 原文、`evidence_scope='assistant'` 写入、独立 eval 集；ku| 世代既有 assistant 来源 KU 完成归属迁移或显式豁免。
2. 覆盖矩阵落地：`source × role × pass` 的"消息数/已单元化数/未覆盖原因"进入 `pk-ku doctor`，zcode/grok/qoder 等零覆盖源显形报警。
3. eligible 口径唯一化：inspect 与 prepare 对"什么算 eligible 证据"使用同一定义（assistant 是否纳入由轨而非隐式 SQL 差异决定），"inspect 有 delta 而 prepare no_op"的 Gate B 判定恢复可信。

### Phase 42: Conversation Dedup with Stable Session Keys

**Goal:** 把 canonical 会话去重从文件内容 hash 改为会话级稳定键，消除"会话更新 → 新旧 canonical session 双份并存"的结构性重复。

**Requirements:** DED-01, DED-02

**Depends on:** Phase 41（eligible/证据口径稳定后改去重键才安全）
**Plans:** 3 documented; 2 complete, 1 partial

**Success criteria:**

1. 同一会话的内容增长（jsonl 追加）被识别为同一 canonical session 的更新而非新会话：canonical 库中同会话不再多份并存，旧副本有明确的 supersede/替代语义。
2. 全量重建幂等：重跑 sync 不产生重复 session/message，merge 结果跨运行稳定（含 legacy 代表行选择的确定性）。

### Phase 43: L2 Scope Redefinition (Cross-turn State Ownership and Incremental Dedup)

**Goal:** 把 L2 从"L1 补漏"重定义为"跨轮状态变更所有者 + 增量去重守门"：同一事实不再被 L1/L2 反复产出平行重述 unit，目录/分支/阶段/计划类状态知识有可查的时效语义。

**Requirements:** L2G-01, L2G-02, L2G-03, L2G-04

**Depends on:** Phase 42（stable session key 去重收敛后，subject/会话级增量语义才可定义）；42-03 delta 归因已查明（41 eligibility 收紧的口径账，非会话合并）
**Plans:** 9 plans

Plans:
**Wave 1**

- [ ] 43-01-PLAN.md — 状态 subject 清单 yaml + 归一化/匹配共用模块 + LLM 聚类建议人工确认（D-04/D-05）
- [ ] 43-03-PLAN.md — candidate lifecycle 值扩展迁移 + publish 排除 + pk-ku promote 人工转正通道（D-06/D-09）
- [ ] 43-06-PLAN.md — L2G-03 当前值视图：history「← 当前值」+ rag-search --current-only（最小路线）

**Wave 2** *(blocked on Wave 1 completion)*

- [ ] 43-02-PLAN.md — 注入召回（两阶段）+ duplicate_of 模型字段与白名单校验（D-01/D-02/D-03）
- [ ] 43-08-PLAN.md — L2G-04 前半：11,008 条三层分级报告 + LLM 复核 + 抽样人工检视（D-08）

**Wave 3** *(blocked on Wave 2 completion)*

- [ ] 43-04-PLAN.md — L1 双轨接线：v2 prompt + 注入段 + 提交处校验/candidate 路由
- [ ] 43-05-PLAN.md — L2 接线：v2_session_window prompt + 状态管辖语义 + 提交处校验
- [ ] 43-09-PLAN.md — L2G-04 后半：治理链分批处置 + watermark 收敛执行笔记（D-09/D-10）

**Wave 4** *(blocked on Wave 3 completion)*

- [ ] 43-07-PLAN.md — L2G-01/02 验收：集成对照测试 + 实验库实跑证据

**Success criteria:**

1. 增量去重守门：L1/L2 抽取时注入同 subject 已有 canonical 清单，新抽 unit 与已有等价时标 supersede 而非新增 current；构造重复会话重跑抽取，平行重述新增率相对基线（⑧ 一轮 294 q-gated 对）显著下降。
2. 状态类知识归属：目录/分支/阶段/计划类 subject 归 L2 跨轮管辖，双轨 run 后这些 subject 的 L1 current 新增为 0（或产出即标 candidate）。
3. 时效语义：状态类 unit 沿用 supersede 链区分"当前值/历史值"，查询侧可区分，不新增 schema 字段。
4. 工具输出源知识分级处置：11,008 条源消息被 41 新口径排除的 staging unit 先分级（真知识/噪音）再处置，不一刀切 deprecate；分级报告 + 治理链处置记录 + inspect delta 收敛方案（watermark 推进时机）写进执行笔记。

### Phase 44: Topic Authority and Deterministic Read Projection (v1.5 active)

**Goal:** 为 Project、Goal、Decision 建立只读、deterministic、snapshot-bound 的 Wiki authority/read contract。
**Requirements:** WIKI-01
**Plans:** 2/2 implementation plans complete; live Personal authority is read-only `stale` via latest committed run when serving snapshot changed

### Phase 45: Topic Directory and Evidence-backed Pages (v1.5 active)

**Goal:** 提供 P0 topic directory、typed page、显式 evidence drawer 和 truth-category separation，不复制事实库或决策写入。
**Requirements:** WIKI-02
**Plans:** 2/2 implementation plans complete; component/build verified

### Phase 46: Materialization, Invalidation and Wiki-first Fallback (v1.5 active)

**Goal:** 用 dedicated metadata-only derived store 支持显式 materialize/rebuild、选择性 stale 与安全 fallback。
**Requirements:** WIKI-03
**Plans:** 3/3 implementation plans complete; contract/integration verified

### Phase 47: P0 Hardening, Cohort UAT and Expansion Decision (v1.5 active)

**Goal:** 在去标识化 cohort 和授权真实只读会话上完成日常可用性、隐私、降级和 scope decision 证据。
**Requirements:** WIKI-04
**Plans:** 3/3 executed — Phase 47 closed; WIKI-04 P0 authorized read-only UAT passed, expansion deferred

### Phase 48: Pi Package Qualification and Runtime Containment (v2.0)

**Goal:** 形成可进入产品树的精确锁定 Pi 依赖基线，并以可执行负向测试证明 runtime/resource/package 能力默认拒绝、可审计且不接触个人数据 authority。
**Requirements:** SEC-01, SEC-02, TOOL-02
**Depends on:** `.planning/spikes/pi-package-qualification` 与 Spike 001；v1.5 P0 authority 和服务保持不变
**Plans:** 3 plans in 3 waves — executed; composite decision accepted

**Success criteria:**

1. 所有 `@earendil-works/pi-*` 生产候选依赖精确锁定版本、tarball integrity、Node engine、license、dependency tree 和 install-script policy；npmjs.org production audit 无未处置 High/Critical。
2. Kernel bootstrap 显式构建 allowlisted ResourceLoader/Tool registry；coding built-ins、ambient `.pi`、全局 auth/settings/skills、extension/package 自动发现均不可达。
3. filesystem、process、network、credential、log 和 package 越权负向测试 fail-closed，且 authority、watermark、active pointer、Candidate 和 Session 指纹不变。
4. 输出 `accepted | conditional | rejected` 的逐包决议；只有 `accepted` 基线允许 Phase 49 引用为产品依赖。

### Phase 49: Pi Kernel Host and Event Lifecycle (v2.0)

**Goal:** 建立随产品服务受控启停的 Pi Kernel Host 和统一、版本化、可回放的事件生命周期。
**Requirements:** KERNEL-01, KERNEL-02
**Depends on:** Phase 48 accepted package baseline
**Plans:** 2/2 executed; journal, host lifecycle and loopback SSE transport verified

### Phase 50: Durable Task, Domain Tool Bridge and Session Isolation (v2.0)

**Goal:** 将 durable task、typed Python Domain Tool bridge、Session/Candidate staging 和 authority 隔离组合为可恢复的执行底座。
**Requirements:** KERNEL-03, TOOL-01, DATA-01, DATA-02, SESSION-01
**Depends on:** Phase 49
**Plans:** 2/2 executed; task/artifact isolation, typed domain bridge and recovery contracts verified

### Phase 51: Provider, Skill Registry and Full AI Workflow Migration (v2.0)

**Goal:** 统一 Provider/模型预算、受控 Skill registry，并把现有 AI 入口迁移到 Pi Kernel，消除并行主控制面。
**Requirements:** MODEL-01, MODEL-02, SKILL-01
**Depends on:** Phase 50
**Plans:** 2/2 executed; provider routing, legacy rollback adapter, skill registry and entrypoint inventory verified

### Phase 52: Cockpit Streaming, Supervision and Observability (v2.0)

**Goal:** 将安全事件投影、cancel/resume、supervisor、readiness 和无正文 telemetry 接入现有 Cockpit 与本地服务。
**Requirements:** UI-01, OPS-01
**Depends on:** Phase 51
**Plans:** 2/2 executed; cockpit projection, task controls, SSE boundary and supervisor integration verified

### Phase 53: Real Baseline, Fault Injection and UAT (v2.0)

**Goal:** 以相同真实 cohort/模型/预算完成 Pi/legacy 基线、故障注入、隐私与浏览器 UAT，并冻结 staged activation 决策证据。
**Requirements:** EVAL-01, EVAL-02, ACT-01
**Depends on:** Phase 52
**Plans:** 2/2 implementation; replay/fault infrastructure, bounded real Pi Kernel smoke and browser UAT verified; paired baseline executed but remains INCONCLUSIVE on the frozen response contract and minimum-sample gate

### Phase 54: Primary Activation and Exact Rollback (v2.0)

**Goal:** 按 `shadow → canary → primary` 激活 Pi 为唯一主 AI Runtime，完成 exact rollback 演练并把 legacy 降级为受控备用实现。
**Requirements:** ACT-02
**Depends on:** Phase 53 acceptance
**Plans:** 2/2 implementation; activation ledger and exact rollback verified, primary activation remains gated

### Phase 55: Unified Capability Registry and Project Tool Surface (v2.0)

**Goal:** 建立 REST、MCP 与 Pi SDK Kernel 共用的 Project Capability Registry，并把现有项目只读能力收敛为稳定、namespaced、profile-aware 的 Domain Tools。
**Requirements:** CAP-01, CAP-02, PTOOL-01
**Depends on:** Phase 51 registry/provider baseline；复用现有 REST/MCP/Service，不改变 authority
**Plans:** 2/2 implemented — registry, descriptors and unified read Tool surface verified

### Phase 56: Controlled Warehouse Inspection, Ingestion and Canonical Operations (v2.0)

**Goal:** 让 Pi 在无任意 SQL/路径/破坏性权限的前提下，检查底仓并执行可预览、幂等、可补偿的 source ingestion 与 canonical 维护。
**Requirements:** WARE-01, WARE-02, SEC-03
**Depends on:** Phase 55
**Plans:** 2/2 implemented — bounded warehouse reads, ingestion ledger and canonical compensation verified

### Phase 57: Semantic/Retrieval Maintenance and Guarded Release Tools (v2.0)

**Goal:** 将 L1/L2 抽取、repair/backfill、索引构建、reconcile、evaluation、snapshot activate/rollback 组合成受控数据平面，并统一正式写 Tool 的事务协议。
**Requirements:** WARE-03, WARE-04, PTOOL-02
**Depends on:** Phase 56；现有 KU/retrieval/promotion authority
**Plans:** 2/2 implemented — semantic/retrieval maintenance and guarded snapshot release verified

### Phase 58: Project Workflow Skills Library (v2.0)

**Goal:** 把项目稳定业务流程注册为版本化、可审计、可评测的 Pi Skills，以有限 Tool 白名单执行个人智能与数据维护工作流。
**Requirements:** PSKILL-01, PSKILL-02, PSKILL-03
**Depends on:** Phase 55–57
**Plans:** 2/2 implemented — 11 bounded personal/data Skills, checkpoints and recovery evaluation verified

### Phase 59: Kernel Control Plane and Runtime Observability (v2.0)

**Goal:** 收口 Pi SDK Kernel 的 Task/Session/Skill/Tool/Provider/authority transaction 控制面，提供统一状态投影、cancel/resume/reconcile 与无正文诊断，同时确保不存在第二套 Agent 或平级协调器。
**Requirements:** OPS-02
**Depends on:** Phase 58；Phase 49–52 runtime/observability baseline
**Plans:** 2/2 implemented — single Kernel operation control, recovery and metadata-only Cockpit projection verified

### Phase 60: Whole-system UAT and Final Primary Activation (v2.0)

**Goal:** 对 Tool、Skill、底仓事务、Kernel 控制面、故障/补偿和隐私完成全系统验收；在 Phase 53 accepted baseline 与用户授权成立后重新执行 shadow → canary → primary。
**Requirements:** EVAL-03, ACT-03
**Depends on:** Phase 53 accepted decision；Phase 55–59 verified；显式用户激活授权
**Plans:** 2/2 implemented — deterministic UAT and synthetic rollback verified; real paired baseline/primary remain manual checkpoints

## Progress

| Phase | Requirements | Plans Complete | Status |
|---|---|---:|---|
| 36 | CCK-01, CCK-02, CCK-03, CCK-04 | 4/4 executed | Closed — 36-01 secure transport, 36-02 safe projection envelope, 36-03 frontend DTO/vocabulary hardening, 36-04 auditable baseline + 36-VERIFICATION.md all closed |
| 37 | STATE-01, STATE-02, STATE-03, EVID-01 | 3/3 executed | Closed — 37-01 server contracts (evidence_resolve 六态), 37-02 authority-aware state UI, 37-03 evidence drilldown + widget containment; independent verification 4/4 PASS 2026-07-27 |
| 38 | DEC-01, DEC-02, DEC-03 | 3/3 executed | **Verified 2026-07-27** — technical passed, security contract_scoped_passed（38-VERIFICATION） |
| 39 | 4/4 | Complete    | 2026-07-27 |
| 40 | UX-01, UX-02, QA-01, QA-02 | 3/3 executed | **Blocked 2026-07-27** — technical passed（npm build+255 tests、Python 矩阵全绿）；UX-01/QA-02 pending_human_uat，待真实浏览器 Live UAT |
| 41 | EXT-01, EXT-02, EXT-03 | 4/4 waves + closure | **Complete 2026-07-27** — assistant 轨全链闭合,active 40,200 向量,doctor OK（见 41-CLOSURE-CHECKLIST） |
| 42 | DED-01, DED-02 | 2/3 complete | **Partial 2026-07-27** — 42-01 stable-key rebuild and 42-02 ref migration complete; 42-03 doctor/idempotence complete but dual-track strict yield gate failed, so watermark remains intentionally unadvanced |
| 43 | L2G-01, L2G-02, L2G-03, L2G-04 | 9/9 executed | **Partial 2026-07-28** — 施工完成、测试全绿、doctor OK；224 个治理 proposal 待人工裁定，watermark 按判据未推进（43-WATERMARK-NOTE） |
| 44 | WIKI-01 | 2/2 implementation | **Implemented / authority stale-aware** — deterministic read contract and REST are present; latest committed Personal run is surfaced as stale when serving snapshot differs |
| 45 | WIKI-02 | 2/2 implementation | **Implemented / component verified** — directory, typed pages and read-only evidence drawer are wired |
| 46 | WIKI-03 | 3/3 implementation | **Implemented / contract verified** — metadata-only derived store, selective invalidation and fallback are wired |
| 47 | WIKI-04 | 3/3 executed | **Closed 2026-07-28** — latest 8000 service passed P0 authorized read-only UAT; expansion domains explicitly DEFER |
| 48 | SEC-01..02, TOOL-02 | 3/3 executed | **Complete** — same-run package/runtime/privacy/fingerprint gate accepted; Phase 49 unlocked |
| 49 | KERNEL-01..02 | 2/2 | **Complete 2026-08-04** — durable kernel journal/host lifecycle and loopback SSE transport verified |
| 50 | KERNEL-03, TOOL-01, DATA-01..02, SESSION-01 | 2/2 | **Complete 2026-08-04** — task/artifact isolation, typed domain bridge and recovery contracts verified |
| 51 | MODEL-01..02, SKILL-01 | 2/2 | **Complete 2026-08-04** — provider routing, legacy rollback adapter, skill registry and AI-entrypoint inventory verified |
| 52 | UI-01, OPS-01 | 2/2 | **Complete 2026-08-04** — cockpit projection, task controls, SSE boundary and supervisor integration verified |
| 53 | EVAL-01..02, ACT-01 | 2/2 implementation | **Revise / blocked 2026-08-04** — real paired arms executed once each, but response-contract and minimum-sample gates are INCONCLUSIVE; browser UAT accepted |
| 54 | ACT-02 | 2/2 implementation | **Revise / blocked 2026-08-04** — activation ledger and exact rollback pass; primary remains unactivated and runtime remains `legacy` |
| 55 | CAP-01..02, PTOOL-01 | 2/2 | **Verified 2026-08-05** — shared registry and read Tool surface |
| 56 | WARE-01..02, SEC-03 | 2/2 | **Verified 2026-08-05** — controlled ingestion/canonical data plane |
| 57 | WARE-03..04, PTOOL-02 | 2/2 | **Verified 2026-08-05** — derived maintenance and guarded release |
| 58 | PSKILL-01..03 | 2/2 | **Verified 2026-08-05** — project workflow Skill library |
| 59 | OPS-02 | 2/2 | **Verified 2026-08-05** — Kernel control plane and runtime observability |
| 60 | EVAL-03, ACT-03 | 2/2 | **UAT verified / activation blocked 2026-08-05** — real paired baseline remains revise; runtime remains legacy |

## Requirement Coverage

| Requirement group | Phase | Count |
|---|---:|---:|
| CCK-01..04 | 36 | 4 |
| STATE-01..03, EVID-01 | 37 | 4 |
| DEC-01..03 | 38 | 3 |
| FDB-01..02, RUN-01 | 39 | 3 |
| UX-01..02, QA-01..02 | 40 | 4 |
| **Total v1.4** | — | **18/18** |
| EXT-01..03 | 41 | 3 |
| DED-01..02 | 42 | 2 |
| L2G-01..04 | 43 | 4 |
| **Total v1.4.1** | — | **9/9** |
| WIKI-01 | 44 | 1 |
| WIKI-02 | 45 | 1 |
| WIKI-03 | 46 | 1 |
| WIKI-04 | 47 | 1 |
| **Total v1.5** | — | **4/4 accepted for P0 scope; expansion domains deferred by recorded decision** |
| SEC-01..02, TOOL-02 | 48 | 3 |
| KERNEL-01..02 | 49 | 2 |
| KERNEL-03, TOOL-01, DATA-01..02, SESSION-01 | 50 | 5 |
| MODEL-01..02, SKILL-01 | 51 | 3 |
| UI-01, OPS-01 | 52 | 2 |
| EVAL-01..02, ACT-01 | 53 | 3 |
| ACT-02 | 54 | 1 |
| CAP-01..02, PTOOL-01 | 55 | 3 |
| WARE-01..02, SEC-03 | 56 | 3 |
| WARE-03..04, PTOOL-02 | 57 | 3 |
| PSKILL-01..03 | 58 | 3 |
| OPS-02 | 59 | 1 |
| EVAL-03, ACT-03 | 60 | 2 |
| **Total v2.0** | — | **34/34 mapped** |

## Backlog

> 未排序停车场（999.x）。来源：2026-07-26 全量修补梳理（4 路并行架构/数据流/
> 评估/规划核查）。执行细则见各 phase 目录；Phase 41 收尾清单见
> [`phases/PDA-41-extraction-scope-redefinition-assistant-track-coverage-eligi/41-CLOSURE-CHECKLIST.md`](./phases/PDA-41-extraction-scope-redefinition-assistant-track-coverage-eligi/41-CLOSURE-CHECKLIST.md)。
> 用 `$gsd-review-backlog` 择机提升为活跃 phase。

### Phase 999.1: 数据管线健壮性修复 (BACKLOG)

**Goal:** 消除审计登记的低概率高影响缺口：canonical 发布崩溃窗口（两次
`os.replace` 之间 dest 缺失，审计 M4）、timestamp 格式混存致字典序比较不可靠
（L2，定 `YYYY-MM-DDTHH:MM:SSZ` 规范 + 存量迁移 + 写入口校验）、
`parent_canonical_id` 永不回填（L3，实现回填或删字段，与 lifecycle 审查同批）。
**Requirements:** TBD
**Plans:** 11 plans

### Phase 999.2: 检索性能优化 (BACKLOG)

**Goal:** canonical_messages 关键词层由多 token AND LIKE 全扫（80,516 行）改
FTS5 虚表 + 同步触发器（保留 LIKE 滑窗片段路给 code-literal 长粘贴）；rag-api
启动预热 embedding 模型与 Chroma 连接，消除 p95 冷启动尾部（实测 2,082ms vs
稳态 25–150ms）。
**Requirements:** TBD
**Plans:** 12 plans

### Phase 999.3: 治理与架构例外收口 (BACKLOG)

**Goal:** 收掉 `architecture.yaml` 中 retrieval(`vector`) 反向 import
knowledge/conversation domain 契约的唯一方向性例外（契约下沉 core 或显式接口
层）；升级 GSD scanner 词表消除 6 个历史 UAT 文件（Phase 14/17/18×2/20/27）
的 open 误报。
**Requirements:** TBD
**Plans:** 0 plans

### Phase 999.4: domains facade 物理删除 (BACKLOG)

**Goal:** 日期门 2026-08-13（PRODUCT-READINESS）。前置已满足：
`application → domains` 真实 import = 0（`pk-ku doctor` 守护）。整包删除
`domains/` + shim registry `expected_count` 下调（走 retirement cohort 人工
批准）。
**Requirements:** TBD
**Plans:** 0 plans

### Phase 999.5: 评测简化与 gold 集扩充（单人可持续协议） (BACKLOG)

**Goal:** 把评测体系从"团队级严谨"降档为"单人可持续"，同时闭合就绪分两
短板之一 Eval/canary=74 与 Phase 17 human gold/judge UAT 残留。三层协议：
L1 机械门（隐私/citation/reconcile）全自动保持不动；L2 canary LLM 打标 +
人工只复核 critical（每次 promote ~15 分钟），judge 校准做一次性 1 小时投
资（一致率 ≥90% 后信任 + 10% 抽查）；L3 gold 集用"LLM 起草 → 人三键核对
（对/错/删）"扩到 100+ 条（复用 `review_packets.py` /
`llm_review_receipt.py` 基建），后续增长靠"使用即标注"（错例随手记 jsonl，
每月转正式 case；v1.4 Cockpit 落地后升级为界面 👎 按钮，衔接 Phase 39 反馈
链路）。同时显式降档：`eval_policy` 阈值调宽（yaml 改策略不改代码）、放弃
统计显著性追求（评测职责=抓回归）、场景覆盖收缩到真实提问/时变/隐私三类、
Phase 17 遗留 UAT 以简化协议关闭并记录。细化见
[`phases/999.5-eval-simplification-gold-expansion/999.5-NOTES.md`](./phases/999.5-eval-simplification-gold-expansion/999.5-NOTES.md)。
**Requirements:** TBD
**Plans:** 0 plans

## v1.5 Execution Source

v1.5 的详细契约和背景仍保留在 [`future-milestones/v1.5-personal-knowledge-wiki-projection`](./future-milestones/v1.5-personal-knowledge-wiki-projection/README.md)；其 Phase 41–44 已映射为当前活动 Phase 44–47，以避开 v1.4.1 已占用的历史编号。当前 P0 实现与真实服务授权只读 UAT 已完成；扩域保持单独延期。

### Phase 61: Conversation-first Desktop Harness and Evidence-bound Reflection Loop

**Goal:** Deliver a local conversation-first desktop Walking Skeleton that uses the real Pi Agent tool loop, existing governed Skills/Tools, bounded read-only SQLite evidence access, and one end-to-end evidence-to-Candidate-to-personal-model reflection path.
**Requirements**: HARNESS-01, HARNESS-02, HARNESS-03, HARNESS-04, HARNESS-05, HARNESS-06, HARNESS-07, HARNESS-08
**Depends on:** Phases 55, 58, and 59; Phase 60 primary activation is not required
**Plans:** 12 plans

Plans:
**Wave 1**

- [x] 61-01-PLAN.md — Electron package qualification and blocking approval record
- [x] 61-03-PLAN.md — real Pi AgentSession conversation loop and three profiles
- [x] 61-04-PLAN.md — bounded governed SQLite evidence Tool

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 61-02-PLAN.md — approved Electron shell, IPC schema and preload boundary
- [x] 61-05-PLAN.md — canonical thread projection and dual freshness

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 61-06-PLAN.md — post-commit canonical event publisher and durable dispatcher

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 61-07-PLAN.md — evidence-bound reflection Candidate and deterministic proactive adapter

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 61-08-PLAN.md — guarded Candidate review fixed route

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 61-09-PLAN.md — versioned projection and governed next-turn injection

**Wave 7** *(blocked on Wave 6 completion)*

- [x] 61-10-PLAN.md — fixed deterministic proactive control/read/dismiss/undo routes

**Wave 8** *(blocked on Wave 7 completion)*

- [x] 61-11-PLAN.md — conversation-first renderer, provider bridge binding and controlled-query display

**Wave 9** *(blocked on Wave 8 completion)*

- [x] 61-12-PLAN.md — regression aggregation and six-step Electron UAT

### Phase 62: Multi-format conversation adapters, unified event authority, and replaceable extraction views

**Goal:** Evolve the existing canonical conversation authority into a loss-aware, typed multi-format event generation covering every currently observed agent family, while preserving immutable native evidence, compatibility consumers, replaceable extraction views, deterministic/semantic gates, and exact rollback without paid extraction.
**Requirements**: CONV-01, CONV-02, CONV-03, CONV-04, CONV-05, CONV-06, CONV-07, CONV-08
**Depends on:** Phase 61
**Plans:** 8 plans

Plans:
- [x] 62-01-PLAN.md — typed event/fidelity contracts, immutable snapshots, and generation-bound v2 repository
- [x] 62-02-PLAN.md — JSONL/DAG/loop adapters for ten stream-oriented agent families
- [x] 62-03-PLAN.md — SQLite/directory/partial adapters and credential-adjacent privacy boundary for seven families
- [x] 62-04-PLAN.md — canonical compatibility projection, `pk-sync` integration, atomic activation and rollback state machine
- [x] 62-05-PLAN.md — replaceable turn/trace/episode/compaction/session/topic/cross-session views and extraction policy
- [x] 62-06-PLAN.md — deterministic-first semantic admission and zero-cost view candidate preparation
- [x] 62-07-PLAN.md — all-family live shadow fidelity validation and blocking human activation review
- [x] 62-08-PLAN.md — approved live canonical v2 activation, compatibility verification, and rollback/reactivation drill

---
*Updated 2026-08-05 after consolidating the Capability OS on the Pi SDK Kernel and removing the Local Pi Agent; real activation remains explicitly gated*

### Phase 62.1: Reliable longitudinal memory closure (INSERTED)

**Goal:** Make the existing personal knowledge and conversation system retain, update, retrieve and apply evidence-bound memory across sessions, recover interrupted work, honor scope/corrections/revocation, and demonstrate useful behavior through actual product entrypoints.
**Depends on:** Phase 61, Phase 62
**Requirements:** MEM-01..08 in `phases/PDA-62.1-reliable-longitudinal-memory/62.1-CONTEXT.md`
**Status:** In progress; user authorized autonomous optimization on 2026-09-05. Historical phase acceptance does not prove this goal.
**Plans:** Incremental vertical slices, independently verified; original objective remains open until all requirements have current evidence.

- [ ] 62.1-01: Reliable committed-delta delivery and evidence-correct projection retrieval
- [ ] 62.1-02: Persistent candidate/review bindings and useful next-conversation context
- [ ] 62.1-03: Temporal correction, scoped defaults and user revocation across consumers
- [ ] 62.1-04: Live freshness reconciliation, longitudinal product acceptance and usefulness baseline
