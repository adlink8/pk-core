---
document_type: ui-design-contract
product: Personal Decision Intelligence System
surface: Personal Decision Cockpit
created: 2026-07-19
status: candidate_not_activated
candidate_milestone: v1.4 Decision Cockpit UI
source_of_truth_inputs:
  - .planning/PROJECT.md
  - .planning/STATE.md
  - .planning/ROADMAP.md
  - .planning/milestones/v1.2-MILESTONE-AUDIT.md
  - .planning/milestones/v1.3-MILESTONE-AUDIT.md
  - src/personal_knowledge/services/api_server.py
  - src/personal_knowledge/services/decision_intelligence_reads.py
  - apps/personal_data_chatgpt/README.md
  - src/personal_knowledge/services/dashboard.py
activation_policy: requires fresh milestone requirements and explicit authorization
---

# Personal Decision Cockpit 前端设计契约

## 1. 结论

下一版前端不应继续扩展旧的数据统计仪表盘。应新建一个以**个人当前状态、决策、行动、结果和校准**为主线的独立 Web 产品：

> **Personal Decision Cockpit — 个人决策驾驶舱。**

它不是数据库管理后台，也不是把 ChatGPT 回复换成卡片。它必须让用户在一个页面内看清：

```text
我现在是什么状态
→ 什么变化值得关注
→ 当前有哪些决策
→ 每个方案的收益、成本、风险和证据
→ 我已经确认了什么
→ 实际执行到哪里
→ 结果如何
→ 系统从结果中学到了什么
```

当前文档只定义产品与 UI 契约，不激活 Phase 36，不修改业务代码，不新增写权限。

---

# 2. 当前前端现状审计

## 2.1 已有三类展示面

| 展示面 | 当前能力 | 适用定位 | 主要问题 |
|---|---|---|---|
| `src/personal_knowledge/services/dashboard.py` | Streamlit 总览、事件、旧向量检索、跨模块链路 | 历史诊断工具 | 读取旧 `integration/db` 和旧 `personal_events`，未绑定当前 Serving Snapshot、External、Analysis、Pilot、Calibration 权威 |
| ChatGPT MCP Widgets | Data Browser、Memory Graph、Relation Review | 对话中的局部下钻 | 适合一次工具结果，不适合长期状态、决策工作流和跨页面导航 |
| REST/MCP Agent surfaces | External、Analysis、Pilot、Calibration、Session Orchestration | 稳定后端接口 | 缺少独立产品壳、统一信息架构和用户可视化工作流 |

## 2.2 旧 Streamlit 仪表盘的处理决定

旧 Streamlit 页面不删除，但重新定位为：

```text
Legacy Diagnostic Dashboard
```

保留用途：

- 历史数据排查；
- 旧事件表和分类检查；
- 开发阶段快速观察；
- 回归取证。

禁止继续承担：

- 当前个人状态首页；
- 决策方案比较；
- 用户确认；
- Action/Outcome 跟踪；
- External Context 展示；
- Calibration 结论；
- 正式产品导航。

## 2.3 ChatGPT Widget 的处理决定

现有 Widget 保留，并作为驾驶舱的补充入口：

```text
ChatGPT
├── 快速提问和解释
├── 单条证据下钻
├── Data Browser Widget
├── Memory Graph Widget
└── Relation Review Widget

Independent Web Cockpit
├── 长期状态总览
├── 决策工作区
├── 行动结果时间线
├── 外部环境
├── 主动提醒
└── 系统健康
```

两者共享后端 Service/REST 契约，不复制事实权威。

---

# 3. 产品目标与边界

## 3.1 前端目标

1. 把 v1.2–v1.3 已交付能力转化为用户可理解的产品流程。
2. 默认展示当前有效状态，而不是原始数据库行。
3. 让事实、观察、推断、预测、建议和用户决定具有明确视觉区分。
4. 让每条高层结论可以下钻到 Personal/External Evidence。
5. 让所有写入都经过 exact preview、显式确认和幂等保护。
6. 让 `ABSTAIN`、`INCONCLUSIVE`、过期、冲突和部分可用状态醒目可见。
7. 在桌面端完成复杂分析，在手机端完成状态扫视、确认和结果记录。

