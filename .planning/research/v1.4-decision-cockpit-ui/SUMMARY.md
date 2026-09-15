# Project Research Summary

**Project:** 个人数据分析项目  
**Domain:** 本地优先、受证据约束的个人决策驾驶舱  
**Researched:** 2026-07-22  
**Confidence:** HIGH

## Executive Summary

v1.4 不从零构建前端，也不扩展个人决策系统的事实权威。仓库已有 React/Vite Cockpit、版本化只读 Projection、REST `/app` 托管和 v1.3 guarded orchestration；它们目前仍是未正式纳入里程碑的实现基线，必须先接受版本、契约与安全审查，不能提前视为已交付。

推荐路径是把已有能力按风险边界产品化：先收口 Projection DTO、同源 transport、安全错误和审计基线；再展示 Personal/External/Evidence 的真实状态；随后将低风险 `project` 决策工作区接入 exact preview/confirm/replay；最后形成反馈闭环并通过真实浏览器 UAT。Personal Knowledge Wiki 保留为依赖 v1.4 结果的 v1.5 候选，不进入本里程碑。

最大风险不是视觉实现，而是将浏览器变成第二个 authority，或让 wildcard CORS 与确认链暴露本地 mutation。v1.4 必须保持“程序和 authority 决定事实与边界，UI 只呈现和受控发起，用户始终显式确认”。

## Key Findings

### Recommended Stack

复用 React 18、TypeScript、Vite、Tailwind、TanStack Query、Zod 与 Python REST。生产构建继续由 REST `/app` 托管；不引入 Next.js、Redux、客户端数据库或新的 Node 常驻服务。

**Core technologies:**

- React/Vite：已有可用 UI 基线和本地构建路径。
- `CockpitProjectionService`：五类 authority 的只读、snapshot-aware 聚合。
- Guarded Orchestration：所有低风险写入继续由 exact preview、HMAC、sequence 和 idempotency 保护。

### Expected Features

**Must have:**

- truthful 状态/External/证据展示，包含 freshness、partial、limitations 与 stable evidence link；
- 低风险项目决策的方案比较、exact preview、具体确认与 exact replay；
- Action→Outcome→non-causal Effectiveness→Calibration 反馈浏览；
- 同源安全、私密内容控制、响应式、无障碍与真实服务故障降级。

**Defer:**

- Personal Knowledge Wiki、Topic Page、backlinks、页面物化与 LLM Wiki 叙述；
- 新的高风险决策域、外部自动动作、自动 promotion 与浏览器离线缓存。

### Architecture Approach

浏览器只调用相对同源 `/ui/*` 和既有 guarded session endpoint。REST 负责隐私过滤、transport 和静态托管；Projection 负责只读权威聚合；authority 保存事实/状态/反馈；浏览器绝不直连 SQLite/Chroma、不得保存 shadow state 或决定 lifecycle。

### Critical Pitfalls

1. **WIP 被误报为完成** — 先建立 tracked baseline、真实 build/test 与 requirement mapping。
2. **wildcard CORS 暴露 local mutation** — Phase 36 先收口同源、origin rejection 与跨域无写入测试。
3. **Projection DTO 漂移** — 以 versioned DTO、Zod、真实 response fixture 和 contract test 收口。
4. **partial/stale/External 被伪装为确定事实** — 可见语义、snapshot gating 和阻写不可省略。
5. **证据与浏览器验收只做表面** — 不能以 iframe 或 Vitest 代替 current-evidence drill-down 与真实浏览器 UAT。

## Implications for Roadmap

### Phase 36: Projection and Secure Transport Baseline

**Rationale:** 先让客户端接收到的 DTO、错误和 transport 成为可信边界，才能安全展示或确认。  
**Delivers:** tracked baseline、Projection v1 DTO 收口、same-origin/CORS/security header、safe error envelope。  
**Avoids:** WIP 误报、跨域 mutation、字段漂移。

### Phase 37: Authority-aware State, External and Evidence

**Rationale:** 只读状态首先证明 Cockpit 能诚实表示事实、外部信息、partial 与证据。  
**Delivers:** Overview、Personal State、External、Evidence、System 的真实接线与可恢复降级。  
**Avoids:** External 污染 Personal、空 iframe、过期数据伪装当前。

### Phase 38: Guarded Decision Workspace

**Rationale:** 只有在 snapshot/evidence/read semantics 已可信后，才让用户通过 UI 发起 write flow。  
**Delivers:** 低风险 project workspace、prepare/preview/confirm/replay、typed recovery、refresh fail-closed。  
**Avoids:** 一键完成、重复写入、过期 preview。

### Phase 39: Feedback and Proactive Review

**Rationale:** 反馈只能基于已确认的决策链展示，不能提前创造新的自动化。  
**Delivers:** Actions、Outcomes、non-causal Effectiveness、Calibration、Proactive 的 truthful read surfaces。  
**Avoids:** 因果夸大、自动 promotion、假写入控件。

### Phase 40: Product Hardening and Live UAT

**Rationale:** 仅在端到端工作流完整后，响应式、无障碍、故障与隐私验收才有意义。  
**Delivers:** 真实浏览器 UAT、fault/degraded verification、release/rollback evidence。  
**Avoids:** “组件测试全绿但产品不可用”。

### Phase Ordering Rationale

- 五个阶段来自五个独立失败/回滚边界，不是固定五段模板。
- 每个 requirement 将只映射一个 phase；浏览器写入不和 transport 安全、证据真值混在同一验收单元。
- Wiki 需要 Cockpit 的 projection/evidence 基线，因此明确在 v1.5 后续规划。

## Confidence Assessment

| Area | Confidence | Notes |
|---|---|---|
| Stack | HIGH | `package.json`、Vite 和 REST 托管路径已存在。 |
| Features | HIGH | 用户确认 v1.4 先行、Wiki 后置，且候选 UI 契约完整。 |
| Architecture | HIGH | Service、REST、orchestration 和 contract tests 已明确边界。 |
| Pitfalls | HIGH | 多项问题可在当前代码中定位；CORS 是 P0。 |

**Overall confidence:** HIGH

### Gaps to Address

- Cockpit 核心文件未跟踪；正式执行前先建立审计基线。
- 未发现 Cockpit 专用浏览器 E2E；Phase 40 需确定工具并记录真实 UAT 证据。
- Proactive 的写入控制未暴露 REST，v1.4 应诚实只读/禁用而非补假按钮。
- Evidence 需补 current-object 的 Authority drill-down，不能只嵌入遗留 Widget。

## Sources

### Primary

- `apps/personal_decision_cockpit/` — 前端、路由、Zod、确认抽屉与测试。
- `src/personal_knowledge/services/ui_projection.py` — Projection contract 和只读限制。
- `src/personal_knowledge/services/api_server.py` — REST、静态托管与 transport surface。
- `src/personal_knowledge/intelligence/orchestration/` — v1.3 guarded write contract。
- `.planning/research/v1.4-decision-cockpit-ui/UI-SPEC.md` — 候选体验契约。

---
*Research completed: 2026-07-22*  
*Ready for requirements: yes*
