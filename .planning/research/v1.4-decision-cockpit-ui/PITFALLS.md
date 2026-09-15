# Pitfalls Research

**Domain:** 个人决策 Cockpit 的安全、治理与产品验收  
**Researched:** 2026-07-22  
**Confidence:** HIGH（当前代码审阅、契约与既有验证约束）

## Critical Pitfalls

### Pitfall 1: 未跟踪 WIP 被宣称为已交付

**What goes wrong:** Cockpit、Projection、UI tests 和候选规格已经存在，但未进入 Git 且未完成真实浏览器 UAT；README 却可能写成已完成。

**Why it happens:** “代码能运行”被误当成“版本可审计、产品已验收”。

**How to avoid:** Phase 36 先建立可审计基线、构建/测试证据和实际 requirement mapping；文件进入版本控制后才可计算完成度。

**Warning signs:** `git status` 显示核心 Cockpit 目录和 Projection tests 未跟踪，或文档声称 Phase 36–40 complete 但 ROADMAP 无活动里程碑。

**Phase to address:** Phase 36。

---

### Pitfall 2: wildcard CORS 与浏览器确认组合成跨域 mutation 面

**What goes wrong:** 任意网页可能向 loopback REST 发送 mutation；当前服务端可在 `confirmed=true` 时铸造 confirmation token，而浏览器 actor hash 是随机值。

**Why it happens:** 把 local loopback 误认为天然受信任，并把 UI 确认误当作认证。

**How to avoid:** 生产 `/app` 与 API 同源、移除 `Access-Control-Allow-Origin: *`、开发期只用显式 allowlist、mutation route 拒绝跨 origin，并增加无写入的跨域拒绝测试。

**Warning signs:** response header 仍为 `*`、跨 origin preflight/POST 被接受、或确认不需要同源 binding。

**Phase to address:** Phase 36，作为 P0 发布门禁。

---

### Pitfall 3: DTO 漂移让页面显示“未提供”或错误优先级

**What goes wrong:** External 页期望的 `fact_type/source_id/observed_at` 与后端实际 `subject/predicate/valid_from` 不一致；总览使用不存在的 `confirmation_state=confirmed` 或 `importance.score`。

**Why it happens:** 页面以宽松字段访问绕过了编译错误，未建立最小稳定 DTO 与真实响应 fixture。

**How to avoid:** 先固化 projection schema 和 Zod contract；决定一个状态分类与 `final_score` 真值；破坏性改动显式 version/compat。

**Warning signs:** 页面大量 `未提供`、已接受/拒绝项仍进入“现在最重要”、真实高优先级提醒不出现。

**Phase to address:** Phase 36。

---

### Pitfall 4: 把 partial、stale 或 External 当作当前个人事实

**What goes wrong:** 单 authority 失败、证据不足或外部事实过期时，UI 仍显示确定性建议；或把外部招聘/政策自动写成个人状态。

**Why it happens:** 页面追求“永远有答案”，忽略 authority 边界。

**How to avoid:** 所有展示标明 Fact/Observation/Inference/External/Historical/Conflict/Partial；binding mismatch、stale、conflict 或 evidence 不足时禁用 prepare/confirm，并要求重读/新 preview。

**Warning signs:** partial 没有 limitations、External 卡片没有来源/地区/有效期、或 stale 数据仍允许写入。

**Phase to address:** Phase 37 与 Phase 38。

---

### Pitfall 5: 证据页只有空 iframe 或把旧图谱当作 current authority

**What goes wrong:** MCP Widget 服务停止时页面空白；用户看到旧 Memory Graph 却以为它等于当前 Personal State。

**Why it happens:** 通用 Widget 嵌入被当作已有 evidence drill-down。

**How to avoid:** 从当前对象提供 stable ID/checksum 的只读 evidence 路径；Widget 必须有 iframe 安全限制和 degraded state，且明确其历史/诊断定位。

**Warning signs:** 页面不能从决策或状态对象跳到相关证据；MCP down 时只显示白屏。

**Phase to address:** Phase 37。

---

### Pitfall 6: 将一次 Outcome 视觉包装为建议导致结果

**What goes wrong:** 行动/结果时间线暗示因果，或自动把单次成功 promotion 为有效策略。

**Why it happens:** 结果叙事比“不确定/INCONCLUSIVE”更好看。

**How to avoid:** `causal_claim=false` 同时出现在数据、文案和视觉；Calibration 只展示 evidence，不自动 promotion。

**Warning signs:** 出现“建议使结果变好”没有对照/样本证据；用户不能看到样本不足限制。