## 3.2 非目标

- 不做通用数据库管理后台；
- 不显示所有内部表和 checksum 作为首页主体；
- 不把生活压缩成一个“人生总分”；
- 不用炫目图表制造虚假精确感；
- 不默认自动执行外部动作；
- 不允许前端直接访问 SQLite 或 Chroma；
- 不在浏览器中持有 provider key、控制平面 key或数据库路径；
- 不让前端重新实现后端风险、权限和状态机规则。

---

# 4. 产品形态

## 4.1 三层产品面

```text
Personal Decision Cockpit        独立主产品
        │
        ├── REST Read Projection  统一只读投影
        ├── Guarded Orchestration 显式确认写入
        └── Evidence Drill-down   证据下钻

ChatGPT MCP App                   自然语言与嵌入式组件入口

Legacy Streamlit Dashboard       开发/诊断入口
```

## 4.2 推荐实现位置

```text
apps/personal_decision_cockpit/
```

推荐技术栈：

```text
React + TypeScript + Vite
Tailwind CSS
TanStack Query
Zod
ECharts 或 Recharts
Playwright
```

选择 Vite，而不是默认采用 Next.js，原因是：

- 当前是本地单用户产品；
- 不需要 SEO、SSR、公开内容和多租户认证；
- 构建后可以作为静态资源由现有 Python REST 服务托管；
- 生产环境不新增一个长期运行的 Web Server；
- 现有 Supervisor 仍只需管理 REST、MCP、Tunnel，加上独立 Chroma 服务。

未来出现远程多用户、账户、云同步或服务端渲染需求时，再重新评估 Next.js。

---

# 5. 核心用户流程

## 5.1 每日查看流程

```text
打开驾驶舱
  ↓
查看数据新鲜度与系统状态
  ↓
查看“现在最重要的三件事”
  ↓
检查目标、约束、风险和外部变化
  ↓
进入待决策事项或主动提醒
  ↓
查看证据和限制
  ↓
决定是否启动正式决策流程
```

## 5.2 正式决策流程

```text
定义决策问题
  ↓
选择 Personal State Snapshot
  +
选择 External Context Snapshot
  ↓
核对目标、约束、风险预算、时间窗口
  ↓
生成 Decision Analysis Candidate
  ↓
比较候选方案与“不行动”基线
  ↓
查看证据、假设、缺失信息和停止条件
  ↓
接受 / 修改 / 拒绝 / 延迟
  ↓
显式确认写入 Decision
  ↓
记录 Action Start / Action Complete
  ↓
记录 Outcome
  ↓
查看非因果 Effectiveness 与 Calibration
```

## 5.3 写入安全流程

每一个写入步骤都必须保持现有 v1.3 契约：

```text
Prepare
→ 展示 exact preview
→ 用户逐项检查
→ Confirm
→ Execute
→ 返回 sequence / event_id / checksum
→ 网络重试返回 exact replay
```

前端不得提供“一键自动完成全部阶段”。

---

# 6. 信息架构

## 6.1 桌面端主导航

```text
1. 今日总览        Overview
2. 个人状态        Personal State
3. 决策中心        Decisions
4. 行动与结果      Actions & Outcomes
5. 外部环境        External Context
6. 主动提醒        Proactive Inbox
7. 证据中心        Evidence
8. 系统状态        System
```

## 6.2 移动端主导航

底部只保留五个高频入口：

```text
总览 / 决策 / 行动 / 提醒 / 更多
```

“更多”中包含：个人状态、外部环境、证据、系统状态。

## 6.3 全局顶部栏

始终显示：

- 当前 Personal Snapshot；
- 当前 External Snapshot；
- 数据新鲜度；
- 系统运行状态；
- 隐私模式；
- 全局搜索；
- “新建决策”按钮。

快照 ID 只显示短 ID，完整 ID 通过悬浮或详情查看。

---

# 7. 页面设计

## 7.1 今日总览

### 目标

让用户在 30 秒内回答：

```text
我现在最需要关注什么？
```

### 桌面布局

