---
analysis_type: current-expected-target-gap
project: Personal Decision Intelligence System
analysis_date: 2026-07-18
status: current_authoritative_release_ready
supersedes: TARGET-GAP-ANALYSIS-2026-07-17.md
vision_source: PERSONAL-DECISION-INTELLIGENCE-VISION-STATUS-2026-07-18.md
method: GSD PROJECT/REQUIREMENTS/STATE/ROADMAP + Phase 24-27 verification + current source inspection
estimate_policy: percentages are planning estimates, not release metrics
---

# 个人决策智能系统：最新预期目标差距分析

> **Live closure update:** 本文后文的 Phase 24 失败、Live schema unapplied 和 persisted rows=0 是收口前的时点证据，已被 2026-07-18 的真实运行结果取代。Phase 24 三个 checkpoint、strict review、strict lifecycle、Phase 25–27 validated committed live runs 与用户 Product UAT 均 PASS，当前里程碑 release-ready。PDI-T2 External Context 与 PDI-T3 LLM Decision Analysis 仍是后续产品差距。

## 0. 收口后权威证据

- Active collection: `knowledge_units_ir_4cd8af4ad_20260718054940`
- Active snapshot: `ss_5d816a6bf3ebd0bce9463236`
- Final evaluation: `3a4b7f7b85e864b86031a79a0c017fa74c80e5b9908aa7fd73e765343fcc5d99`
- Recall@5 improvement: `+10.4478pp`; CI lower bound: `+4.4776pp`
- Lifecycle: 6 events, 2 applied manifests
- Phase 25: `psr_3a28363b9d1c6d9ab656fde5`
- Phase 26: `dfr_e367f7689d64ad96a10311bd`
- Phase 27: `pir_065c80888c81723abd43fc4a`
- Automated acceptance: release-ready, technical PASS, Product UAT PASS, zero side effects

---

## 1. 本次更新纠正了什么

旧分析将最终目标概括为“长期个人智脑”，容易把知识检索、项目状态分析、主动提醒和完整决策智能混为一体。

当前权威目标修正为：

> **以长期个人数据作为内部状态，以社会、行业、政策、市场等外部环境作为外部状态，以用户目标、价值和约束作为决策条件，通过受控 LLM 与确定性规则生成证据可追溯、风险可解释、结果可反馈的个人决策建议。**

项目状态只是 `project` 决策域中的一个输入，不是系统本体。系统默认不替用户执行外部动作，也不取得用户的最终决策权。

本文件取代 `TARGET-GAP-ANALYSIS-2026-07-17.md` 对最终目标和当前距离的判断；旧文件保留为历史快照。

---

# 2. 最终目标链路

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
          决策问题识别与候选方案构造
                    ↓
       收益 / 风险 / 机会成本 / 情景比较
                    ↓
       Recommendation Candidate + 不确定性
                    ↓
         用户确认 / 拒绝 / 修改 / 延迟
                    ↓
              Action / Outcome
                    ↓
         Effectiveness / 偏差 / 副作用评估
                    ↓
             后续建议持续校准
