# 个人数据分析项目

## What This Is

这是一个 Windows 本地优先的**个人决策智能系统**。它把 Google、GPT、Agent 与 AgentView 等长期个人数据转换为可持续追加、可查询、可追溯的个人知识与状态，并以独立 External Context Authority 接入受控公共事实，通过受控 LLM 与确定性规则形成个性化决策建议、行动结果和反馈校准。系统同时提供 CLI、REST、MCP、可视化与 RAG 消费接口。

项目状态分析只是 project 域的一个输入能力，不是产品最终目标。系统默认不替用户执行外部动作，用户保留价值选择、风险接受与最终决策权。

## Core Value

以长期个人数据为内部状态、以外部社会环境为外部状态，在隐私安全、证据可回查和不确定性可解释的前提下，为用户提供可验证、可反馈、可持续校准的个人决策支持。

## Long-term Product Target

权威目标说明：[`.planning/audits/PERSONAL-DECISION-INTELLIGENCE-VISION-STATUS-2026-07-18.md`](audits/PERSONAL-DECISION-INTELLIGENCE-VISION-STATUS-2026-07-18.md)。

当前预期目标差距：[`.planning/audits/TARGET-GAP-ANALYSIS-2026-07-18.md`](audits/TARGET-GAP-ANALYSIS-2026-07-18.md)。

v1.4 前端产品契约：[`UI-SPEC.md`](research/v1.4-decision-cockpit-ui/UI-SPEC.md)。

```text
长期个人数据 + 当前个人状态 + 外部环境 + 历史决策结果
→ 状态与变化建模
→ 决策案例与多方案比较
→ LLM 决策分析候选
→ 用户确认、行动与结果
→ 后验评估与建议校准
```

LLM 输出是 Recommendation Candidate，不是个人事实、最终决策或执行权限。

## Current Milestone: v2.0 Pi Personal Intelligence Capability OS — Scope Expanded 2026-08-05

**Goal:** 将 `@earendil-works/pi` 彻底嵌入为唯一主 AI Runtime，并把项目已有能力统一注册为 Domain Tools、把稳定业务流程沉淀为 Skills，使用户请求、数据 Delta、调度任务、底仓维护、模型调用、Skill/Tool 调度、Session 和流式交互进入统一事件驱动闭环；legacy Agent 只保留为有界回滚路径。

Pi 接管 AI 控制面并可通过显式、参数化、事务化的 Domain Tools 操作底仓流程，但不接管事实权威。Python 确定性核心继续独占 canonical facts、evidence、watermark、evaluation、promotion、active pointer、rollback 和正式生命周期规则。Pi 不得直连 authority store、执行任意 SQL/路径/callable 或绕过 gate；所有生成物先进入隔离的 Session/Candidate staging，正式写入必须经过 plan → dry-run → exact preview → confirm/policy → execute → verify → receipt/rollback。

同一份 Project Capability Registry 是 REST、MCP 与 Pi SDK Kernel 的能力 SSOT。Pi SDK Kernel 是唯一 AI 协调内核，并使用 deny-by-default 的 production profile；不接入本机 Pi Agent，不建立第二套 Agent、RPC operator 或平级控制面。

当前 `@earendil-works/pi-*` 官方版本为 `0.83.0`，本机 Node 版本满足 `>=22.19.0`。隔离 Spike 已验证核心协议可行，但依赖审计仍有 2 High + 1 Moderate，真实 Provider/legacy baseline、浏览器 UAT 和 feature-flag rollback 尚未闭合；这些项目是 v2.0 首阶段的阻断条件，不得被“彻底嵌入”目标绕过。

正式研究与证据基线见 [`.planning/spikes/pi-embedded-personal-kernel`](spikes/pi-embedded-personal-kernel/README.md)、[`.planning/spikes/pi-package-qualification`](spikes/pi-package-qualification/README.md) 和 [`.planning/spikes/pi-frontier-controls`](spikes/pi-frontier-controls/README.md)。

## Previous Milestone: v1.5 Personal Knowledge Wiki Projection — P0 Accepted

v1.5 Wiki 的 Phase 44–47 已完成 P0 只读实现和授权真实浏览器 UAT；扩域决策保持 DEFER。Wiki 继续作为 canonical/KU/Personal State/External/Decision authority 的确定性投影，不创建新的事实 SSOT，不把页面内容写回 KU、Chroma 或检索索引。

## Previous Milestone: v1.4 Decision Cockpit UI — User UAT Accepted

**Goal:** 把已验证的 Personal State、External、Decision、Action/Outcome、Proactive、Evidence 与 Guarded Orchestration 变成可每日使用、可追溯且不突破用户主权的本地 Web 驾驶舱。Phase 40 UAT 已由用户确认通过；详细证据见 `phases/PDA-40-product-hardening-and-live-uat/40-UAT.md`。