```text
┌────────────────────────────────────────────────────────────────────┐
│ Personal Decision Cockpit              快照新鲜度  系统状态  新决策 │
├──────────────┬─────────────────────────────────────────────────────┤
│ 导航         │ 现在最重要                                           │
│              │ ① 求职窗口接近  ② 英语投入不足  ③ 项目已进入收口期   │
│ 今日总览     ├──────────────────────┬──────────────────────────────┤
│ 个人状态     │ 当前目标与约束        │ 主要变化与风险                │
│ 决策中心     │ 外企 IoT / DevOps     │ 项目扩张风险  中               │
│ 行动与结果   │ 每周 30 小时          │ 外部岗位要求变化  新           │
│ 外部环境     ├──────────────────────┴──────────────────────────────┤
│ 主动提醒     │ 待决策事项                                             │
│ 证据中心     │ [职业] 实习优先还是项目继续？       查看方案           │
│ 系统状态     │ [项目] 是否启动 Phase 36？           建议暂缓           │
│              ├──────────────────────┬──────────────────────────────┤
│              │ 主动提醒              │ 最近行动与结果                │
│              │ 3 条，1 条高重要性    │ 计划执行 4 周 / 等待 outcome   │
│              ├──────────────────────┴──────────────────────────────┤
│              │ 外部环境摘要 + 数据可信度 + 最近更新时间               │
└──────────────┴─────────────────────────────────────────────────────┘
```

### 核心模块

1. **Now Stack**：最多三项，按重要性、紧迫性、证据质量排序。
2. **Goal & Constraint Summary**：不超过五个当前目标与五个硬约束。
3. **Change & Risk Cards**：变化、冲突、趋势、风险分别标记。
4. **Decision Queue**：草稿、分析中、等待确认、执行中、等待结果。
5. **Proactive Inbox Preview**：只显示达到阈值的候选。
6. **Recent Outcomes**：最近行动结果和预测偏差。
7. **External Delta**：只显示与当前目标相关的外部变化。
8. **Freshness Footer**：Personal/External/Knowledge 各自更新时间和状态。

### 禁止项

- 首页展示 20 个 KPI；
- 使用“人生健康度 82 分”；
- 使用大面积饼图；
- 把数据库行数作为核心成果；
- 将 LLM 置信度伪装成客观概率。

---

## 7.2 个人状态

### 页面目的

展示当前有效个人模型，并让用户区分：

```text
事实 / 观察 / 推断 / 用户确认 / 历史状态
```

### 页面结构

```text
顶部：Personal Snapshot + as_of + evidence coverage

八领域状态网格：
learning / career / project / health
finance / relationship / time / energy

每个领域卡片：
- 当前目标
- 当前约束
- 当前观察
- 当前推断
- 最近变化
- 证据数量
- 数据新鲜度
```

点击领域后进入详情：

- Current Assertions；
- Change Timeline；
- Conflict/Resolution；
- Historical/Superseded；
- Evidence Drawer；
- 用户纠正入口。

### 视觉规则

| 类型 | 视觉标识 |
|---|---|
| Fact | 实线边框 + `事实` 标签 |
| Observation | 蓝灰标签 |
| Inference | 紫色虚线边框 + `推断` |
| Forecast | 琥珀色 + 时间窗口 |
| Recommendation | 靛蓝卡片 + `建议候选` |
| User Confirmation | 绿色确认标识 |
| Conflict | 红色双向冲突标识 |
| Historical | 降低透明度，不默认参与当前判断 |

颜色不能成为唯一提示，必须同时包含文字和图标。

---

## 7.3 决策中心

### 列表页

分组：

```text
需要关注
等待确认
执行中
等待结果
已完成
已延迟 / 已拒绝
```

每张 Decision Card 显示：

- 决策问题；
- Domain；
- 当前阶段；
- 时间窗口；
- Personal/External Snapshot；
- 推荐方案；
- 主要限制；
- 更新时间；
- 下一步动作。

### 决策详情页

