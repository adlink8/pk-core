---
document_type: candidate-product-and-gsd-planning-spec
product: Personal Decision Intelligence System
surface: Personal Knowledge Wiki Projection
created: 2026-07-22
status: candidate_not_activated
candidate_milestone: post-Cockpit Personal Knowledge Wiki
depends_on:
  - Personal Decision Cockpit read projection and guarded-write contract
  - Active serving snapshot and evidence drill-down authorities
activation_policy: fresh milestone requirements plus explicit user authorization
source_of_truth_inputs:
  - .planning/PROJECT.md
  - .planning/STATE.md
  - .planning/ROADMAP.md
  - .planning/research/v1.4-decision-cockpit-ui/UI-SPEC.md
  - src/personal_knowledge/services/ui_projection.py
  - src/personal_knowledge/services/api_server.py
  - apps/personal_decision_cockpit/src/app/router.tsx
  - apps/personal_decision_cockpit/src/pages/evidence/EvidencePage.tsx
---

# Personal Knowledge Wiki Projection 候选规格与 GSD 规划

## 1. 结论与当前事实

应建设的是一个面向个人决策系统的 **Personal Knowledge Wiki Projection（个人主题知识投影）**：将高价值、稳定且可回溯的主题组织为可浏览页面，减少每次从大量 KU 与原始证据重新汇总的成本。

它不是新的事实库，也不是让 LLM 随意写百科；更接近绑定权威快照的物化知识视图。

当前仓库存在两种不能混用的“Wiki”：

| 名称 | 当前位置/状态 | 用途 | 是否本候选范围 |
|---|---|---|---|
| 开发与运维 Wiki | `docs/wiki/`，已有 10 篇静态 Markdown | 解释项目架构、命令、治理与排障 | 否；保留，另行做文档准确性审计 |
| 个人主题 Wiki Projection | 当前不存在路由、投影或页面 | 解释个人项目、目标、决策及其证据与历史 | 是 |

已实现但尚未纳入正式 GSD 里程碑状态的 Decision Cockpit 工作区，已有总览、个人状态、外部环境、决策、行动结果、主动提醒、证据中心和系统状态路由，也已有只读 `CockpitProjectionService`。其中没有 `wiki`、`topic_page`、`backlink` 或个人主题页 API；因此本能力不能宣称已实现。

## 2. 产品定位与边界

### 2.1 解决的问题

```text
高频长期主题
  → 读取已验证的结构化状态与证据索引
  → 形成带快照绑定的主题页
  → 仅对新变化与新问题做增量检索/推理
```

它优先回答“这个主题是什么、现在处于什么状态、如何演变、与哪些决策相关”，而 Decision Cockpit 继续回答“现在应该关注什么、准备什么、确认什么、如何记录结果”。

### 2.2 不做项

- 不把 `docs/wiki/` 迁移为个人知识页面，不改写既有运维手册；
- 不将 Wiki 页面、页面摘要或 LLM 文案重新写入 KU / Chroma 作为检索权威，避免自我检索闭环；
- 不复制、覆盖或手工维护 Personal State、External Context、KU、Decision、Outcome 的事实字段；
- P0 不提供自由编辑器、页面合并、任意标签体系或“全部知识自动建页”；
- P0 不调用 Provider、不抓取外部网页、不执行外部动作，也不自动 promotion；
- 不把 Observation、Inference、Forecast、Recommendation 渲染成 Fact；
- 不把实时端口、临时提醒或未确认情绪表达固化为长期主题页正文。

### 2.3 角色分工

```text
Canonical / KU / Personal State / External / Decision Authority
    └── 保存并治理事实、状态、建议、行动和结果

Personal Knowledge Wiki Projection
    └── 按主题组织已存在且可验证的信息；保存的是可重建页面版本与依赖

Decision Cockpit
    └── 当前状态、决策工作流、显式确认、行动结果和系统健康

LLM
    └── 后续仅生成受证据约束的叙述候选；不拥有页面或事实写权
```

## 3. 架构契约

### 3.1 权威输入与输出