**Phase to address:** Phase 39。

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|---|---|---|---|
| 浏览器聚合 authority | 很快出页面 | Shadow SSOT、隐私/状态漂移 | Never |
| wildcard CORS | 本地开发省配置 | 跨域 mutation 风险 | 仅一次性隔离原型，不能进入 v1.4 release。 |
| localStorage 保存决策正文 | “离线体验” | 私密数据泄露、stale 与撤销失控 | Never in v1.4 |
| 只跑 Vitest | 快速绿 | 漏掉焦点、缩放、iframe、真实服务故障 | 仅早期开发，不可代替 Phase 40 UAT。 |
| 假按钮（未接 API 的 suppress） | 页面看完整 | 用户误以为已生效 | Never |

## Security Mistakes

| Mistake | Risk | Prevention |
|---|---|---|
| `Access-Control-Allow-Origin: *` 用于 mutation | 恶意网页触发本地 append-only 写入 | 同源生产、explicit dev allowlist、origin 拒绝与测试。 |
| 将 `str(exc)` 送入 UI limitation | 暴露路径、内部信息或敏感上下文 | 稳定 safe error code/message，内部细节只记本地受控日志。 |
| 日志/DOM 记录 raw message、provider body、secret 或 HMAC | 隐私与凭据泄露 | metadata-only、privacy guard、console/DOM scan。 |
| iframe 未限制来源 | 嵌入内容可造成意外资源暴露 | 精确 CSP `frame-src`、sandbox、referrer policy、degraded fallback。 |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---|---|---|
| 紫色或高分暗示模型正确 | 用户把 Candidate 当事实 | 文字标签 + evidence + limitations；紫色仅表示 AI candidate。 |
| 一键确认 | 无法理解写入内容和边界 | exact preview、具体确认文案、replay receipt。 |
| 空状态与故障状态相同 | 用户不知道没有数据还是系统坏了 | 区分 empty / partial / stale / offline / unauthorized，并给恢复路径。 |
| 只为桌面表格设计 | 移动端不可读、长中文裁切 | 320 下转卡片、提供文本摘要、支撑 200% 缩放。 |

## "Looks Done But Isn't" Checklist

- [ ] **Cockpit:** 代码存在但未跟踪、未构建或未映射 requirements 时，不得标记已交付。
- [ ] **Confirmation:** 仅有按钮不是安全；必须有 origin、preview、sequence、idempotency、replay 与 fail-closed 验证。
- [ ] **Evidence:** 只有通用 iframe 不是当前对象的 evidence drill-down。
- [ ] **Health:** REST 200 不是 Chroma、MCP 或 authority 都健康。
- [ ] **Offline:** Query cache 不是可安全离线使用的个人数据缓存。
- [ ] **Calibration:** 一条 Outcome 不是个性化长期增益证明。

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---|---|---|
| stale/partial/binding drift | LOW | 停止写入 → 重读 authority → 生成新 preview。 |
| 重复确认/网络重试 | LOW | 使用同一 idempotency key → 显示 exact replay；不同 payload 走 conflict。 |
| provider outcome unknown | MEDIUM | 不自动 retry → 显示 typed recovery → 手工 review/resume。 |
| DTO mismatch | MEDIUM | 修正 projection schema/compat 字段 → 更新 Zod fixtures → 构建与真实服务 smoke。 |
| 误显示或泄露 | HIGH | 立即停止发布 → 关闭暴露表面 → 记录安全事件 → 清理客户端可见内容并复测。 |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---|---|---|
| WIP 误报完成 | 36 | tracked baseline、build、tests、requirements traceability。 |
| CORS / mutation 风险 | 36 | 跨 origin 被拒绝且无写入；同源 flow 可用。 |
| DTO/Now Stack 漂移 | 36 | Zod fixture + contract + UI regression。 |
| stale/partial/External 混淆 | 37–38 | UI 状态语义、阻写、snapshot/evidence tests。 |
| Evidence iframe 空白 | 37 | MCP down non-empty degraded UAT。 |
| 因果夸大/自动 promotion | 39 | `causal_claim=false`、无 promotion、feedback contract tests。 |
| a11y/真实服务遗漏 | 40 | browser UAT、keyboard、responsive、fault injection、privacy check。 |

## Sources

- `src/personal_knowledge/services/api_server.py`。
- `src/personal_knowledge/services/ui_projection.py`。
- `src/personal_knowledge/intelligence/orchestration/service.py`。
- `apps/personal_decision_cockpit/src/pages/overview/OverviewPage.tsx`。
- `apps/personal_decision_cockpit/src/pages/evidence/EvidencePage.tsx`。

---
*Pitfalls research for: v1.4 Decision Cockpit UI*  
*Researched: 2026-07-22*
