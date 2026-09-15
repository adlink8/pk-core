# Stack Research

**Domain:** 本地优先的个人决策 Web 驾驶舱  
**Researched:** 2026-07-22  
**Confidence:** HIGH（以当前仓库实现、契约和测试为依据）

## Recommended Stack

### Core Technologies

| Technology | Version / current use | Purpose | Why Recommended |
|---|---|---|---|
| React | 18.3.1 | 页面、组件和受控交互 | 已有 Cockpit 实现与测试；无需重写为其他框架。 |
| TypeScript | 5.7.x | 前端 DTO、状态和路由的静态约束 | 与后端投影的契约漂移可在构建与测试中尽早发现。 |
| Vite | 6.0.x | 本地开发与静态构建 | 开发期代理 `:5173 → :8000`，生产构建继续由 Python REST 的 `/app` 托管；不增加 Node 常驻服务。 |
| Python REST / `CockpitProjectionService` | existing | 只读 UI Projection 与受控写入入口 | 统一聚合 Personal、External、Decision、Proactive、Knowledge 权威，浏览器不接触 SQLite/Chroma。 |

### Supporting Libraries

| Library | Version / current use | Purpose | When to Use |
|---|---|---|---|
| TanStack Query | 5.62.x | 带 stale、retry 和 loading/error 语义的只读数据读取 | 调用 `/ui/*` 及既有只读 authority 接口；不是离线数据仓库。 |
| Zod | 3.24.x | 入站 DTO 校验 | 所有 Projection 和 orchestration 返回值在进入组件前校验。 |
| React Router | 6.30.x | 驾驶舱路由与工作流子页面 | 保持现有总览、状态、决策、行动、外部、提醒、证据、系统导航。 |
| Tailwind CSS | 3.4.x | 语义化视觉 token 与响应式布局 | 保持冷静分析型视觉；状态必须有文字和图标，不能只靠颜色。 |
| Vitest / Testing Library | 3.0.x / existing | 组件、路由、DTO 与错误恢复测试 | 每个 Projection DTO 与确认抽屉必须具有回归覆盖。 |

### Development Tools

| Tool | Purpose | Notes |
|---|---|---|
| `npm run test` | 前端单元与组件回归 | 当前已有 78 项前端测试；不能替代浏览器验收。 |
| `npm run build` | 类型检查与静态构建 | 会清除 `dist/` 再构建；`dist/` 是产物，不是审计证据。 |
| Python `pytest` | Projection、orchestration、隐私与 replay 契约 | 与前端分别验证，避免 UI 测试代替 authority 测试。 |
| 真实浏览器 UAT | 响应式、键盘、焦点、降级和隐私观测 | Phase 40 决定采用现有浏览器工具的脚本化验收或经评审后添加测试依赖。 |

## Installation

v1.4 P0 不应为重新搭建前端而新增框架或生产依赖。复用现有依赖与服务：

```powershell
Set-Location <repo-root>\apps\personal_decision_cockpit
npm run test
npm run build

Set-Location <repo-root>
$env:PYTHONPATH = "$PWD\src"
python -m pytest tests/contract/test_ui_projection.py tests/contract/test_ui_projection_state_external.py tests/contract/test_ui_projection_decision.py tests/contract/test_ui_projection_actions_proactive.py -q
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|---|---|---|
| React + Vite + `/app` static hosting | Next.js / SSR | 仅在未来需要多租户、公共 SEO、服务端会话渲染时重评估；本地单用户 Cockpit 当前不需要。 |
| Server-owned UI Projection | 浏览器拼接多个 authority | 永不用于本产品；会复制真值、生命周期和风险规则。 |
| TanStack Query 内存缓存 | `localStorage` 持久化决策数据 | 只有先设计加密、过期、撤销和隐私清除策略后才可评估；v1.4 不做。 |
| 真实浏览器验收 | 只依赖组件截图或 Vitest | 不可替代；浏览器级故障、焦点和响应式必须真实验证。 |

## What NOT to Use

| Avoid | Why | Use Instead |
|---|---|---|
| 新的前端 SSOT / 客户端数据库 | 会与 Personal/External/Decision 权威漂移 | 只读 `decision_cockpit_projection_v1` 与既有 authority 接口。 |
| Redux 或全局业务规则层 | 单用户、服务端 authority 已处理业务规则；会引入重复裁决 | 组件局部状态 + TanStack Query。 |
| Service Worker 持久缓存个人正文 | 易泄露、过期且会把旧事实伪装成当前 | 明确 offline/recovery 状态；不离线保存业务数据。 |
| iframe 静默失败 | MCP 停止时用户看到空白证据页 | 受限 iframe + 可见 degraded state + 可恢复提示。 |

## Version Compatibility

| Package A | Compatible With | Notes |
|---|---|---|
| React 18.3.x | React Router 6.30.x / TanStack Query 5.62.x | 已在工作区 Cockpit 使用。 |
| TypeScript 5.7.x | Vite 6.0.x / `@vitejs/plugin-react` 4.3.x | `npm run build` 先执行 `tsc --noEmit`。 |
| Projection v1 | Zod inbound schemas | 破坏性 DTO 变化必须显式版本化或提供兼容字段，不能依赖页面的宽松猜测。 |

## Sources

- `apps/personal_decision_cockpit/package.json` — 当前前端依赖与脚本。
- `apps/personal_decision_cockpit/vite.config.ts` — 开发代理与生产托管边界。
- `src/personal_knowledge/services/ui_projection.py` — Projection-only 约束。
- `.planning/research/v1.4-decision-cockpit-ui/UI-SPEC.md` — 既有候选视觉与交互契约。

---
*Stack research for: v1.4 Decision Cockpit UI*  
*Researched: 2026-07-22*