```text
┌────────────────────────────────────────────────────────────────────┐
│ 决策：未来 8 周如何分配英语、项目和求职时间                         │
│ 阶段：Analysis Ready   Personal ss_xxx   External es_xxx           │
├───────────────────┬────────────────────────────┬───────────────────┤
│ 决策条件           │ 方案比较                    │ 建议与限制         │
│ 目标               │ A 英语优先                  │ 推荐：D 双主线      │
│ 硬约束             │ B 项目优先                  │ 置信度：中          │
│ 风险预算           │ C Kubernetes 优先           │ 缺失信息            │
│ 时间窗口           │ D 双主线                    │ 停止条件            │
│ 不行动基线         │ E 收口不新增                │ 反面证据            │
├───────────────────┴────────────────────────────┴───────────────────┤
│ 标签：分析 │ 证据 │ 假设 │ 历史 │ Session Events                  │
└────────────────────────────────────────────────────────────────────┘
```

### 方案比较表

| 维度 | A | B | C | D | 不行动基线 |
|---|---:|---:|---:|---:|---:|
| 目标贡献 | 文本等级 | 文本等级 | 文本等级 | 文本等级 | 文本等级 |
| 时间成本 | 明确数值/区间 | | | | |
| 金钱成本 | | | | | |
| 可逆性 | 高/中/低 | | | | |
| 风险 | | | | | |
| 机会成本 | | | | | |
| 证据完整性 | | | | | |
| 主要假设 | | | | | |

前端可以提供排序，但不得只显示一个综合分数。综合排序必须能展开查看权重和计算依据。

### 确认抽屉

用户点击“接受/修改/延迟/拒绝”后，从右侧打开确认抽屉：

1. 操作名称；
2. exact preview；
3. 将新增的 Event；
4. 不会执行的动作；
5. checksum；
6. idempotency key；
7. 风险和不可逆性；
8. 确认按钮。

确认按钮文案必须具体，例如：

```text
确认写入“接受方案 D”
```

禁止使用模糊的“继续”或“确定”。

---

## 7.4 行动与结果

### 页面目的

把决策从“语言建议”转换为可追踪的真实过程。

### 主视图

使用纵向时间线：

```text
Recommendation
  ↓
Decision
  ↓
Action Start
  ↓
Action Complete
  ↓
Outcome
  ↓
Effectiveness
  ↓
Calibration
```

每个节点显示：

- event type；
- timestamp；
- user/system ownership；
- status；
- evidence；
- observed result；
- expected vs actual；
- causal claim 状态。

### Outcome 表单

只允许记录观察：

- 实际完成内容；
- 实际耗时；
- 实际成本；
- 主观满意度；
- 未预期副作用；
- 未完成原因；
- 证据附件或引用；
- 数据可信度。

前端必须提示：

```text
结果记录不自动证明建议导致了结果。
```

---

## 7.5 外部环境

### 页面结构

1. Active External Snapshot；
2. Allowlisted Sources；
3. 与个人目标相关的 External Facts；
4. 新增、更新、过期和冲突；
5. 来源详情；
6. 地区、时间和可信度筛选；
7. Evidence/Provenance 下钻。

### 外部事实卡

显示：

- 标题；
- 类型；
- 适用地区；
- observed_at / valid_at；
- 来源；
- source quality；
- freshness；
- conflict state；
- 与哪些目标/决策相关。

必须明确写出：

```text
外部事实不会自动成为个人事实。
```

---

## 7.6 主动提醒

### 视图

分为：

```text
需要现在处理
可延后
已抑制
冷却中
历史
```

每张提醒卡显示：

- 领域；
- 标题；
- 重要性；
- 新颖性；
- 触发依据；
- 建议动作；
- 限制；
- 冷却时间；
- 噪声预算状态。

操作：

- 查看证据；
- 创建 Decision Case；
- Snooze；
- Suppress；
- 限定 Scope；
- Restore。

Suppress/Restore 必须走现有显式确认与 append-only control history。

---

## 7.7 证据中心

证据中心整合现有能力，而不是重写一套数据浏览器。

子页面：

```text
Knowledge Search
Evidence Drill-down
Data Browser
Memory Graph
Relation Review
Lifecycle History
```

### 默认搜索结果

每个结果显示：

- KU/Message/Event 类型；
- 当前生命周期状态；
- 来源；
- 时间；
- 相关度；
- 当前是否 eligible；
- Snapshot；
- 下钻路径。

