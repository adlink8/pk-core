---
document_type: corrected-product-vision-and-current-status
project: Personal Knowledge / Decision Intelligence System
date: 2026-07-18
status: current_milestone_release_ready
source_of_truth_inputs:
  - .planning/STATE.md
  - .planning/ROADMAP.md
  - .planning/REQUIREMENTS.md
  - Phase 24-27 verification artifacts
  - current intelligence source tree
related:
  - ARCHITECTURE-LAYERING-DATA-GOVERNANCE-AUDIT-2026-07-17.md
  - TARGET-GAP-ANALYSIS-2026-07-17.md
---

# 基于长期个人数据与外部环境的个人决策智能系统

> **2026-07-18 closure update:** Phase 24 质量与 lifecycle 短板已用真实数据关闭，Phase 25–27 Live schemas 和真实 State→Decision→Action→Outcome→Assessment→Proactive 链已运行，用户 Product UAT 已通过，当前里程碑 release-ready。本文后文中“schema unapplied / rows=0 / Phase 24 blocked”为收口前历史证据。External Context Authority 与真正 LLM Decision Analysis 仍属后续长期愿景。

## 1. 目标纠正

本项目的最终目标不是“项目状态 Agent”，也不是替用户执行事务的自动化 Agent。

正确目标是：

> **以长期个人数据作为内部状态，以社会、行业、政策、市场等外部环境作为外部状态，以用户目标、价值和约束作为决策条件，通过 LLM 与确定性规则生成证据可追溯、风险可解释、结果可反馈的个人决策建议。**

项目状态只是八个个人决策领域中的一个输入域：

```text
学习 / 职业 / 项目 / 健康 / 财务 / 关系 / 时间 / 精力
```

系统不取得用户的最终决策权，也不默认执行外部动作。

---

## 2. 最终系统定义

```text
长期个人数据
+ 当前个人状态
+ 外部社会环境
+ 历史决策、行动与结果
+ 用户目标、价值、风险与约束
                    ↓
              状态与变化建模
                    ↓
          外部环境相关性与时效分析
                    ↓
       决策问题识别、方案构造与情景比较
                    ↓
     收益 / 风险 / 机会成本 / 不确定性评估
                    ↓
        个性化建议、替代方案与停止条件
                    ↓
          用户确认、拒绝、修正或延迟
                    ↓
             行动、结果与效果记录
                    ↓
            后验评估与策略持续校准
```

最准确的产品定位：

> **Personal Decision Intelligence System — 个人决策智能系统。**

---

# 3. 收口前项目状态（历史证据，已被页首 closure update 取代）

## 3.1 已经完成的底层能力

### 数据与知识基础

- Raw、Canonical Conversation、Turn、KU 和 Serving Snapshot 已分层。
- AgentsView 作为只读上游，Canonical Conversation 是对话事实入口。
- KU、Turn、Canonical Message 和 Google Signal 可以通过统一契约下钻到证据。
- SQLite、Chroma、source watermarks 和评测证据由复合 Serving Snapshot 绑定。
- 当前 Active KU collection：`knowledge_units_ir_4cd8af4ad_20260716020508`。
- 当前 Active Serving Snapshot：`ss_1590353394c948b908a5d675`。
- D/S/R/A 类型注册表、版本、水位、Doctor 和 fail-closed 治理已经完成 Phase 23 技术验收。

### 当前项目健康基础

Phase 25 验证时：

- Phase 25 相关测试 87 项通过；
- 邻接回归 33 项通过；
- Governance Preflight 13/13；
- 全仓 723 passed、2 skipped。

Phase 26 验证时：

- Phase 26 相关测试 69 项通过；
- 邻接回归 120 项通过；
- 全仓 792 passed、2 skipped。

Phase 27 验证时：

- Phase 27 相关测试 90 项通过；
- Phase 25/26 邻接回归 156 项通过；
- Governance Preflight 13/13；
- 全仓测试通过。

这些结果证明技术合同和失败边界存在，不等于真实个人决策产品已经上线。

---

## 3.2 Phase 25：个人状态与变化智能

**技术状态：PASSED。产品状态：RELEASE BLOCKED。**

已经实现：

- 独立 `a.personal_change` A-layer authority；
- 不可变、snapshot-bound 的 Personal State Run；
- 目标、约束、观察、推断的类型边界；
- Current State Projection；
- Bitemporal `valid_at / observed_at / as_of` 边界；
- Change、Conflict、Resolution、Trend、Risk；
- Metadata-only explanation；
- CLI、REST、MCP 共享只读服务；
- 缺失证据、跨快照、隐私、版本漂移时 abstain。

尚未形成：

