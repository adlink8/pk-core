---
analysis_type: expected-target-gap
project: Personal Knowledge System
date: 2026-07-17
status: historical_snapshot_superseded
method: GSD project/requirements/readiness audit + live database/CLI/test observations
related_audit: ARCHITECTURE-LAYERING-DATA-GOVERNANCE-AUDIT-2026-07-17.md
estimate_policy: percentages are planning estimates, not release metrics
---

# 个人数据分析项目：预期目标差距分析

> **Superseded 2026-07-18：**本文件保留为 2026-07-17 阶段性差距快照。当前权威目标差距分析见 [`TARGET-GAP-ANALYSIS-2026-07-18.md`](./TARGET-GAP-ANALYSIS-2026-07-18.md)；最终产品定义与现状说明见 [`PERSONAL-DECISION-INTELLIGENCE-VISION-STATUS-2026-07-18.md`](./PERSONAL-DECISION-INTELLIGENCE-VISION-STATUS-2026-07-18.md)。

## 1. 分析目的

### 2026-07-17 修复后距离更新

首次分析中的底层数据完整性阻断项已显著收窄：统一 Inventory registry、生产库 FK 清零、知识写连接 FK enforcement、doctor/publish gate、watermark-safe inspect、完整增量执行集合、依赖与治理门禁均已落地并验证。因此 Target A 的“真实数据完整性与运维落地”可由首次的 **60%–70%** 上调为约 **82%–88%**。

Target A 仍不能宣告退出：SQLite/Chroma 原子发布协议、D/S/R/A 类型化注册表、权威证据查询 view、真实 supersede/conflict adoption 尚未完成。Target B 仍被 Phase 17 人工 gold/judge/UAT 和当前检索质量阻断；Target C/D 的判断不变。

本文件将项目目标拆成四层，避免把“数据已经很多”“Phase 已完成”“CLI 能运行”和“个人智脑已经形成”混为同一件事。

项目核心价值是：

> 把个人历史转换为隐私安全、证据可回查、能够持续增量学习的外部知识系统。

因此需要分别回答：

1. 底层数据架构和治理是否可靠；
2. 当前 v1.1 评测与发布里程碑是否完成；
3. 是否已经成为稳定可日常使用的个人知识产品；
4. 距离主动理解、辅助决策和反馈闭环的个人智脑还有多远。

---

# 2. 目标层级

## Target A — 底层个人数据与知识治理基础

预期结果：

- Raw、Canonical、Turn、KU、Profile/Growth 各层职责固定；
- SQLite 是结构化事实与 lineage 主存储；
- Chroma 只是可重建检索空间；
- AgentView 只读，所有来源先进入快照/Canonical；
- 每条 KU 能回到原始 session/message/event；
- Incremental、Watermark、Candidate、Eval、Promote、Rollback 形成完整闭环；
- 生命周期能表达 current、superseded、conflict、deprecated 和 historical；
- 隐私、外键、版本、索引和文档状态均可自动验证。

## Target B — v1.1 Knowledge Unit Evaluation & Quality

项目自身定义的当前里程碑目标：

- 同一冻结协议下比较 Raw、L1、L2-only、L1+L2、Hybrid；
- 计算 Recall、MRR、nDCG、无答案误报、隐私命中和延迟；
- 衡量 L2 跨轮净增益、冲突、时效、重复和 grounded precision；
- 评测最终答案正确性、忠实度、引用和 abstain；
- Candidate 未通过门禁时禁止 Promote；
- 评测由单一 CLI/CI 可重复运行；
- 产生不可变 manifest、历史和可视化报告。

## Target C — 稳定可用的个人知识系统

预期结果：

- 新对话和新来源可以稳定增量同步；
- AI 查询默认使用当前有效 KU；
- 信息不足时受控 fallback 到 Turn、Canonical Message 或 Google signal；
- 当前事实、历史事实和冲突事实不混淆；
- CLI、REST、MCP 可以稳定运行；
- 用户可以查询“我现在是什么状态”和“我是如何变化的”；
- 系统不会泄露 thinking、secret、PII 或过期知识。

