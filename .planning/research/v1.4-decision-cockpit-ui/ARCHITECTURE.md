# Architecture Research

**Domain:** 本地个人决策系统的 Web 驾驶舱  
**Researched:** 2026-07-22  
**Confidence:** HIGH（源自现有 Service/REST/前端实现与契约测试）

## Standard Architecture

### System Overview

```text
┌──────────────────────────────────────────────────────────────┐
│ Personal Decision Cockpit (React / TypeScript / Vite)         │
│ Overview · State · Decisions · Actions · External · Evidence  │
└───────────────────────┬──────────────────────────────────────┘
                        │ same-origin HTTPS/HTTP only
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ Python REST (`rag-api`)                                       │
│ /app static host · /ui/* read projections · guarded sessions │
│ privacy guard · typed safe errors · security headers          │
└───────────────┬───────────────────────────┬──────────────────┘
                │ read-only                 │ exact guarded write
                ▼                           ▼
┌──────────────────────────┐   ┌────────────────────────────────┐
│ CockpitProjectionService │   │ Guarded Orchestration Service   │
│ v1 envelope / partial    │   │ preview → confirm → replay     │
└───────┬──────────────────┘   └────────────┬───────────────────┘
        │                                    │
        ▼                                    ▼
┌──────────────────────────────────────────────────────────────┐
│ Existing Authorities                                           │
│ Personal · External · Decision/Pilot · Proactive · Knowledge   │
│ immutable snapshots / evidence / append-only events            │
└──────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Must Not Do |
|---|---|---|
| Cockpit UI | 呈现状态、收集显式用户意图、显示 preview/replay/recovery | 直连数据库、裁决 lifecycle、保存新的事实或跨域确认。 |
| `CockpitProjectionService` | 只读聚合五类 authority，返回版本化 DTO、snapshot、freshness、partial 和 limitations | 写任何 authority、调 Provider、自动 promotion。 |
| REST server | 同源路由、静态 `/app`、隐私过滤、受控 session 转发 | 把 `/health` 说成所有下游健康、接受任意 origin mutation。 |
| Guarded Orchestration | exact preview、HMAC confirmation、sequence、idempotency、replay 和 fail-closed | 自动重试 provider 未知结果、执行高风险或外部动作。 |
| Authorities | 持有事实、外部信息、决策、反馈、证据和生命周期 | 被 UI Projection 覆盖或由浏览器复写。 |

## Recommended Project Structure

```text
apps/personal_decision_cockpit/
├── src/app/                 # shell, routes, navigation
├── src/api/                 # relative REST client, Zod schemas, query hooks
├── src/pages/               # domain pages; no authority aggregation
├── src/components/          # cards, drawers, recovery and accessibility primitives
├── src/design-system/       # semantic tokens and responsive primitives
└── docs/                    # UI-specific write-flow contract

src/personal_knowledge/services/
├── api_server.py            # REST routes, static /app, transport/security boundary
└── ui_projection.py         # versioned read-only CockpitProjectionService
```

### Structure Rationale

- **UI/API separation:** 页面使用已经校验的 DTO；不能在多个页面各自拼装 authority。
- **Projection/service separation:** 后端知道 authority、snapshot、privacy 和 partial；页面只解释其结果。
- **Existing orchestration reuse:** 写入不另造 REST 逻辑，继续复用 v1.3 exact-confirmation 契约。

## Architectural Patterns

### Pattern 1: Versioned, projection-only read envelope

**What:** 所有 Cockpit 读取通过 `decision_cockpit_projection_v1`，含 `snapshot_bindings`、`freshness`、`partial` 和 `limitations`。

**When to use:** 总览、状态、External、决策列表、行动、主动提醒、校准和系统页面。

**Trade-offs:** 减少 UI 灵活拼接，换取真值与错误语义一致；新增页面需要先补 projection，不可直接读取 SQLite。

### Pattern 2: Server-owned guarded mutation

**What:** `prepare → exact preview → explicit confirm → commit/replay`；前端只持有短期会话数据。

**When to use:** v1.4 唯一允许的低风险 `project` 域 session。

**Trade-offs:** 多一步确认和 refresh 后 fail-closed；换取无重复事件、可审计用户主权和安全恢复。

### Pattern 3: Truthful partial/degraded rendering

**What:** 单 authority、MCP Widget 或 Chroma 失败时，返回受影响部分的 `partial`、limitations 和恢复动作；其余安全读取仍可显示。

**When to use:** 所有组合视图与证据入口。

**Trade-offs:** 页面比“永远成功”的 UI 更复杂；但不会把未知、过期或旧结果假装成当前事实。

## Data Flow

### Read Flow

```text
用户打开页面
  → React Query relative `/ui/*`
  → REST privacy guard + route
  → CockpitProjectionService read-only aggregation
  → Authority/snapshot/evidence metadata
  → Zod validation
  → semantic card / partial / recovery UI
```

### Guarded Write Flow

```text
用户选择低风险 project 决策
  → prepare（不写入）
  → exact preview + checksum + sequence + idempotency key
  → 具体确认文案
  → confirm
  → append-only event / exact replay
  → UI 展示 receipt、replayed 或 typed recovery
```

### Key Data Flows

1. **State/External evidence:** UI 显示 snapshot binding、freshness、authority ID 和 evidence link；证据详情只读下钻。
2. **Decision feedback:** Recommendation → Decision → Action → Outcome → non-causal Effectiveness → Calibration 只读浏览现有 append-only 链。
3. **Health:** REST/MCP/Tunnel/Chroma/authority freshness 各自表示；REST 健康不能替代 Chroma 或检索健康。

## Anti-Patterns

### Browser-owned fact aggregation

**What people do:** 前端直接请求多个数据库/接口，然后在 JavaScript 中定义 current、risk 或 lifecycle。

**Why it's wrong:** 会造出影子 SSOT，并让页面之间对 snapshot、冲突和隐私作出不同结论。

**Do this instead:** 只接收 `CockpitProjectionService` 的 versioned envelope；业务裁决仍在 authority。

### UI-only confirmation

**What people do:** 点击“确认”即认为安全，或让浏览器生成长期可复用的身份/令牌。

**Why it's wrong:** 不防重放、页面刷新或跨域调用，且模糊认证和显式确认。

**Do this instead:** 服务端 exact preview binding、HMAC、sequence、idempotency 与同源 transport 共同执行。

## Integration Points

| Boundary | Communication | Notes |
|---|---|---|
| Cockpit ↔ REST | 相对 URL、同源 `/app` | v1.4 必须移除 production wildcard CORS；开发期只能显式 allowlist。 |
| REST ↔ Projection | Python service calls | Projection 只读；维持 SQLite `mode=ro` / `query_only`。 |
| Cockpit ↔ MCP Widgets | 受限嵌入或已有浏览入口 | MCP 不可用时提供 non-empty degraded state；iframe 需 CSP、sandbox/referrer policy。 |
| Cockpit ↔ Orchestration | 既有 session endpoint | 当前仅 `project + low`，不得扩域。 |

## Sources

- `src/personal_knowledge/services/ui_projection.py`。
- `src/personal_knowledge/services/api_server.py`。
- `src/personal_knowledge/intelligence/orchestration/service.py`。
- `apps/personal_decision_cockpit/src/api/orchestration.ts`。
- `apps/personal_decision_cockpit/docs/write-flow.md`。

---
*Architecture research for: v1.4 Decision Cockpit UI*  
*Researched: 2026-07-22*