- Live schema migration；
- Live Personal State publication；
- 真实用户长期状态样本；
- 真实近期变化报告；
- 真实趋势和风险评估记录。

当前 Live Acceptance 明确报告：

```text
analysis_schema_unapplied
persisted_rows = 0
mutations = 0
network_calls = 0
paid_calls = 0
```

因此 Phase 25 是**技术骨架完成**，不是**个人状态产品已运行**。

---

## 3.3 Phase 26：决策、行动与反馈闭环

**技术状态：PASSED。产品状态：RELEASE BLOCKED。**

已经实现：

- 独立 `a.decision_feedback` A-layer authority；
- `fact / observation / inference / recommendation / user_confirmation` 类型边界；
- Recommendation 不能成为 KU、事实、命令或执行权限；
- 用户接受、拒绝、延迟和修正；
- Action、Outcome、Effectiveness append-only history；
- Outcome 只能是观察；Effectiveness 是 `causal_claim=false` 的推断；
- Sequence、checksum、idempotency、concurrency 和 tamper fail-closed；
- REST/MCP 只读；本地写入需要显式确认。

当前 Recommendation Engine 的真实实现是：

```text
Versioned deterministic recommendation rules
+ explicit abstention
```

`src/personal_knowledge/intelligence/decision/recommendations.py` 没有 LLM 调用。它依赖人工或上游提供的结构化 target、expected benefit、costs、assumptions 和 support，再用确定性规则决定 eligible 或 abstain。

尚未形成：

- LLM 自动构造真实 Decision Case；
- LLM 生成候选方案和情景分析；
- 真实建议发布；
- 真实用户确认、行动和结果；
- 基于真实结果的建议校准；
- 对生活决策有效性的产品证据。

Live Decision schema 当前同样是 `unapplied`，没有真实决策记录。

---

## 3.4 Phase 27：主动多领域协调

**Technical Target D：PASSED。Product Target D：RELEASE BLOCKED。**

已经实现：

- 独立 `a.proactive_intelligence` A-layer authority；
- 八个固定领域：learning、career、project、health、finance、relationship、time、energy；
- 跨领域目标、约束和有限资源冲突；
- Importance、Novelty、Dedup、Cooldown、Quiet Period、Domain/Global Noise Budget；
- 用户 Suppress、Snooze、Scope、Restore 和 Correction routing；
- 只读 Inbox、Digest、Explain 和 Metrics；
- 隐私、证据和 Trust veto 优先于重要性排序；
- 无通知发送器、调度器、外部执行器或连接器。

尚未形成：

- Live Phase 27 schema；
- 真实主动建议候选；
- 真实用户噪声偏好和控制历史；
- 真实多领域资源协调；
- 真实长期 usefulness UAT；
- 基于外部环境的主动机会/风险识别。

Phase 27 的 Sandbox 证明机制可以运行，但 fixture 身份、fixture 目标、fixture 行动和 fixture 结果不能证明真实个人价值。

---

# 4. 当前最大阻塞：Phase 24

Phase 24 当前为 `gaps_found`，4 个要求中仅 1 个通过。

| Requirement | 状态 | 当前事实 |
|---|---|---|
| QUAL-01 | FAIL | L1+L2 总体提升 2.99pp，门槛 10pp |
| QUAL-02 | PASS | 隐私、引用、abstain、grounding 和 review provenance 通过 |
| LIFE-01 | FAIL | Live lifecycle manifest/action/event 均为 0 |
| LIFE-02 | FAIL | 无真实 correction/supersede/conflict/restore 可逆序列 |

真实检索根因证据：

- 170 个 Gold KU ID 均存在于 Active Index；
- self-semantic sample 中 47/50 能 rank 1；
- 45 个真实 cross-turn query 中，仅 5 个能在前 500 名找到预期 Gold；
- 当前向量文本只嵌入 Canonical Question + Answer；
- 真实查询常与 member evidence 语义相似，而不与标准化 Question/Answer 表述相似。

因此现在的根问题不是“缺少更多 Agent”，而是：

```text
底层知识存在
但真实问题无法稳定检索到正确知识
                    ↓
个人状态、建议和主动智能会建立在不完整上下文上
                    ↓
技术 Target D 不能安全转化成产品 Target D
```

---

# 5. 与最终目标相比，当前真正缺失的系统层

## 5.1 外部环境情报层：当前不存在

当前 adapters 只有：

```text
agentsview.py
google_activities.py
```

Google Activities 是个人行为数据，不是社会环境情报。

项目目前没有正式的：