## Target D — 长期个人智脑

预期结果：

```text
持续采集
→ 识别变化
→ 形成候选认知
→ 与历史比较
→ 发现冲突、趋势和风险
→ 给出行动建议
→ 用户确认或纠正
→ 跟踪结果
→ 形成下一轮反馈
```

它不仅能回答“过去说过什么”，还应支持：

- 长期目标与实际行为偏差；
- 项目进展和阻塞识别；
- 学习、职业、健康、财务和关系决策；
- 主动复盘与提醒；
- 建议结果的后验验证；
- 个人算法和推荐排序持续演化。

---

# 3. 总体距离估算

百分比是规划估算，不是正式发布指标。

| 目标 | 当前估计 | 剩余差距 | 判断 |
|---|---:|---:|---|
| Target A：架构设计方向 | **80%–88%** | 12%–20% | 分层和治理思想先进且基本正确 |
| Target A：真实数据完整性与运维落地 | **60%–70%** | 30%–40% | FK、Delta Inventory、inspect、环境等存在底层缺口 |
| Target B：v1.1 评测代码与基础设施 | **75%–85%** | 15%–25% | 多路评测、报告和 Gate 已建 |
| Target B：v1.1 质量证明与人工签收 | **45%–60%** | 40%–55% | 完整评测 FAIL，Gold/Judge/UAT 未收口 |
| Target C：稳定个人知识产品 | **70%–78%** | 22%–30% | KU 主链、证据和 MCP 已形成；日常链仍有风险 |
| Target D：长期个人智脑 | **45%–55%** | 45%–55% | 当前主要完成“可查询知识”，主动决策闭环尚弱 |

需要同时保留两种评价：

> 项目的架构思想与工程骨架已经相当成熟。

但：

> 底层数据完整性、真实质量证明和主动反馈闭环仍没有达到最终目标。

---

# 4. Target A：底层架构与治理差距

## 4.1 当前已经具备

| 能力 | 当前状态 |
|---|---|
| Raw / Canonical / KU 分离 | 已具备 |
| AgentView 只读 | 已具备 |
| Canonical Conversation SSOT | 已具备 |
| KU Candidate / Canonical / Active Index | 已具备 |
| KU→Evidence 回查 | 当前 32,184/32,184 可追溯 |
| Candidate→Canary→Promote | 已具备主流程 |
| Active Pointer 与 rollback 思想 | 已具备 |
| 隐私扫描和敏感会话隔离 | 已具备 |
| CLI、REST、MCP 只读消费 | 已具备 |
| 生命周期和 history 命令 | 已实现 |

## 4.2 距离“底层搭稳”仍差什么

| 缺口 | 当前事实 | 达标要求 |
|---|---|---|
| SQLite 外键 | 默认 `foreign_keys=0` | 统一 connection factory，强制 FK |
| 历史完整性 | 18,859 条 FK 违规 | 迁移并清零或形成合法 subtype lineage |
| Inventory 模型 | Delta ID 外键错误指向 Full Inventory | 统一父表或显式 subtype |
| Incremental Inspect | 默认不读取 Watermark | 默认安全 no-op，禁止人工传 checksum 作为常规流程 |
| Refresh 输入 | `new/deleted refs` 执行列表截断 100 | Preview 与完整执行集合分离 |
| Composite SSOT | SQLite current 与 Active Chroma 可短暂分裂 | 版本快照和 serving authority 固定 |
| Turn 增量 | 不在主产品同步链 | 独立 watermark 或纳入 `pk-sync` |
| Google 更新 | 仍是 module/tribal path | 产品 CLI + source lifecycle |
| 状态命名 | lifecycle/current/status/active 混用 | 类型化命名空间 |
| Repository 治理 | preflight 仍有失败 | 12 门治理真正全绿 |
| 运行环境 | Python 3.12/3.14 分裂 | 单一可复现安装与 smoke |

