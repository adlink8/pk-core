# Feature Research

**Domain:** 受证据约束的个人决策驾驶舱  
**Researched:** 2026-07-22  
**Confidence:** HIGH（当前产品能力和用户确认范围已明确）

## Feature Landscape

### Table Stakes (Users Expect These)

| Feature | Why Expected | Complexity | Notes |
|---|---|---|---|
| 当前状态总览 | 用户需要先知道目标、约束、风险、待决策项和数据新鲜度 | MEDIUM | 默认展示 current；历史、冲突、partial 和 stale 必须醒目。 |
| 可解释决策工作区 | 多方案、证据、假设、风险与停止条件必须可比较 | HIGH | 不能只给单一评分或一句模型建议。 |
| 证据与快照下钻 | 高层结论必须能返回权威 ID、snapshot、来源和限制 | HIGH | `EvidencePage` 不能仅靠与当前对象无关的 Widget iframe。 |
| 明确故障与降级 | REST、MCP、Chroma 或单 authority 不可用时必须诚实显示 | MEDIUM | 不能空白、不能将旧缓存说成当前结果。 |
| 响应式与键盘访问 | 驾驶舱必须在桌面与手机摘要场景下可读、可操作 | MEDIUM | 320/768/1024/1440、200% 缩放、reduced motion、长中文与长 ID。 |

### Differentiators (Competitive Advantage)

| Feature | Value Proposition | Complexity | Notes |
|---|---|---|---|
| Projection-only 驾驶舱 | UI 不复制事实和裁决，始终绑定五类权威 | HIGH | 是本项目与普通“AI dashboard”最重要的差异。 |
| Snapshot-bound 决策 | 每次建议展示 personal/external binding、freshness 和 limitations | HIGH | 漂移、冲突、过期或证据不足时阻写。 |
| `prepare → preview → confirm → exact replay` | 用户确认有原样预览、幂等与审计回执 | HIGH | 只允许 `project + low` 写入试点；不扩域。 |
| 非因果反馈时间线 | 决策、行动、结果、效果与校准可连续浏览 | MEDIUM | `causal_claim=false` 不能被视觉文案弱化。 |
| 主动提醒的用户主权 | 建议可被查看、抑制、恢复，但权限与实际 API 保持一致 | MEDIUM | v1.4 不为了 UI 完整感新增未授权写入端点。 |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---|---|---|---|
| 个人 Wiki / Topic Pages | 希望浏览稳定知识与历史 | 需要独立 materialization、staleness、页面生命周期与证据策略；会稀释 Cockpit 验收 | 列为 v1.5 候选，依赖 v1.4 Projection 和 evidence 基线。 |
| 人生总分/单一建议分数 | 看起来直观 | 会伪造精确性、掩盖风险与多目标冲突 | 方案比较 + 假设、限制、风险和停止条件。 |
| 一键接受并执行 | 缩短流程 | 绕过 preview、确认、风险和用户主权 | 每一写步具体文案确认，保留 exact replay。 |
| 在浏览器持久化个人正文 | 看起来支持离线 | 增加隐私泄露、撤销和过期风险 | 显式离线状态；后续单独设计加密缓存。 |
| 将 External 自动写成 Personal Fact | 看起来让状态“自动更新” | 混淆权威，可能错误改变个人判断 | 保持物理、语义和权限隔离。 |

## Feature Dependencies

```text
安全的同源 Projection / DTO
    └──requires──> 当前状态、External 与证据展示
                           └──requires──> 决策工作区与 guarded confirmation
                                                          └──requires──> Action/Outcome/Calibration 反馈

证据与状态语义 ──enhances──> 所有页面与真实 UAT
Wiki Projection ──depends on──> Cockpit 的稳定 Projection / evidence 基线（v1.5）
```

### Dependency Notes

- **展示要求依赖 Projection DTO：** External 字段、Now Stack 分类、主动优先级键未对齐前，页面不能被视为可信。
- **任何写入依赖安全传输与 snapshot：** 先移除 wildcard CORS、限制同源 mutation，再开放浏览器 confirm。
- **反馈依赖前一阶段的真实决策：** Action/Outcome/Calibration 只能浏览已有 append-only 链，不创建新的自动化。
- **Wiki 依赖 Cockpit：** Wiki 必须复用而非绕过 v1.4 的投影、证据和 stale 语义。

## MVP Definition

### Launch With (v1.4)

- [ ] Projection-only、同源安全、safe error envelope — 驾驶舱不能成为新的真值或跨域 mutation 面。
- [ ] 总览、个人状态、External、Evidence 的 truthful read UI — 用户能看清状态、来源、限制和故障。
- [ ] 低风险项目决策 workspace 与 exact preview/confirm/replay — 将已有编排变为可审计 UI。
- [ ] Action/Outcome/Calibration 与 Proactive 的只读反馈闭环 — 用户能复盘但系统不自动推广策略。
- [ ] 浏览器 UAT、无障碍和服务故障降级 — 证明产品能在真实环境被使用。

### Add After Validation (v1.x)

- [ ] Personal Knowledge Wiki Projection — 仅在 Cockpit Projection/evidence 通过真实验收后启动。
- [ ] Proactive Snooze/Suppress/Restore UI 写入 — 仅在 REST 写接口、确认和审计契约明确授权后加入。
- [ ] 受控本地加密离线缓存 — 仅在完成隐私、撤销与 staleness 设计后评估。

### Future Consideration (v2+)

- [ ] learning/career 等新写入领域 — 需要新的风险预算、真实试点与预注册校准。
- [ ] 自动外部动作、自动 promotion 或“自动做决定” — 与用户主权和现有产品边界冲突。
- [ ] 通用知识百科编辑器 — 不属于当前个人决策产品的核心闭环。

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---|---|---|---|
| Projection/transport baseline 与 DTO 收口 | HIGH | MEDIUM | P1 |
| 状态、External、证据和真实降级 | HIGH | HIGH | P1 |
| Guarded Decision Workspace | HIGH | HIGH | P1 |
| Action/Outcome/Calibration/Proactive 浏览 | HIGH | MEDIUM | P1 |
| 浏览器 UAT、a11y、响应式 | HIGH | MEDIUM | P1 |
| Personal Wiki Projection | MEDIUM-HIGH | HIGH | P2（v1.5） |

## Sources

- 用户已确认：先正式化 v1.4 Cockpit，Wiki 作为后续 v1.5。
- `.planning/research/v1.4-decision-cockpit-ui/UI-SPEC.md`。
- `.planning/future-milestones/v1.5-personal-knowledge-wiki-projection/SPEC.md`。
- `apps/personal_decision_cockpit/src/app/router.tsx`。

---
*Feature research for: v1.4 Decision Cockpit UI*  
*Researched: 2026-07-22*