- 政策、法律和教育制度数据源；
- 招聘岗位和技能需求数据源；
- 行业、公司和技术趋势数据源；
- 宏观经济与市场环境数据源；
- 商品、住房、投资、生活成本等价格数据源；
- 可靠新闻事件和来源质量模型；
- External Context Snapshot；
- 外部事实的有效期、地区、可信度和冲突治理；
- External Context 与个人 KU 的隔离边界。

未来必须建立独立的外部环境权威：

```text
External Source
→ Canonical External Event / Indicator
→ Source Quality / Time / Region / Confidence
→ External Context Snapshot
→ Decision Case Support
```

外部环境数据不能写成“用户事实 KU”。

---

## 5.2 LLM 决策推理层：当前不存在

搜索 `src/personal_knowledge/intelligence` 未发现 LLM 调用。

Phase 25–27 当前主要是：

```text
确定性 Schema
+ 规则引擎
+ 状态机
+ 不可变记录
+ 证据/隐私/版本门禁
+ 沙箱验收
```

这恰好是未来接入 LLM 所需的安全底座，但还不是 LLM 决策系统。

未来 LLM 应负责：

- 从 Personal State + External Context 识别 Decision Case；
- 补全候选方案，但不能发明事实；
- 分析收益、风险、机会成本和不可逆性；
- 进行情景分析和反事实比较；
- 输出推荐、备选、停止条件和缺失信息；
- 解释建议与个人目标的关系；
- 对无法判断的问题明确 abstain。

LLM 不应负责：

- 修改 KU 或原始事实；
- 自动确认用户价值权重；
- 自动执行购买、投资、医疗、求职或关系行为；
- 将建议写回为个人事实；
- 绕过 evidence、privacy、risk 和 review gates。

---

## 5.3 决策目标函数：当前只存在结构，没有真实个人权重

最终系统需要针对不同决策域表达：

```text
目标
硬约束
偏好权重
风险预算
时间窗口
不可逆性
机会成本
最低可接受结果
停止条件
```

当前 Phase 27 能协调资源 ID、单位和时间范围，但尚未形成真实、经用户确认的个人价值函数。

不能把所有生活领域压缩为一个总分。应使用：

```text
Multi-objective ranking
+ hard constraints
+ domain-specific risk policy
+ user-confirmed trade-offs
```

---

## 5.4 真实反馈学习：当前只有合同，没有真实数据

系统已支持：

```text
Recommendation
→ Confirmation
→ Action
→ Outcome
→ Non-causal Effectiveness
```

但当前没有真实序列，因此尚不能知道：

- 哪类建议用户会接受；
- 哪类建议虽然正确但无法执行；
- 用户通常高估多少时间；
- 哪些领域建议有效；
- 哪些风险模型过于保守或激进；
- 哪些建议造成负面副作用；
- 用户价值是否发生变化。

没有真实 Outcome 数据，就不能称为“持续校准的个人算法”。

---

# 6. 当前完成度重新判断

百分比只用于规划，不是正式 KPI。

| 目标维度 | 当前估计 | 说明 |
|---|---:|---|
| 数据与治理架构 | **88%–94%** | Phase 23 已完成复合 SSOT、版本、水位和证据契约 |
| 个人知识检索产品质量 | **60%–70%** | 安全门较强，但 cross-turn 检索增益不达标 |
| 生命周期真实采用 | **10%–20%** | 机制存在，Live reviewed action/event 仍为 0 |
| Personal State 技术骨架 | **85%–92%** | Phase 25 技术通过，但 Live schema/unapplied、无真实 run |
| Decision Feedback 技术骨架 | **85%–92%** | Phase 26 技术通过，但无 LLM 和真实决策序列 |
| Proactive Intelligence 技术骨架 | **82%–90%** | Phase 27 技术通过，但只有 sandbox 和 metadata-only acceptance |
| 外部环境情报层 | **5%–15%** | 尚无正式社会/行业/政策/市场数据权威 |
| LLM 决策推理层 | **5%–15%** | Intelligence package 中没有 LLM 生产调用 |
| 真实个人决策产品 | **20%–30%** | 基础设施和合同很强，Live 数据、外部环境、LLM、UAT 均未形成 |
| 最终个人决策智能愿景 | **35%–45%** | 底座贡献较大，但核心实际决策价值仍待证明 |

这里必须区分：

```text
Technical Target D PASSED
≠
Product Target D PASSED
≠
最终个人决策智能系统完成
```

---

# 7. 建议的正确总体架构

## 7.1 内部个人状态域

```text
Personal Facts / Observations
→ Goals / Constraints / Preferences
→ Current State
→ Changes / Trends / Risks
```

Authority：Phase 23–25。

## 7.2 外部环境域

```text
Public Sources
→ Canonical External Facts / Events / Indicators
→ Source Quality + Time + Region + Confidence
→ External Context Snapshot
```