## 4.3 Target A 的退出条件

只有同时满足以下条件，才可称“底层架构和治理基础完成”：

1. SQLite 所有生产连接启用 FK。
2. `foreign_key_check` 为 0，或所有历史例外有受控迁移表和豁免记录。
3. Delta/Full Inventory 使用合法且明确的身份模型。
4. `pk-ku inspect` 在 source 未变时默认 no-op。
5. 增量处理绝不因 preview 截断漏处理数据。
6. Canonical Conversation、Turn、KU、Active Index 各自有版本和 Watermark。
7. Serving 查询只消费同一版本快照，跨层 fallback 显示版本。
8. 生命周期状态机具有真实 supersede/conflict 数据和泄漏测试。
9. CLI、REST、MCP 和安装环境一致可运行。
10. planning、eval、runtime 和 governance 报告具有唯一 latest/supersedes 规则。

当前尚未完全满足 1、2、3、4、5、6、7、8、9、10。

---

# 5. Target B：v1.1 评测与发布目标差距

## 5.1 已经具备的能力

- Raw、L1、L2、L1+L2、Hybrid 多路比较框架；
- Recall@K、MRR、nDCG、延迟、隐私和 abstain 指标；
- JSON/SQLite/HTML/PNG 报告；
- Bootstrap 与 win/loss；
- Candidate override Canary；
- Strict gate；
- Active Pointer 不变检查；
- Promotion 和 rollback 命令；
- 当前 Candidate 的 30-query Canary PASS。

## 5.2 与里程碑要求的差距

### EVAL-01 / EVAL-02：多路检索和基础指标

**状态：基本具备。**

仍需使用当前 Active Collection 重新跑完整集，并固定 dataset/collection/version。

### EVAL-03 / EVAL-04：L2 跨轮净增益与 lineage

**状态：部分具备。**

系统已存在 L2 session window 和 lineage 报告，但仍需证明：

- 哪些问题只有 L2 能回答；
- L2 相比 L1 的新增覆盖；
- 重复与错误合并；
- 时效、冲突和隐私；
- pilot/full 数量差异可完整对账。

### EVAL-05：最终回答评测

**状态：不足。**

最近完整评测 `answer.skipped=true`，主要仍在检索层；需要实际评估最终 RAG Answer 的：

- correctness；
- faithfulness；
- citation precision/coverage；
- abstain accuracy；
- stale/current distinction。

### EVAL-06：Judge Calibration

**状态：未完成。**

Canary 包含已有标签、LLM 补标和人工 override，但尚缺统一校准报告，不能把所有 helpful 标签视为同等级 Gold。

### EVAL-07 / EVAL-08：不可变报告与可视化

**状态：大部分具备。**

但 `latest.txt` 无效，旧报告和新 Active 不对应，artifact supersedes 规则不完整。

### EVAL-09：Fail-closed Promotion

**状态：部分具备。**

流程和 strict gate 已存在，但仍需保证：

- Promote 默认必须绑定 eval/gate；
- SQLite publish 与 Chroma promote 的 split window 有明确语义；
- Watermark 只能在 Promote 成功后推进；
- rollback drill 有当前版本证据。

### EVAL-10：单命令/CI 复跑

**状态：部分具备。**

评测框架存在，但 Python 环境、产品 CLI、latest artifact 和治理 preflight 尚未形成完全一致的单入口。

## 5.3 当前质量证据与目标差距

旧完整评测：

| 指标 | 当前观测 |
|---|---:|
| Hybrid Recall@5 | 11.64% |
| MRR@5 | 7.65% |
| nDCG@5 | 8.66% |
| No-answer FP rate | 90.63% |
| Privacy hit | 1 |
| Gate | FAIL |