```

底层原则：

```text
程序确定事实与硬约束
LLM 构造解释、方案和情景分析
规则验证证据、时效、风险和权限
用户保留目标定义与最终决策权
真实结果反过来校准系统
```

---

# 3. 目标分层

为避免继续混用项目 Phase 完成率和产品完成度，当前目标分为五层。

## PDI-T0 — 数据、知识与发布治理基础

目标：

- Raw、Canonical、Turn、KU、Personal State、Decision、Proactive 各层职责固定；
- SQLite、Chroma、Watermark、Serving Snapshot 和证据版本一致；
- 所有高层结果可回到合法个人证据；
- Candidate、Evaluation、Active、Rollback 分离；
- 隐私、权限、版本、数据完整性和失败边界可自动验证。

## PDI-T1 — 可信个人状态与变化模型

目标：

- 从长期数据形成目标、约束、偏好、能力和当前状态；
- 识别真实变化、冲突、趋势和风险；
- 正确区分 current、superseded、conflict、corrected 和 historical；
- 能解释“当前状态如何从历史形成”；
- 默认只使用当前有效知识，缺证据时 abstain。

## PDI-T2 — 外部环境情报权威

目标：

- 接入社会、行业、政策、岗位、市场、价格和宏观环境；
- 每条外部事实具有来源、时间、地区、可信度和冲突状态；
- 外部环境与个人 KU 完全隔离；
- 形成不可变 `External Context Snapshot`；
- 决策分析绑定明确的 Personal + External 双快照。

## PDI-T3 — LLM 决策分析与建议候选

目标：

- 从 Personal State + External Context 构造 Decision Case；
- 生成候选方案、不行动基线和情景比较；
- 分析收益、成本、风险、机会成本、不可逆性和缺失信息；
- 所有事实主张引用 evidence ID；
- 建议永远是 Candidate，不成为事实或执行权限；
- 高风险、冲突、过期或信息不足时明确 abstain。

## PDI-T4 — 真实决策、行动结果与长期校准

目标：

- 用户确认目标、价值权重和风险预算；
- 记录 Recommendation→Decision→Action→Outcome；
- 评估时间、成本、收益、副作用、遗憾和满意度；
- 不把相关性包装成因果；
- 证明个性化建议相对通用 LLM 建议有真实增益；
- 从低风险领域逐步扩展到多领域。

---

# 4. 收口前项目状态（历史证据，已被第 0 节取代）

## 4.1 PDI-T0：底层治理已经接近完成

当前已完成：

- Phase 23 的 D/S/R/A 类型注册表；
- Composite Serving Snapshot；
- SQLite、Chroma、Conversation、Turn、Google、KU 版本与 Watermark 绑定；
- 统一证据下钻；
- fail-closed Doctor 与 Governance；
- Candidate/Active/Promotion/Rollback 主链；
- Inventory/FK/Inspect/增量执行边界修复；
- 当前 Active Collection：`knowledge_units_ir_4cd8af4ad_20260716020508`；
- 当前 Active Serving Snapshot：`ss_1590353394c948b908a5d675`。

当前主要剩余：

- 真实知识检索质量没有达到产品门槛；
- 生命周期机制没有真实采用样本；
- Phase 25–27 Live schema 尚未授权应用；
- 运行数据和产品价值证据仍不足。

## 4.2 Phase 24：当前正式阻塞点

Phase 24 验证结果：`1/4`。

| Requirement | 状态 | 当前事实 |
|---|---|---|
| QUAL-01 | FAIL | 总体检索提升 2.99pp，要求 10pp |
| QUAL-02 | PASS | 隐私、引用、abstain、grounding 和 review provenance 通过 |
| LIFE-01 | FAIL | Live lifecycle manifest/action/event 均为 0 |
| LIFE-02 | FAIL | 没有真实 correction/supersede/conflict/restore 可逆序列 |

检索根因证据：

- 170 个 Gold KU ID 都存在于 Active Index；
- self-semantic 检查中 47/50 能在 rank 1 找到自身；
- 45 个真实 cross-turn query 中，仅 5 个能在前 500 条结果找到预期 Gold；
- 当前向量主要嵌入 canonical question + answer；
- 真实查询往往更接近 member evidence 的语义，而不是规范化问答文本。

这说明：

```text
知识已经存在
但真实问题与知识表示没有充分对齐
```

在该问题关闭前，上层个人状态和建议系统可能使用不完整上下文，因此产品发布保持阻断是正确的。

## 4.3 Phase 25：个人状态技术骨架通过，真实产品未运行

已经实现：

- `a.personal_change` 独立 A-layer authority；
- 不可变 Personal State Run；
- goal / constraint / observation / inference 类型边界；
- bitemporal current-state projection；
- change / conflict / resolution / trend / risk；
- metadata-only explanation；
- CLI、REST、MCP 共享只读接口；
- 版本、证据、隐私和跨快照漂移时 abstain。

但当前：

```text
Live schema = unapplied
persisted_rows = 0
真实 Personal State Run = 0
真实变化/趋势/风险记录 = 0
```

因此 Phase 25 是技术合同完成，不是产品状态模型已经运行。

## 4.4 Phase 26：决策反馈合同通过，但没有 LLM 决策

已经实现：

- `a.decision_feedback` 独立 authority；
- fact / observation / inference / recommendation / user_confirmation 分离；
- Recommendation 不具有 KU、事实或执行权；
- Confirmation / Action / Outcome / Effectiveness append-only；
- checksum、sequence、idempotency、concurrency 和 tamper fail-closed；
- REST/MCP 只读；本地写入需要明确确认。

当前建议引擎实际是：

```text
Versioned deterministic rules
+ structured input
+ explicit abstention
```

当前不存在：

- LLM 自动识别 Decision Case；
- LLM 自动生成候选方案；
- 情景模拟和机会成本分析；
- 真实 Recommendation；
- 真实 Decision / Action / Outcome；
- 基于真实结果的建议校准。

## 4.5 Phase 27：主动协调技术通过，但只有沙箱证明

已经实现：

- 八领域：learning、career、project、health、finance、relationship、time、energy；
- 多领域目标、约束和有限资源冲突；
- importance、novelty、dedup、cooldown、quiet period、noise budget；
- suppress、snooze、scope、restore 和 correction routing；
- 只读 inbox、digest、explain 和 metrics；
- 无 scheduler、sender、executor 或外部连接器。

但当前：

```text
Live Phase 27 schema = unapplied
真实 proactive candidate = 0
真实用户控制历史 = 0
真实多领域 usefulness UAT = 0
```

Technical Target D 已通过，不代表 Product Target D 已完成。

## 4.6 外部环境情报层：基本不存在

当前 adapter 主要是：

- `agentsview.py`；
- `google_activities.py`。

它们属于个人数据来源，不是社会环境权威。

当前缺少：

- 政策、法律和教育制度来源；
- 行业、岗位和技能需求来源；
- 公司和技术趋势来源；
- 宏观经济、市场和价格来源；
- 新闻事件与来源质量模型；
- External Context Schema、Snapshot、Watermark；
- 地区、有效期、可信度和多来源冲突治理。

## 4.7 LLM 决策推理层：基本不存在

`src/personal_knowledge/intelligence` 当前没有生产 LLM 调用。

现有 Phase 25–27 主要提供：

```text
Schema
+ authority
+ rule engine
+ state machine
+ evidence/privacy/version gates
+ sandbox acceptance
```

这是一套优良的 LLM 接入底座，但还不是 LLM 决策产品。

---

# 5. 当前完成度估算

百分比用于规划，不是发布 KPI。

| 目标维度 | 当前估计 | 剩余差距 | 判断 |
|---|---:|---:|---|
| PDI-T0 数据与治理架构 | **88%–94%** | 6%–12% | 底层 authority、snapshot、证据和治理已很成熟 |
| PDI-T1 可信个人知识检索 | **60%–70%** | 30%–40% | 安全门较强，cross-turn 语义对齐未达标 |
| PDI-T1 生命周期真实采用 | **10%–20%** | 80%–90% | 机制存在，真实 reviewed action/event 为 0 |
| Personal State 技术骨架 | **85%–92%** | 8%–15% | 技术通过，Live schema 和真实数据未形成 |
| Decision Feedback 技术骨架 | **85%–92%** | 8%–15% | 状态机成熟，没有真实建议和结果 |
| Proactive Intelligence 技术骨架 | **82%–90%** | 10%–18% | 沙箱机制成熟，没有真实主动使用 |
| PDI-T2 外部环境情报层 | **5%–15%** | 85%–95% | 尚无正式外部环境 authority |
| PDI-T3 LLM 决策推理层 | **5%–15%** | 85%–95% | 尚无生产 LLM 决策调用 |
| PDI-T4 真实单领域决策产品 | **5%–10%** | 90%–95% | 尚无真实 Decision→Outcome 试点 |
| 最终个人决策智能愿景 | **35%–45%** | 55%–65% | 底座贡献大，但核心决策价值尚未证明 |

### 为什么最终愿景不是只有 10%

因为项目已经完成大量不可替代的底座：

- 长期个人数据规范化；
- 证据与隐私治理；
- 当前知识和历史知识边界；
- Serving Snapshot；
- Personal State、Decision Feedback、Proactive 的 typed contracts；
- 用户信任控制和零外部执行边界。

这些是最终系统的重要组成部分。

### 为什么也不能评为 70% 以上

因为最终产品的核心价值仍未形成：

- 没有外部社会环境；
- 没有 LLM 决策分析；
- 没有真实个人状态 publication；
- 没有真实建议、行动和结果；
- 没有证明个性化建议优于通用 LLM；
- 没有长期真实用户校准数据。

---

# 6. 最大目标差距

## Gap 1 — 知识存在，但检索表示不匹配真实问题

这是当前最先需要关闭的阻塞。

目标不是简单提高一个 Recall 数字，而是保证：

```text
真实用户问题
→ 找到正确 Personal Evidence / KU
→ 正确构造 Current State
→ 再进入 Decision Analysis
```

建议方向：

- evidence-aware candidate embedding；
- canonical QA + member evidence 的受控表示；
- 隐私安全的多字段或多向量表示；
- query rewriting / retrieval routing；
- cross-turn 专用评测；
- 不降低现有质量门槛。

## Gap 2 — 生命周期只有机制，没有真实历史

当前不能可靠回答：

- 哪个目标已经被替代；
- 哪个偏好只是过去状态；
- 哪些知识存在冲突；
- 哪次用户纠正改变了当前状态。

需要建立小规模、高价值、人工审核的真实 lifecycle cohort，而不是为了通过测试制造事件。

## Gap 3 — Personal State、Decision、Proactive 仍未进入 Live Authority

Phase 25–27 schema 尚未应用，意味着真实系统还没有：

- Personal State Run；
- Decision Run；
- Recommendation Stream；
- Outcome History；
- Proactive Inbox。

上线前必须有明确授权、迁移、回滚和最小真实 UAT。

## Gap 4 — 缺少外部环境

没有 External Context，系统只能根据个人历史给建议，不能真正回答：

- 当前岗位市场是否变化；
- 某技术是否值得投入；
- 政策是否改变机会和风险；
- 价格、经济和社会环境是否改变方案优先级。

## Gap 5 — 缺少 LLM 决策分析

当前系统只能验证已经结构化好的 Recommendation Input，不能自行完成：

- 决策问题定义；
- 方案发现；
- 情景构造；
- 多目标权衡；
- 机会成本分析；
- 解释性建议。

## Gap 6 — 缺少真实结果和后验校准

没有真实 Outcome，就不能知道：

- 建议是否可执行；
- 时间和成本预测偏差；
- 建议是否带来副作用；
- 用户真正重视什么；
- 哪些建议只是在语言上显得合理。

---

# 7. 正确推进顺序

```text
Phase 24 检索质量修复
        ↓