| 输入 | 允许用途 | 禁止用途 |
|---|---|---|
| Active KU / Serving Snapshot | 主题证据索引、细节下钻、缺失信息补充 | 复制为新的事实 SSOT |
| Personal State | 当前目标、约束、观察、生命周期与变更 | 以页面文案覆盖状态 |
| Decision / Pilot / Calibration | 决策、确认、行动、结果、非因果评估 | 将单次 Outcome 解释成因果结论 |
| External Context | 与主题有关的外部事实、来源、地区、有效期 | 写入个人事实或改变个人状态 |
| 现有 MCP Evidence Widget | 证据浏览与旧图谱入口 | 宣称旧 Memory Graph 是当前 Personal State |

输出为只读 `personal_wiki_projection_v1` 信封，沿用 Cockpit 的明确降级语义：

```json
{
  "schema_version": "personal_wiki_projection_v1",
  "operation": "topic.get|topic.list|topic.backlinks",
  "ok": true,
  "generated_at": "...",
  "snapshot_bindings": {
    "serving": "...",
    "personal": "...",
    "external": "...",
    "decision": "..."
  },
  "freshness": {"status": "fresh|stale|partial|unavailable", "reason_codes": []},
  "authorities": {},
  "partial": false,
  "limitations": [],
  "data": {}
}
```

任何输入 Authority 不可用时，仅使依赖该 Authority 的页面区域成为 `partial` 或 `unavailable`，不得以旧页面伪装为当前事实。

### 3.2 主题身份与页面版本

首版不假设仓库已有通用实体图谱或稳定的 Skill 身份表。主题身份必须由可解释键确定：

| P0 类型 | 稳定主题键 | 主要权威 |
|---|---|---|
| Project | `project:{scope}` | Personal State `domain=project`、Project Decision / Outcome |
| Goal | `goal:{domain}:{scope}:{predicate}` | Personal State `assertion_kind=goal` |
| Decision | `decision:{recommendation_id}` | Decision Feedback / Pilot authority |

`skill`、`career_direction` 和 `external_topic` 仅列为后续候选：它们需要先解决别名、同名消歧、跨 KU 归属和稳定 topic identity，不能由一次语义命中自动创建页面。

页面版本至少绑定：

```text
topic_id
topic_type
projection_version
source snapshot bindings
generated_at
freshness state
dependency references
evidence references
projection checksum
```

若后续需要保存用户固定标题、别名或页面排序，这些只能进入独立的 **UI metadata registry**；其权威范围仅限导航元数据，不得承载个人事实、推断或建议。

### 3.3 “持久化缓存”的精确定义

Wiki 是物化投影，而不是无失效策略的缓存：

```text
上游 Authority / Snapshot 改变
  → 依据显式 dependency 判断受影响 topic
  → 旧页面标记 stale
  → 重新生成 deterministic projection
  → 新版本替换可见版本或标记 partial
```

P0 页面必须能在不依赖缓存正文的情况下，从绑定的 Authority 完整重建。浏览路径优先级为：

```text
fresh Wiki Topic
→ current structured authority
→ Active KU / search
→ raw evidence drill-down
```

页面正文与其摘要不得自动回灌进向量检索索引；否则 Wiki 的推导内容会被误当成独立证据。

### 3.4 事实类型与隐私显示

每个条目必须显示类别、状态与来源，而不是仅给流畅叙述：

| 类别 | 页面标签 | 规则 |
|---|---|---|
| Fact | 已验证事实 | 可在正文摘要中展示，但附 evidence/authority 链接 |
| Observation | 观察 | 不得升级为稳定人格或因果结论 |
| Inference | 系统推断 | 使用不确定性文案和独立区块 |
| Forecast | 预测 | 必须带时间范围、外部来源或假设 |
| Recommendation | 建议候选 | 链向 Decision Workspace，不写成个人状态 |
| Historical / Superseded / Conflict | 历史或冲突 | 与 current 分区，默认不作为当前结论 |

页面遵循既有 metadata-only 和隐私封存策略。内容权限不足时显示“内容经隐私封存，仅展示元数据与可用证据状态”，不尝试用模型补写缺失内容。

## 4. 首版用户体验

### 4.1 信息架构