Private body 默认折叠，并遵守现有 Privacy Guard。

### 旧 Widget 复用

- `data-browser-widget.html`：嵌入数据浏览子页；
- `memory-graph-widget.html`：作为旧关系层探索工具；
- `relation-review-widget.html`：作为治理审核工具；
- 不把旧 Memory Graph 宣称为当前 Personal State 图。

---

## 7.8 系统状态

### 面向用户的状态

显示：

- REST；
- MCP；
- Tunnel；
- Chroma；
- Active KU Collection；
- Serving Snapshot；
- Watermark；
- Personal/External/Analysis/Pilot/Calibration Authority；
- 最近验证时间；
- 数据新鲜度；
- 当前隐私模式。

### 面向开发者的高级区域

默认折叠：

- full snapshot IDs；
- checksums；
- row counts；
- Doctor checks；
- Governance Preflight；
- recent runtime events；
- typed recovery details。

不要在普通首页展示大量工程指标。

---

# 8. 核心组件

| 组件 | 用途 |
|---|---|
| `SnapshotChip` | 显示 Personal/External/Serving Snapshot 与新鲜度 |
| `AuthorityBadge` | 标记 Personal、External、Analysis、Pilot、Calibration |
| `ClaimTypeBadge` | Fact、Observation、Inference、Forecast、Recommendation |
| `FreshnessBadge` | current、stale、expired、unknown |
| `EvidenceLink` | 打开证据抽屉 |
| `EvidenceDrawer` | 下钻到 KU、message、event、source |
| `DecisionStageStepper` | 显示 prepare 到 calibration 的阶段 |
| `OptionComparisonMatrix` | 方案和基线对比 |
| `ConstraintChip` | 硬约束和软偏好 |
| `RiskCard` | 风险、严重度、可逆性、缓解措施 |
| `RecommendationCard` | 推荐、备选、停止条件、限制 |
| `ExactPreviewPanel` | 显示写入前 preview/checksum/idempotency |
| `OutcomeTimeline` | Action/Outcome/Effectiveness 时间线 |
| `CalibrationPanel` | personalized/generic 和 INCONCLUSIVE 说明 |
| `ProactiveCard` | 主动候选及 suppress/snooze/restore |
| `SystemHealthStrip` | 运行态和数据新鲜度 |
| `AbstentionPanel` | 信息不足、冲突、高风险时的拒绝原因 |
| `TypedRecoveryPanel` | 可恢复错误和下一步 |

---

# 9. 视觉系统

## 9.1 风格

采用：

```text
Calm Analytical
+ Executive Dashboard
+ Drill-down Analytics
```

不采用：

- Cyberpunk；
- 大面积霓虹；
- 复杂玻璃拟态；
- 过度动画；
- 游戏化人生分数。

## 9.2 主题

默认浅色，提供深色模式。

### 浅色语义色

| 语义 | 建议色 |
|---|---|
| Primary / Action | Indigo 600 |
| Verified / Confirmed | Emerald 600 |
| Uncertainty / Stale | Amber 600 |
| Risk / Conflict | Rose 600 |
| LLM Candidate | Violet 600 |
| External Context | Cyan 700 |
| Neutral Text | Slate 900 / 600 |
| Surface | White / Slate 50 |
| Border | Slate 200 |

### 规则

- 红色只表示风险、冲突或失败；
- 绿色只表示验证、确认或完成；
- 紫色用于 AI Candidate，不表示正确；
- 琥珀色用于不确定、过期和等待；
- 所有颜色状态同时提供文字和图标。

## 9.3 字体

默认使用本地系统字体，避免外部字体 CDN：

```css
font-family: Inter, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
```

ID、checksum、sequence 使用：

```css
font-family: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
```

## 9.4 密度

提供两档：

```text
Comfortable  默认
Compact      高数据密度
```

复杂表格支持列选择和保存本地视图，但不改变后端数据。

## 9.5 动画

- 150–250ms；
- 只用于状态过渡、抽屉和下钻；
- 不使用持续脉冲，除非存在实时严重故障；
- 尊重 `prefers-reduced-motion`。