真实 lifecycle adoption
        ↓
授权并应用 Phase 25–27 Live schema
        ↓
生成真实 Personal State Run
        ↓
建立 External Context Authority
        ↓
接入受控 LLM Decision Analysis Candidate
        ↓
低风险单领域真实试点
        ↓
收集 Decision / Action / Outcome
        ↓
校准建议与风险模型
        ↓
逐步扩展多领域
```

不建议跳过前四步直接接入一个“万能决策 Agent”。那会让 LLM 建立在未验证知识和空生命周期之上。

---

# 8. 候选后续阶段

这些阶段仍是 Candidate，不代表已经激活。

## Candidate Phase 28 — External Context Authority

目标：建立社会、行业、政策、市场和价格的独立事实权威。

关键退出条件：

- External Source Registry；
- Canonical External Event / Indicator；
- 来源可信度、地区、有效时间和冲突；
- External Snapshot / Watermark；
- 与个人 KU 的物理和语义隔离；
- Personal + External 双快照绑定。

## Candidate Phase 29 — LLM Decision Analysis Candidate

目标：让 LLM 只生成结构化决策分析候选。

关键退出条件：

- `DecisionCaseInput` / `DecisionAnalysisOutput` 严格 Schema；
- 所有事实引用 evidence ID；
- 方案、基线、收益、成本、风险、假设和停止条件完整；
- 模型、prompt、schema、token、cost 和版本可审计；
- 缺证据和高风险时 abstain；
- 不写 KU、不修改状态、不执行外部动作。

## Candidate Phase 30 — Low-risk Domain Pilot

建议第一批领域：

```text
学习 / 项目 / 职业
```

原因：

- 风险较低；
- 目标和结果相对容易定义；
- 用户可以明确纠正；
- 可以验证时间、完成率和实际收益。

暂缓：

```text
医疗诊断 / 自动投资交易 / 高风险财务 / 重大关系决定
```

## Candidate Phase 31 — Recommendation Calibration and Product UAT

目标：证明系统建议具有真实个性化价值。

指标至少包括：

- 接受率；
- 执行率；
- 完成率；
- 预计时间与实际时间偏差；
- 预计成本与实际成本偏差；
- 用户满意度；
- negative side effect；
- regret；
- 与通用 LLM 建议的对照增益。

---

# 9. 完成标准

最终个人决策智能系统不能仅凭代码或沙箱验收宣布完成。至少需要：

1. 当前知识检索质量在真实问题上通过门禁。
2. 真实生命周期能够解释当前与历史状态。
3. Personal State、Decision 和 Proactive authority 已在 Live 环境受控运行。
4. External Context 具有独立、可追溯、可版本化权威。
5. LLM 输出严格受证据、风险和权限约束。
6. 至少一个低风险领域形成真实 Decision→Outcome 闭环。
7. 用户可以纠正事实、建议、权重和生命周期。
8. 系统能表达不确定性和明确拒绝判断。
9. 真实结果证明个性化建议优于无个人历史的通用建议。
10. 所有高层结论可下钻到 Personal / External Evidence。

---

# 10. 最终判断

项目目前已经是：

> **具有强数据治理、个人知识、状态建模、决策反馈和主动协调技术合同的个人智能基础设施。**

但还不是：

> **真正结合长期个人数据和外部社会环境，通过 LLM 持续生成并根据真实结果校准个性化建议的个人决策智能产品。**

当前最重要的不是继续增加抽象层，而是把已有技术骨架沿着以下纵向链路跑通：

```text
可信知识
→ 真实当前状态
→ 外部环境
→ 受控 LLM 决策分析
→ 用户选择
→ 真实行动结果
→ 后验校准
```

最新估计：

```text
最终愿景当前完成度：约 35%–45%
剩余差距：约 55%–65%
```

其中最大的未完成部分不是工程框架，而是：

```text
外部环境
+ LLM 决策推理
+ 真实用户决策数据
+ 长期结果反馈
+ 个性化增益证明
```