**Target features:**

- 版本化、只读且同源安全的 Cockpit Projection 与静态 `/app`；
- 当前状态、External、证据、决策、反馈、主动提醒与系统状态的真实 UI；
- 仅限低风险 `project` 域的 `prepare → exact preview → confirm → replay`；
- 真实浏览器、无障碍、降级和隐私验收。

正式需求见 [`REQUIREMENTS.md`](./REQUIREMENTS.md)，研究结论见 [`research/v1.4-decision-cockpit-ui/SUMMARY.md`](./research/v1.4-decision-cockpit-ui/SUMMARY.md)。

## Previous Milestone: v1.3 Agent Productization — Shipped

**Shipped:** 2026-07-18. 独立审计通过 13/13 requirements、12/12 integrations、4/4 flows。

**Target features:**
- External、Analysis、Pilot、Calibration 的共享只读 Service、REST 和 MCP 工具
- `prepare → confirm → generate → decide → observe → calibrate` 受控 Agent 会话编排
- 紧凑的 Agent 响应、方案比较、限制说明和 evidence drill-down
- REST/MCP/Tunnel 一键启动、健康恢复和真实 ChatGPT MCP E2E
- 所有写入均需显式确认；保持零未授权外部动作和零自动策略 promotion

**Previous:** v1.2 shipped 2026-07-18（Phases 28–31），完成 External Authority、真实 `gpt-5.4` 决策链和诚实 `INCONCLUSIVE` 校准。见 [v1.2 audit](milestones/v1.2-MILESTONE-AUDIT.md)。

## Historical v1.5 Direction

**Personal Knowledge Wiki Projection** 已在 v1.5 完成 P0 只读实现与授权 UAT。规格见 [`SPEC.md`](future-milestones/v1.5-personal-knowledge-wiki-projection/SPEC.md)；learning/career 扩域和更大 calibration cohort 仍按 v1.5 expansion decision 后置，不并入 v2.0 Kernel 改造。

## Current Reality — 2026-07-19

- Phase 32 已完成：External、Analysis、Pilot、Calibration 具备统一 checksum-verifying Service/REST/stdio MCP/ChatGPT HTTP MCP 的 list/get/explain 读面；52 项阶段与相邻回归测试通过，四个 live authority 数据库读取前后指纹一致。
- Phase 33 已完成：低风险 project 会话具备 snapshot-bound prepare、逐步显式确认、at-most-once generation、immutable Pilot/Calibration bridges，以及 REST/stdio MCP/ChatGPT MCP 真实传输；40 项 Python/Node 阶段回归通过。
- Phase 34 已完成：统一 16 KiB compact envelope、稳定 IDs/limits/next actions/evidence links 和 typed recovery contract。
- Phase 35 完成并通过独立审计：生产安全的一键 REST/MCP/tunnel supervisor、44-tool descriptor 快照、真实 ChatGPT Web/Data connector read/explain 与 confirmed exact replay；五权威库指纹不变，provider/external/promotion 均为零。
- Phase 28–31 已完成：External Authority、双快照结构化 LLM 分析、project 真实决策链和成对校准均可校验重建。
- Phase 29 通过现有 ChatGPT 登录完成一次真实 `gpt-5.4` 分析；Phase 31 严格执行两臂各一次调用、零重试、零费用。
- Phase 30 有一条 11-event 真实 Recommendation→Decision→Action→Outcome 主链及一条 defer 控制链；系统外部动作数为零。
- Phase 31 个性化相对 generic 的效果结论为 **INCONCLUSIVE**：样本 1 小于最小 2、generic 缺少真实 outcome、实际 token 超出预注册预算；`causal_claim=false`。
- v1.2 需求 PDI-01..08 全部完成，里程碑审计通过；后续扩样或扩域必须重新预注册并保持无自动 promotion。

- Phase 23 已完成复合 SSOT、D/S/R/A registry、Serving Snapshot、证据下钻和 fail-closed Doctor。
- Phase 24 已闭环：最终 Recall@5 提升 **10.4478pp**，置信下界 **+4.4776pp**；真实 lifecycle ledger 有 6 个事件、2 个 applied manifests。
- Phase 25–27 Live schemas 已应用，Personal State、Decision Feedback、Proactive Intelligence 各有 1 个真实 committed run，并通过 exact snapshot/checksum 验证。
- 真实链路已覆盖状态、建议、用户确认、行动、结果、非因果效果评估、主动候选以及 suppress/restore。
- `src/personal_knowledge/intelligence` 已有受控 LLM candidate 路径；所有事实、隐私、冲突与风险门仍由确定性规则执行。
- External Context Authority 当前限定两个 allowlisted 公共来源；Google Activities 仍属于个人行为数据，不属于外部社会情报。
- Technical Target D、真实低风险数据链、External Context、LLM 决策分析与 Product UAT 均已通过；比较效果仍需更大真实 cohort。