Cockpit 主导航把“证据中心”演进为 **知识与证据**，但保留 `/evidence` 兼容入口：

```text
知识与证据
├── /knowledge                         Topic Directory
├── /knowledge/project/:scope          Project Topic
├── /knowledge/goal/:topicKey          Goal Topic
├── /knowledge/decision/:id            Decision Topic
├── /knowledge/:type/:id/backlinks     有界反向链接
├── /evidence                           现有 Widget 兼容入口
└── Evidence Drawer                     任意页的证据下钻
```

`/knowledge` 不是第二个搜索框。它先列出已发布的高价值主题（项目、目标、决策）、新鲜度和关联决策；长尾或未建页主题仍通过现有 Knowledge Search / Evidence Widget 处理。

### 4.2 Topic Page 固定结构

```text
标题 / 类型 / 当前状态 / 快照与新鲜度
├── 当前摘要（仅当前有效、具 Authority 绑定的内容）
├── 核心事实、观察与推断（明确分组）
├── 历史演变与生命周期
├── 关联目标、约束与项目
├── 关联 Decision → Action → Outcome
├── 外部上下文（仅存在且关联时；明确非个人事实）
├── 反向链接（仅显式、可证明的关系）
├── 局限、冲突与缺失信息
└── Evidence Drawer / 原始 Authority 下钻
```

页面不显示“人生总分”或把不同证据类型压缩为单一置信分。页面摘要不承担写操作；任何纠正入口必须跳转到现有生命周期或 Guarded Orchestration 流程。

### 4.3 反向链接

P0 只基于结构化共同键生成反向链接：`topic_id`、`domain`、`scope`、`recommendation_id`、`support.record_id`、已绑定 snapshot。不得把向量相似度或 LLM 猜测直接渲染为确定关系。

可选的“可能相关”只在后续 Candidate 区出现，必须显示计算方法、阈值、来源与不确定性，且不进入 P0 Backlinks。

## 5. GSD 候选执行切片

本规划不预留或激活 Phase 编号。当前 `ROADMAP.md` 仍将 Decision Cockpit 标为未激活候选，而工作树中已存在相应实现；先完成其独立验收与里程碑决策，再由新里程碑分配实际编号。

以下是按依赖和独立验收边界拆分的候选切片，不是固定“四阶段模板”：

| 切片 | 主要交付 | 先决条件 | 单独验收 |
|---|---|---|---|
| WIKI-01 Topic identity + read projection | `topic.list/get/backlinks` 合同、P0 topic key、快照/新鲜度/partial 信封 | Cockpit Projection 的实际契约稳定 | 无新事实库；只读；所有 topic 绑定 Authority 和 checksum |
| WIKI-02 Project / Goal / Decision pages | 目录、三种页面、Evidence Drawer 入口、现有 `/evidence` 兼容 | WIKI-01 | 每项当前结论可下钻；Fact/Observation/Inference 不混淆 |
| WIKI-03 Materialization + invalidation | 依赖记录、stale 判定、重建、Wiki-first 读取策略 | WIKI-01/02 和可靠 snapshot 变更信号 | 上游改变后旧页不能显示为 fresh；页面不回灌 KU/向量库 |
| WIKI-04 UAT + 扩域决策 | 隐私/可访问性/移动端/离线降级 UAT；是否引入 Skill、Career、External、LLM narrative 的证据评审 | WIKI-01..03 | 高价值主题可日常使用；后续扩域有可验证 identity 和效用依据 |

若 WIKI-01 发现当前 Authority 缺少稳定的 `scope`、snapshot 或 evidence 路径，应先以失败为结果收口该契约，不得在 WIKI-02 中用前端推测弥补。

## 6. 验收标准与测试策略

### 必须成立的产品事实