---

# 10. 图表规则

| 数据 | 图表 |
|---|---|
| 目标或行为随时间变化 | 折线图 |
| 方案维度比较 | 横向分组条形图 + 表格 |
| 预测与不确定性 | 实线/虚线 + Confidence Band |
| Action/Outcome 过程 | 时间线 |
| 风险严重度和可逆性 | 二维矩阵 |
| 领域资源分配 | 堆叠条形图 |
| 外部来源和状态 | 排序表格 |
| 数据新鲜度 | 时间轴或状态条 |

禁止在核心决策中只使用：

- 雷达图；
- 饼图；
- 仪表盘速度表；
- 无解释的 AI 分数。

所有图表必须提供等价数据表。

---

# 11. 数据与 API 契约

## 11.1 现有接口映射

| 页面 | 当前可用 REST |
|---|---|
| Personal State | `/intelligence/state/current`, `/intelligence/state/history`, `/intelligence/changes/recent`, `/intelligence/state/explain` |
| Recommendations | `/decision/recommendations`, `/decision/recommendation`, `/decision/recommendation/history`, `/decision/recommendation/outcomes`, `/decision/recommendation/effectiveness` |
| Proactive | `/proactive/inbox`, `/proactive/digest`, `/proactive/candidate`, `/proactive/candidate/explain`, `/proactive/controls/status`, `/proactive/metrics` |
| External | `/agent/external`, `/agent/external/item`, `/agent/external/explain` |
| Analysis | `/agent/analysis`, `/agent/analysis/item`, `/agent/analysis/explain` |
| Pilot | `/agent/pilot`, `/agent/pilot/item`, `/agent/pilot/explain` |
| Calibration | `/agent/calibration`, `/agent/calibration/item`, `/agent/calibration/explain` |
| Session read | `/agent/session/resume`, `/agent/session/explain` |
| Session writes | `/agent/session/prepare`, `/confirm`, `/preview`, `/generate`, `/decide`, `/action-start`, `/action-complete`, `/observe`, `/calibrate` |
| Knowledge | `/knowledge/status`, `/search/semantic` |
| System | `/health`, `/stats` |

## 11.2 新增 UI Projection 层

驾驶舱不应在浏览器中拼接十几个权威响应。建议增加只读投影：

```text
src/personal_knowledge/services/ui_projection.py
```

投影只组合已有 Service 结果，不创建新事实权威，不写数据库。

建议端点：

```text
GET /ui/overview
GET /ui/personal-state
GET /ui/decision/{id}/workspace
GET /ui/actions/recent
GET /ui/external/delta
GET /ui/proactive/summary
GET /ui/system/status
```

统一响应：

```json
{
  "schema_version": "decision_cockpit_projection_v1",
  "ok": true,
  "generated_at": "...",
  "snapshot_bindings": {
    "personal": "...",
    "external": "...",
    "serving": "..."
  },
  "freshness": {},
  "limitations": [],
  "data": {}
}
```

## 11.3 投影层边界

- 只读；
- query-only SQLite；
- 不直接调用 provider；
- 不执行外部动作；
- 不 promotion；
- 不创建 Recommendation；
- 不改变排序和风险规则；
- 所有字段保留 authority ID 和 evidence link；
- 任何部分失败时返回 partial 状态，不伪装完整成功。

---

# 12. 前端状态模型

所有页面必须处理以下状态：

| 状态 | UI 行为 |
|---|---|
| Loading | Skeleton，不显示旧数据为新数据 |
| Empty | 说明为什么为空和下一步 |
| Partial | 明确哪些 Authority 不可用 |
| Stale | 显示更新时间和重新同步入口 |
| Conflict | 显示冲突，不自动选择一边 |
| Abstained | 展示 reason code、缺失信息和恢复条件 |
| Inconclusive | 展示样本不足或协议偏离原因 |
| Offline | 提供只读缓存范围说明 |
| Error | `role=alert`，提供恢复路径 |
| Replay | 显示“已返回原事件，未重复写入” |

禁止空白页面和静默失败。

---

# 13. 写入与权限交互

## 13.1 默认只读

页面加载、筛选、比较、搜索和下钻都不得写入。