## Requirements

### Validated (v1.0)

- ✓ 三源增量导入、统一事件、增强与去重管道 — Phase 01-03
- ✓ 结构化记忆、记忆图谱和语义候选层 — Phase 04-10（实验层保留，非知识 SSOT）
- ✓ MCP、Apps SDK、REST 与统一数据访问接口 — Phase 11-12
- ✓ 公共基础层重构和 canonical AgentView 会话证据层 — Phase 13-13.5
- ✓ 知识单元 RAG：30,012 active + KU-01..08 增量 journal/watermark — Phase 14
- ✓ 检索三层 SSOT + layered hybrid + telemetry + holdout — Phase 15
- ✓ Google 轻量结构化 lifecycle + RO consumer — Phase 16
- ✓ 工程结构重整：闲置模块 `_recycle/`；scripts 领域分包 + 兼容 shim
- ✓ Phase 08 取消（MEMX-01 wontfix）

### Validated (v1.2)

- ✓ 独立 External Context Authority、bounded import、生命周期和可逆快照 — PDI-01..04
- ✓ 双快照证据绑定的结构化 LLM Decision Analysis Candidate — PDI-05..06
- ✓ 低风险 project 真实主链、defer 控制链和非因果 outcome — PDI-07
- ✓ 预注册 personalized/generic 对照与诚实 INCONCLUSIVE 边界 — PDI-08

### Validated (v1.3)

- ✓ 四类决策权威的 checksum-verifying Agent read/explain 契约 — AGENT-01..04
- ✓ 显式确认、幂等、可恢复且 fail-closed 的低风险编排 — ORCH-01..04
- ✓ 紧凑 Agent envelope、证据下钻和 typed recovery — UX-01..02
- ✓ 生产安全一键运行与真实 readiness — LIVE-01
- ✓ 真实 ChatGPT/tunnel ingress read/explain 与 confirmed exact replay — LIVE-02
- ✓ 44-tool descriptor/contract 快照与 live 本地 MCP parity — LIVE-03

### Active (v1.4 Decision Cockpit UI)

- [ ] Projection-only、同源安全、safe error 与可审计基线 — CCK-01..04
- [ ] 当前状态、External、snapshot/evidence 与 truthful degraded UI — STATE-01..03、EVID-01
- [ ] 低风险 project 决策工作区与 explicit confirm/exact replay — DEC-01..03
- [ ] Action/Outcome/Calibration/Proactive 与 runtime truthfulness — FDB-01..02、RUN-01
- [ ] 响应式、无障碍、隐私和真实浏览器验收 — UX-01..02、QA-01..02

### Optional backlog (not Active)

- 真实源增量付费 promote；非主路径测试覆盖补齐  
  — 见 ROADMAP § Optional Next

### Out of Scope

- 直接修改 AgentView live database — 它只作为只读上游
- 删除 raw events、legacy 数据库或旧 Chroma collection — 必须保留回滚与追溯
- 让 LLM 输出绕过 schema、evidence、privacy 和 evaluation gate — 所有 AI 产物先进入 staging
- 在核心管道引入大型 Agent/RAG 编排框架 — 优先使用现有 Python、SQLite 和轻量接口
- 硬删除 `_recycle/` 归档（仅软归档；恢复靠 MANIFEST）
- 在 Cockpit 中新建或复制 Personal/External/Decision 事实权威 — 只允许版本化只读 Projection
- Personal Knowledge Wiki、Topic Page、backlinks 或 LLM Wiki 叙述 — 明确后置至 v1.5 候选
- 健康、财务、关系等高风险写入，或任何自动外部动作/promotion — v1.4 仅限既有 `project + low` 受控路径

## Context

- 项目根目录默认运行环境是 Windows + PowerShell（工作区可为 `<repo-root>` 等）。
- **源码布局（Phase 19–21）：** `src/personal_knowledge/`  
  - `core/` 基础（含 `llm.py`）  
  - `domains/*/` 规则/模型/常量 + facade（清理窗口 2026-08-13）  
  - `application/*/` **canonical build/lifecycle**  
  - `evaluation/*/` **canonical eval**（含 `evaluation/vector/`）  
  - `retrieval/` 向量/检索 I/O；`services/` REST/MCP  
- **产品同步入口（2026-07-16）：** `pk-sync conversations [--write]`（AgentsView→canonical）。  
  旧 `rag-pipeline` 统合 1–12 步已退役（仅取证：`PK_ALLOW_LEGACY_PIPELINE` + `--legacy-integrated`）。  
  **KU：** 日常仅增量（`docs/runbooks/ku-incremental.md`）；禁止把全量 `build_knowledge_inventory`+`prod --start` 当对话同步后的默认步骤。  
  Agent 全流程见 `docs/AGENTS.md`。  