1. Wiki 不是新个人事实或外部事实 Authority；删除页面投影后，可从已绑定 Authority 重建。
2. 页面只读：不调用 Provider、不执行外部动作、不 promotion、不修改生命周期。
3. 每个 current 摘要都可显示 source snapshot、生成时间、新鲜度和至少一个下钻路径。
4. `stale`、`partial`、`unavailable` 三种状态视觉及 API 语义不同；不可用 Authority 不可伪装成功。
5. Personal 与 External 内容不混为同一种事实；Observation / Inference / Recommendation 不冒充 Fact。
6. Backlinks 只来自可解释关系；没有可证明关系时诚实显示为空。
7. Wiki 页面不会被写回 Active KU / Chroma，从而不会形成自引用检索循环。
8. P0 路由、键盘导航、320px 布局、长 ID、中文长文本、隐私封存及 REST 离线降级均可用。

### 最小验证层

| 层 | 验证 |
|---|---|
| Python 单元 | topic key、来源裁剪、事实类型分组、fresh/stale/partial 判定、backlink 来源白名单 |
| 服务契约 | UI Projection 信封、缺失/非法 topic 处理、`mode=ro`、Authority 失败隔离、snapshot/checksum 保留 |
| 前端单元 | Directory、三类 Topic Page、分组标签、Evidence Drawer、stale/partial/error 状态 |
| 集成 | 上游绑定变更后旧页面失效；重建后版本绑定变化；Wiki 内容不进入检索写路径 |
| E2E / UAT | 从总览/状态/决策进入主题页；查看历史与 evidence；REST/MCP 任一离线时局部降级 |

## 7. 风险、取舍与解除条件

| 风险 | 当前证据 | 约束/解除条件 |
|---|---|---|
| 将 Wiki 误当成 SSOT | 当前已有多个 Authority 与 Snapshot | 所有页面显式绑定上游；页面表只存投影/依赖元数据 |
| 主题身份漂移 | 当前没有通用 topic/entity registry | 首版只用 P0 可解释键；Skill 等待 identity 设计与样本验证 |
| 旧页面过期 | 当前 Authority 均以快照/运行版本提供读取 | WIKI-03 前不宣称持久化页面为 current；P0 先 on-demand 或明确 stale |
| LLM 幻觉摘要 | 当前有 LLM Candidate 体系但不存在 Wiki narrative validator | 首版 deterministic template；LLM 文案另走 Candidate/Eval/Publish |
| 隐私泄露 | Personal State 当前已采用 metadata-only 约束 | 复用出站封存；敏感正文不进入页面缓存、浏览器日志或测试 fixture |
| 页面数量失控 | KU 数量远高于适合浏览的主题数 | 只发布用户高价值 P0 types；不按词频或所有实体自动建页 |
| 与静态 docs/wiki 混淆 | 两者名称相同但职责不同 | 文档和产品在导航、路径、README 中明确区分；无自动迁移 |

## 8. 需要在激活新里程碑前确认的事项

这些不是本次阻塞性问题，因此本规格以保守默认值封存：

| 决策 | P0 默认 | 激活时确认 |
|---|---|---|
| 用户备注 | 不做；避免备注冒充事实 | 是否提供独立、可删除的 Note authority |
| Topic 创建 | 仅由 P0 可解释键产生 | 是否允许用户固定/隐藏主题及其 metadata 存储位置 |
| Skill / Career Page | 不进入 P0 | topic identity、别名、证据门槛和 lifecycle |
| 外部专题 | 只在相关 Topic 的外部区呈现 | 外部来源扩展、地区/时效与冲突策略 |
| LLM 叙述 | 不进入 P0 | Candidate schema、judge、发布和回滚标准 |
| Wiki-first | 只作为 UI 浏览优化 | 何时允许 Agent 优先读 Wiki、何时必须回退 KU/raw evidence |

## 9. 激活条件与下一步

激活前需满足：

```text
1. 明确 Cockpit 当前工作树变更的归属、验收和里程碑状态；
2. 新里程碑 Requirements 写明 Wiki 的产品目标、P0 types、隐私边界和不做项；
3. 选定 P0 只读、无 LLM narrative 的范围；
4. 为 WIKI-01 建立真实 Authority 样本与 topic identity 验收夹具；
5. 由用户明确授权创建新 Phase / PLAN.md 后，再运行完整 GSD research → plan → checker 流程。
```

在上述条件满足前，本文件是候选设计与 GSD 输入，不是已激活路线图、实现授权或产品完成声明。