## 13.2 写入按钮分类

| 类型 | 示例 | 交互 |
|---|---|---|
| 本地 UI 状态 | 折叠、主题、列宽 | 不进入业务 Authority |
| 用户控制 | Snooze、Suppress、Restore | exact preview + confirm |
| 决策记录 | Accept、Reject、Modify、Defer | exact preview + confirm |
| Action/Outcome | Start、Complete、Observe | exact preview + confirm |
| Provider generation | Generate Analysis | 显示模型、预算、快照和限制后确认 |
| Calibration | Calibrate | 显示 protocol、样本、预算和不可因果声明 |

## 13.3 高风险领域

健康、财务、关系领域在前端增加域级风险提示和更高确认摩擦。自动执行始终禁用。

---

# 14. 响应式与无障碍

## 14.1 断点

- 320–767：移动端；
- 768–1023：平板；
- 1024–1439：桌面；
- 1440+：宽屏。

## 14.2 移动端规则

- 首页只显示三项最重要内容；
- 方案比较改为逐方案卡片；
- 证据详情使用全屏抽屉；
- 确认操作固定在底部；
- 表格自动转卡片，但保留导出和列查看；
- 不允许横向页面整体滚动。

## 14.3 无障碍

- WCAG AA 对比度；
- 键盘完整操作；
- 可见 focus；
- 错误使用 `role=alert` 或 `aria-live`；
- 表单均有 label；
- 图表有文本摘要和表格；
- 状态不只依赖颜色；
- 支持 reduced motion；
- 中文读屏顺序与视觉顺序一致。

---

# 15. 隐私与安全

1. 前端只接收最小必要字段。
2. Private body 默认不请求、不渲染。
3. 所有响应继续经过 Privacy Guard。
4. 浏览器日志禁止输出完整 payload、secret、PII、raw evidence body。
5. `localStorage` 只保存主题、密度和非敏感视图偏好。
6. Decision/Outcome 内容不放入前端持久缓存。
7. 前端不得持有 SQLite 路径、provider key、HMAC secret。
8. 所有写入由现有 Guarded Orchestration Service 完成。
9. 前端展示 provider 调用预算，但不自行计算权限。
10. Tunnel 远程访问时仍以服务端权限边界为准。

---

# 16. 建议目录结构

```text
apps/personal_decision_cockpit/
├── src/
│   ├── app/
│   │   ├── router.tsx
│   │   └── providers.tsx
│   ├── pages/
│   │   ├── overview/
│   │   ├── state/
│   │   ├── decisions/
│   │   ├── actions/
│   │   ├── external/
│   │   ├── proactive/
│   │   ├── evidence/
│   │   └── system/
│   ├── components/
│   │   ├── authority/
│   │   ├── decision/
│   │   ├── evidence/
│   │   ├── feedback/
│   │   └── layout/
│   ├── api/
│   │   ├── client.ts
│   │   ├── schemas.ts
│   │   └── orchestration.ts
│   ├── design-system/
│   │   ├── tokens.css
│   │   └── components.ts
│   └── test/
├── public/
├── package.json
└── README.md
```

构建产物建议输出到：

```text
apps/personal_decision_cockpit/dist/
```

由现有 Python REST 服务托管 `/app`，生产环境不新增常驻前端进程。

---

# 17. 测试与验收

## 17.1 组件测试

- Claim 类型视觉边界；
- Snapshot 新鲜度；
- ABSTAIN/INCONCLUSIVE；
- Exact Preview；
- Replay；
- Partial Authority；
- Evidence Drawer；
- 移动端方案卡。

## 17.2 Contract 测试

- Zod Schema 与 REST 响应一致；
- UI Projection 不丢失 authority/evidence IDs；
- Compact/Full 响应兼容；
- Privacy sealed value 不被错误展开；
- route operation mismatch 正确显示；
- stale checksum 和 idempotency conflict fail closed。

## 17.3 E2E

至少覆盖：