必须是独立 authority，禁止混入个人 KU。

## 7.3 决策案例域

```text
Personal State Snapshot
+ External Context Snapshot
+ User Goal / Constraint / Risk Policy
→ Decision Case
```

Decision Case 至少包含：

- 决策问题；
- 时间窗口；
- 候选方案；
- 不采取行动的基线方案；
- 收益；
- 成本；
- 风险；
- 不可逆性；
- 缺失信息；
- 适用/停止条件。

## 7.4 LLM 决策分析域

```text
Deterministic facts and constraints
→ LLM option construction / scenario analysis
→ Structured Decision Analysis Candidate
→ Rule validation / risk gate / evidence check
→ Recommendation Candidate
```

LLM 输出永远是 Candidate，不是事实和最终决定。

## 7.5 用户主权与反馈域

```text
Recommendation
→ Accept / Reject / Modify / Defer
→ Action
→ Outcome
→ Effectiveness
→ Calibration
```

Authority：Phase 26–27 的 append-only 结构。

---

# 8. 候选后续阶段

以下是下一里程碑的候选方向，不代表已批准进入 Active Roadmap。

## Candidate Phase 28 — External Context Authority

目标：建立社会、行业、政策和市场环境的独立数据权威。

成功标准：

- 外部来源注册表、可信度、时间、地区和 license/provenance；
- Canonical External Event/Indicator；
- 外部数据版本、Snapshot、Watermark 和失效；
- 多来源冲突和 stale detection；
- Personal KU 与 External Fact 物理、语义和权限隔离；
- 决策读取必须绑定 Personal + External 两个 Snapshot。

## Candidate Phase 29 — LLM Decision Analysis Candidate

目标：接入 LLM，但只生成结构化决策分析候选。

成功标准：

- 严格 `DecisionCaseInput` 和 `DecisionAnalysisOutput` Schema；
- 所有事实引用 Personal/External evidence ID；
- 方案、收益、风险、成本、假设、不确定性和停止条件完整；
- 模型不得写 KU、修改 Personal State 或执行动作；
- 缺证据、冲突、高风险、隐私问题时 fail closed；
- 模型、prompt、schema、temperature、cost 和 token lineage 可审计。

## Candidate Phase 30 — Domain Decision Pilot

目标：选择一个低风险、结果可验证的领域进行真实试点。

建议第一试点：

```text
学习 / 项目 / 职业规划
```

暂不优先：

```text
医疗诊断 / 自动投资交易 / 高风险财务 / 关系重大决定
```

成功标准：

- 真实 Decision Case；
- 用户明确确认目标和权重；
- 建议、选择、行动和结果完整；
- 没有自动外部执行；
- 用户能纠正事实和建议；
- 建议 usefulness、执行率和后验偏差可评估。

## Candidate Phase 31 — Recommendation Calibration and Product UAT

目标：根据真实结果校准建议系统，而不是只校准语言质量。

成功标准：

- 建议接受率、执行率、完成率和满意度；
- 预计成本/时间与实际偏差；
- 推荐方案与备选方案结果比较；
- Negative side effect 和 regret 记录；
- 无因果证据时禁止宣称建议导致结果；
- Human override、撤销和信任控制通过真实 UAT。

---

# 9. 正确执行顺序

当前不应跳过 Phase 24，直接宣布个人决策智能上线。

正确顺序：

```text
1. 修复 evidence-aware retrieval
2. 关闭真实生命周期 adoption
3. 授权并应用 Phase 25–27 Live schema
4. 生成真实 Personal State Run
5. 建立 External Context Authority
6. 接入受控 LLM Decision Analysis Candidate
7. 进行低风险单领域真实试点
8. 收集 Action / Outcome / Effectiveness
9. 根据真实结果校准
10. 再扩展多领域
```

---

# 10. 最终判断

项目目前不是一个“项目状态 Agent”。

它现在是：

> **已经具备强数据治理、个人知识、状态建模、决策反馈和主动协调技术合同的个人智能基础设施。**

但尚未成为：

> **真正使用长期个人数据和外部社会环境，通过 LLM 为用户持续生成并校准个性化决策建议的产品。**

最大差距集中在：

```text
真实检索质量
+ 真实生命周期采用
+ Live Personal State / Decision / Proactive 数据
+ 外部环境情报权威
+ LLM 决策推理
+ 真实用户行动与结果反馈
+ 产品 UAT 和长期 usefulness 证明
```

后续规划必须以本文件中的最终目标为准：

> **决策增强，不是项目管理；建议支持，不是自动执行；个人与社会双状态建模，不是只读取个人历史。**