- **数据/运行时（Phase 20）：** `data/`、`var/`、`archive/`；AgentsView live 仍为 protected-external。
- 核心统合库：`var/db/personal_system.sqlite`（非对话 PE 过渡层）；对话 SSOT：`data/canonical/agent/structured/db/agent_conversations.sqlite`。
- 当前 active 知识索引：`knowledge_units_ir_4cd8af4ad_20260718054940`；当前复合 serving snapshot：`ss_5d816a6bf3ebd0bce9463236`。
- 向量模型：`bge-small-zh-v1.5`（512d）— 当前数据量无需更换。
- KU 抽取：L1 **1 message / 1 call** + L2 **session 窗二次抽取**（已并入 canonical/active）。

## Constraints

- **Privacy**: thinking、PII、原始 tool input/result 和 secret-bearing 正文不得进入规范化、知识或向量层。
- **Evidence**: 知识、记忆与回答必须能回查 source session/message/event。
- **Publication**: 新数据库、知识版本和向量 collection 必须 staging → gate → atomic promote，并可 rollback。
- **Compatibility**: 不破坏现有 CLI、REST、MCP 和 12 步数据管道契约（shim 保证旧入口可用）。

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| SQLite 作为结构化事实和 lineage 主存储 | 本地、可审计、容易备份与回滚 | ✓ Good |
| Chroma 只作为检索特征空间 | 避免把向量库误当作事实源 | ✓ Good |
| AgentView 只读快照后再规范化 | 避免污染正在写入的 live DB | ✓ Good |
| canonical conversation 是 Phase 14 的会话证据入口 | 消除 legacy/AgentView 双计数 | ✓ Good |
| evaluation-first knowledge unit RAG | 用冻结测试和 A/B 决定是否发布 | ✓ Good |
| 闲置模块软归档到 `_recycle/` 而非删除 | 可回滚、主树更清晰 | ✓ Good |
| scripts 按领域分包 + 根 shim | 可读性与旧命令兼容 | ✓ Good |
| 三层 SSOT + layered hybrid | 避免 personal_events 冒充全量对话 | ✓ Phase 15 |
| Google 不进对话 KU；light assert 隐私 = service+category | 日志≠断言；地点/支付不进兴趣断言 | ✓ Phase 16 |
| domains 瘦身：build→application / eval→evaluation + core.llm | 消除跨域 hub；facade 30 天窗口 | ✓ Phase 21 |
| Phase 08 memory 融合取消 | KU 已是知识 SSOT | ✓ Cancelled |
| KU 抽取 = message-level | 证据可回查、可增量、可并行 | ✓ Locked (v1.0) |
| L2 是 L1 的跨轮补强，不是独立事实源 | 保留 message 证据边界，并单独测量跨轮净增益 | Planned (v1.1) |
| 所有 KU candidate 必须先通过统一评测门 | 防止“数量增加但质量下降”仍被 promote | Planned (v1.1) |
| 评测使用现有 Python + SQLite/JSON/HTML | 数据私密、本地优先，避免引入重型外部平台 | Planned (v1.1) |
| External 与 Personal 权威物理、语义和权限隔离 | 防止公共事实污染个人事实 | ✓ Good (v1.2) |
| LLM 输出永远是 candidate，事实与安全门确定性执行 | 保留证据边界和用户最终决策权 | ✓ Good (v1.2) |
| 样本不足、缺测或协议偏离必须 INCONCLUSIVE | 防止把单次演示包装为个性化因果增益 | ✓ Good (v1.2) |
| 校准 proposal 不自动 promotion | 策略变更必须独立评审并可回滚 | ✓ Good (v1.2) |
| Agent 写入只接受 exact preview + explicit confirmation + idempotency | 保留用户最终权力并使网络重试安全 | ✓ Good (v1.3) |
| MCP 默认使用 compact envelope，完整证据显式下钻 | 控制上下文、隐私与恢复语义 | ✓ Good (v1.3) |
| Runtime 只管理可证明 owned 的进程 | 防止端口冲突时误杀用户进程 | ✓ Good (v1.3) |
| Cockpit 只消费 server-owned Projection | 防止浏览器形成影子 SSOT、复制生命周期/风险裁决 | — Pending (v1.4) |
| Cockpit mutation 仅限 same-origin `project + low` | wildcard CORS 与 UI 确认不能替代 transport security | — Pending (v1.4 P0) |

## Evolution

本文件在阶段转换和里程碑边界持续更新。每次阶段完成时核对需求、关键决策、范围和真实运行状态；每次里程碑结束时重新检查 Core Value、Out of Scope 与已验证能力。

---
*Last updated: 2026-07-22 after v1.4 requirements definition*