```text
1. 打开总览并显示三类 Snapshot
2. 从提醒创建 Decision Case
3. 查看 Analysis 和 Evidence
4. Prepare → Confirm → exact replay
5. Decide → Action Start → Action Complete → Observe
6. 查看 Outcome 和 non-causal Effectiveness
7. 查看 INCONCLUSIVE Calibration
8. Chroma/REST/MCP 单项离线时显示 partial 和恢复路径
```

## 17.4 视觉验收

- 320、768、1024、1440 宽度；
- 浅色/深色；
- 键盘导航；
- reduced motion；
- 200% 缩放；
- 中文长文本；
- 长 ID 和错误详情不破坏布局。

---

# 18. 候选实施阶段

以下仅为候选，不进入 Active Roadmap。

## Candidate Phase 36 — UI Projection and App Shell

目标：建立只读 UI Projection、React/Vite 应用壳、导航、设计系统和系统状态条。

退出条件：

- `/ui/overview` 与 `/ui/system/status`；
- 无新事实权威；
- Snapshot 和 evidence ID 保留；
- 浅色/深色与响应式壳；
- 当前旧 Widget 可嵌入。

## Candidate Phase 37 — Personal State and External Context

目标：实现个人状态八领域、变化/冲突/历史和外部环境页面。

退出条件：

- Fact/Observation/Inference 边界可视化；
- Current/Historical/Superseded 清晰；
- External 与 Personal 明确隔离；
- Evidence Drawer 完整。

## Candidate Phase 38 — Decision Workspace and Guarded Confirmation

目标：实现方案比较、建议、限制、停止条件和 exact preview/confirm。

退出条件：

- 不行动基线；
- 多方案矩阵；
- prepare/confirm/replay；
- 不重复写入；
- 修改/拒绝/延迟均可追溯。

## Candidate Phase 39 — Actions, Outcomes, Calibration and Proactive

目标：打通行动结果时间线、主动提醒和校准页面。

退出条件：

- Action Start/Complete；
- Outcome observation；
- causal claim 明示；
- INCONCLUSIVE 可解释；
- Suppress/Snooze/Restore。

## Candidate Phase 40 — Product Hardening and Live UAT

目标：完成无障碍、移动端、性能、错误恢复和真实用户 UAT。

退出条件：

- 核心页面首屏快速可用；
- 单 Authority 故障 partial degrade；
- E2E 全链通过；
- 所有写入显式确认；
- 无隐私正文泄露；
- 与 ChatGPT MCP 表意一致。

---

# 19. 优先级

## P0

- 今日总览；
- 个人状态；
- 决策中心；
- exact preview/confirm；
- Action/Outcome 时间线；
- Evidence Drawer；
- System Health。

## P1

- External Context；
- Proactive Inbox；
- Calibration；
- 移动端；
- 深色模式。

## P2

- 自定义布局；
- 高级图谱；
- 多种主题；
- 可导出的决策报告；
- 复杂统计和长期预测。

---

# 20. 最终验收标准

前端完成不能以“页面能打开”判断。必须满足：

1. 用户在 30 秒内看懂当前状态、风险和待决策事项。
2. 用户能从建议下钻到 Personal/External Evidence。
3. 用户能区分事实、推断、预测和建议。
4. 用户能看到模型限制、缺失信息和停止条件。
5. 用户能安全完成 prepare/confirm，并验证 exact replay。
6. 用户能追踪 Recommendation→Decision→Action→Outcome→Calibration。
7. 系统离线、部分故障、过期和冲突不会被伪装为正常。
8. 旧 Widget 得到复用，但旧 Memory/Event 层不会冒充当前决策权威。
9. 前端不创建新 SSOT，不复制后端规则，不直接访问数据库。
10. 用户仍保留最终决策权，系统仍保持零未授权外部动作和零自动 promotion。

---

# 21. 最终产品效果

完成后，项目将从：

```text
后端能力 + REST/MCP + ChatGPT 工具 + 局部 Widget
```

转化为：

```text
一个可每日使用的个人决策驾驶舱

当前状态
→ 重要变化
→ 决策分析
→ 用户确认
→ 行动结果
→ 后验校准
→ 证据下钻
```

这套前端的核心价值不是“更好看”，而是让当前已经完成的个人决策智能能力形成一个**可理解、可操作、可复盘的完整产品界面**。