新 Candidate Canary：

| 指标 | 当前观测 |
|---|---:|
| Query count | 30 |
| Helpful rate | 96.67% |
| P95 | 152ms |
| Critical wrong/stale | 0 |
| Gate | PASS |

两组结果的正确解释是：

```text
Canary PASS
= 小范围上线检查通过

Full Eval FAIL
= 尚未证明广泛检索质量和安全目标达标
```

因此 v1.1 的代码与基础设施接近完成，但质量签收仍有约 40%–55% 工作量。

---

# 6. Target C：稳定个人知识产品差距

## 6.1 能力矩阵

| 子系统 | 当前估计 | 主要差距 |
|---|---:|---|
| AgentsView/Canonical Conversation | 88%–93% | 中文日志、ID continuity 和日常写入验证 |
| KU Extraction/Canonicalization | 82%–88% | API completion、merge gate、真实冲突处理 |
| KU Evidence | 92%–97% | 跨 DB FK 不可用，需要持续 integrity audit |
| Candidate/Active Index | 78%–85% | SQLite/Chroma 一致性与 registry |
| Layered Retrieval | 65%–75% | 低召回、no-answer、跨层版本一致性 |
| Lifecycle/Growth | 35%–45% 数据采用 | 几乎没有 supersedes/conflict |
| CLI/REST/MCP | 70%–80% | rag-search 依赖、服务启动和环境一致性 |
| Privacy | 82%–90% | 评测仍出现 privacy hit，secret gate 需收口 |
| Governance | 70%–80% | preflight shim/docs/secret 未全绿 |
| Product UX | 60%–70% | 当前更偏工程接口，缺少统一日常工作台和反馈 |

## 6.2 当前可以可靠做什么

- 保存并规范化大量个人对话；
- 从消息中抽取 KU；
- 通过 KU 进行知识优先检索；
- 回到证据消息；
- 通过 MCP/REST 只读访问；
- 查看基础 Profile、Graph、Data Browser；
- 生成 Candidate、Canary 和 Active Index；
- 保留历史数据而不硬删除。

## 6.3 当前还不能稳定保证什么

- 每次新增对话都安全、准确地只处理真实增量；
- 所有 console scripts 在同一环境可运行；
- 当前 Active Index 与 SQLite current 完全同步；
- 没有答案时可靠拒答；
- 旧目标不会被检索成当前目标；
- 冲突知识会自动形成正确成长线；
- Google、Turn、KU 使用同一源快照；
- 隐私命中始终为零；
- 系统能够主动发现变化而非只在查询时返回结果。

## 6.4 Target C 达标条件

- 连续多次真实增量周期无误报、漏处理和错误全量重建；
- 外键、证据、索引和 Watermark 全部完整；
- 当前 Active 完整评测通过；
- No-answer FP 降到门禁范围；
- Privacy/Secret Hit 为 0；
- 生命周期在真实高价值主题上运行；
- 默认检索 current-only，history 查询可解释变化；
- CLI/REST/MCP/服务启动具有统一可复现方式；
- 用户能直接查询当前状态、证据和成长历史。

---

# 7. Target D：长期个人智脑差距

## 7.1 当前所处阶段

当前系统主要完成：

```text
采集
→ 规范化
→ 提炼知识
→ 建立索引
→ 被动查询
```

长期个人智脑还需要：

```text
变化检测
→ 解释变化
→ 预测影响
→ 形成建议
→ 用户确认
→ 行动跟踪
→ 结果评估
→ 更新个人模型
```

## 7.2 尚缺的核心模块

### 主动变化检测

不仅发现“有新消息”，还需识别：

- 目标改变；
- 偏好改变；
- 项目停滞；
- 技能增长；
- 风险累积；
- 计划与行为不一致。

### 决策模型

知识库回答“事实是什么”，决策层还需处理：

- 目标；
- 约束；
- 选项；
- 代价；
- 风险；
- 时间窗口；
- 用户价值排序。

决策结果应是可审计建议，不是新的事实 KU。

### 行动与结果闭环

系统需记录：

```text
建议了什么
→ 用户是否接受
→ 实际做了什么
→ 结果如何
→ 原建议是否有效
```

否则系统只能积累信息，无法学习什么建议真正适合用户。

### 多领域协调

长期系统需要协调：

- 学习；
- 职业；
- 项目；
- 健康；
- 财务；
- 关系；
- 时间与精力。

这些领域不能简单合并为一个向量索引，需要目标、风险、时间和权限模型。

### 用户纠错与信任

需要显式支持：

- “这条记错了”；
- “这只是过去的我”；
- “不要把这类信息当长期记忆”；
- “这条只在某项目范围有效”；
- “这个建议没有效果”。

纠错必须形成治理事件，而不是直接覆盖历史。

## 7.3 个人智脑达标条件

1. 能自动生成可靠的近期变化摘要。
2. 能解释当前状态如何从历史发展而来。
3. 能区分事实、观察、推断、建议和用户确认。
4. 能对建议跟踪结果并评估有效性。
5. 能在隐私范围内跨领域做目标协调。
6. 能主动提示真正重要的变化，而不是制造通知噪声。
7. 所有高层判断均可下钻到证据和推理依据。
8. 用户可随时纠正、限制、撤销或改变知识生命周期。

当前主要完成了第 7 条的知识证据基础，其他条目仍处于早期或未系统实现。

---

# 8. 主要阻塞关系

```text
Schema/Incremental/SSOT 仍有缺口
    ↓
无法完全信任当前知识状态
    ↓
生命周期和成长线无法安全自动化
    ↓
高层画像可能混入旧知识或错误知识
    ↓
决策建议缺乏稳定事实基础
    ↓
主动个人智脑无法建立可信反馈闭环
```

正确顺序应是：

```text
数据完整性
→ 增量与版本一致性
→ 检索质量
→ 生命周期真实治理
→ 主动变化检测
→ 决策与行动反馈
```

---

# 9. 推荐阶段划分

## Stage 1 — Foundation Integrity

- 修复 FK 与 Inventory 模型；
- 修复 inspect/preview truncation；
- 固定 SQLite/Chroma serving snapshot；
- 为 Turn/Google 增加版本和产品同步；
- 统一 Python、CLI、UTF-8 和 doctor health。

## Stage 2 — Evaluation Closure

- 使用当前 Active 跑完整 Phase 17；
- 完成 Human Gold 与 Judge Calibration；
- 评测最终答案；
- 降低 no-answer FP；
- Privacy/Secret gate 为 0；
- 完成 Promote/Rollback UAT。

## Stage 3 — Lifecycle Adoption

- 选择职业、项目、技能、偏好等高价值 Subject；
- 建立真实 supersede/conflict/correction；
- 验证 current-only retrieval；
- 建立 Growth History 与变更摘要。

## Stage 4 — Personal Intelligence Loop

- 主动变化检测；
- 目标与约束模型；
- 建议与行动记录；
- 结果反馈和建议评估；
- 跨领域排序与提醒；
- 用户纠错和信任控制。

---

# 10. 最终判断

该项目目前已经是：

> 一个隐私优先、证据可追溯、具有 Candidate/Active 发布治理的个人知识基础设施。

但还不是完整的：

> 会持续理解用户变化、主动辅助决策并从行动结果中学习的个人智脑。

当前最大的目标差距不是继续增加“记忆层”，而是：

```text
底层数据完整性
+ 安全增量
+ 复合 SSOT 一致性
+ 当前质量证明
+ 生命周期真实采用
+ 主动变化检测
+ 决策与反馈闭环
```

后续新功能必须建立在这些底层差距逐步关闭的基础上。
