# Codex Session 019f63ff — Hierarchical Thinking Distill

> 只提炼**分层 / 层级 / 治理指针**思维，不复述整段 transcript。  
> 会话主体在 **novel-mind**；对 **personal-knowledge** 的映射见第 4 节。

---

## 1. Session meta

| Field | Value |
|-------|--------|
| **Session id** | `019f63ff-3576-70f2-b698-e9b3c01db76e` |
| **Rollout path** | `$HOME\.codex\sessions\2026\07\15\rollout-2026-07-15T12-18-22-019f63ff-3576-70f2-b698-e9b3c01db76e.jsonl` |
| **Visualizations dir** | `$HOME\.codex\visualizations\2026\07\15\019f63ff-3576-70f2-b698-e9b3c01db76e\`（空，无 summary HTML/md） |
| **Started (session_meta)** | 2026-07-15T04:18:22Z（本地文件名 12-18-22 为本地时区） |
| **Primary cwd** | `<sibling-repo>` |
| **Branch / git (meta)** | `feat/phase2-wave2-embedding` · `5e0fa287…` |
| **Originator** | Codex Desktop / vscode · model path 含 gpt-5.6-sol |
| **Hierarchy “mind model” peak turns** | ~04:19 UI 缩放层级；~06:58 分析编排依赖；**~09:10–09:13 分层叙事记忆 + 是否重分析**；~09:24 research_architecture → `.planning/research/ARCHITECTURE.md` |

**会话主线（非完整 transcript）：**

1. 时间线 998 节点堆叠 → **UI progressive disclosure**  
2. 统一“开始分析”编排（时间线 → 关系/线索）  
3. 阅读器/书架等产品修复  
4. 用户灵感：“大模型一层层 / RAG 一层层” → **分层叙事记忆 + dry-run sidecar** 规划  

---

## 2. Core hierarchical model（bottom → top）

会话中实际沉淀了**三套可叠加、但不可混为一谈**的层级。编号从底到顶。

### A. 事实与证据栈（Phase 07 不变式 · 强制 SSOT 底）

```text
L0  原文 Chapter.content（坐标 + content hash）
L1  Evidence（可重切、可校验的叶子）
L2  Scene
L3  Chapter（hierarchy root；每章恰好一个）
```

- 代码事实：`chapter → scene → evidence` 确定性组装（Phase 07）。  
- **上层任何 claim 的 evidence closure 最终必须落到 L0/L1**；similarity score / 聊天文本不可充当真值。  
- 会话结论（~09:11）：现状已是「原文 → 证据 → 场景 → 章节 → 分析模块」，但**各分析模块仍是并行读同一套证据**，不是“前一层理解完成才喂下一层”。

### B. 叙事记忆 sidecar（规划中的“真·逐层 RAG”）

在 **不改写** A 的前提下，独立 candidate version 自下而上物化：

```text
L4  Chapter State（章级状态 / claims + source links）
L5  Story Arc / Volume（连续、无重叠章节区间）
L6  Global Story Model（全书主线 / 人物状态 / 未决）
```

- 来源：用户 09:10 灵感 + assistant mermaid（L0…L4 命名）+ research `ARCHITECTURE.md` 的 `Chapter State → Arc → Global`。  
- **Sidecar 原则**：不扩展 `ChunkHierarchyNode.level`；不把 arc/global 塞进 Phase 07 表。  
- **双向**：自下而上归纳；查询时自上而下路由，但必须 **coarse-to-fine + 叶子 fallback**（禁止不可回退的严格剪枝）。

### C. 派生分析产品线（可选输入 · 非知识真值）

```text
Timeline events (candidate → promote → active pointer)
  └─ Relationships（绑定 promoted analysis_version）
  └─ Clues（依赖 timeline version / 证据）
Reader chat / 检索（冻结 manifest + cutoff）
```

- 相对 B：这些是 **evidence-backed 可选派生**，lineage 不合格 → `unavailable/ignored`，不得替代原文 evidence。  
- 编排约束（~06:58–07:01）：**不能真的三任务同时抢跑**；关系必须等 timeline **晋升版本**后才有 `analysis_version_id`。

### D. UI 信息层级（progressive disclosure · 与数据层同构）

| 缩放层级 | 主画面 | 点击后 |
|---------|--------|--------|
| 全书概览（~998 事件） | 密度带/阶段聚合，**不**渲染每点标题 | 缩放到区间 |
| 章节/局部（~10–50） | 圆点 + 少量关键标题 / 泳道 | 打开详情 |
| 近距离（≤15） | 完整标题、因果线 | 原文证据 |

- 核心句（~04:19）：问题不是“把字缩小”，而是 **「概览」与「阅读详情」分层**。  
- 原型口号（~04:33）：**全书聚合 → 章节下钻 → 事件详情**。

### E. 发布 / 治理层级（candidate-first）

```text
freeze sources
  → build (append-only candidate)
  → validate (manifest recompute, evidence closure)
  → evaluate / dry-run report
  → CAS promote active pointer（最后一步）
  → journal / rollback 能力
```

- MVP：**dry-run 禁止写任何 active pointer**（chunk / timeline / relationship / clue / 未来 memory）。  
- 局部失败：只重跑失败 stage（章 / arc / global），不整本推倒。

---

## 3. Key ideas distilled

### 3.1 用户灵感与矫正（~09:10–09:11）

- **用户**：大模型“一层一层下来”；RAG 也可以“一层一层”。  
- **Assistant 矫正**：方向对，但不是模仿“模型层数”，而是 **分层叙事记忆系统**；应是 **双向**（bottom-up 物化 + top-down 检索），不是单向下传。  
- **现状缺口**：模块并行读证据 ≠ 层级理解管线。

### 3.2 复用 vs 重分析（~09:13）

- **结论：不需要整本从头重新分析。** 正确方式是 **「复用底层、增量补建上层」**。  
- 可直接复用（checksum 一致时）：章节、证据、向量、场景层级、坐标引用、已确认 timeline/关系/线索。  
- 只新增：`Chapter State → Arc → Global`。  
- 才重跑：原文变了、旧层级失败/checksum 不一致、结果无法追溯证据、缺字段、模块无数据。  
- 策略：资产审计 → 合格直接升上层；缺失只重跑对应章节/模块；新版本验证后再切 **active pointer**。

### 3.3 Sidecar + lineage（research_architecture / ARCHITECTURE.md）

- 新层用 **独立 memory-owned version/run/stage/node/edge/source-link/report**；**不要**复用 timeline 的 `AnalysisVersion` 发布语义。  
- 上层 claim 必须算 **evidence closure**，最终回到冻结 Phase 07 的原文坐标与哈希。  
- Timeline / relationship / clue 只作 **可选、可追溯派生输入**。  
- 检索：**adaptive coarse-to-fine**（上层路由 + 多层候选融合 + 叶子 fallback）；不采用“上层选错就永久剪枝”。  
- score **只用于排序**，不是事实置信度。

### 3.4 编排依赖队列（产品路径思维）

- 一次“开始分析”→ 三条**持久任务意图**；timeline 先跑；关系/线索显示等待；**promote 后**自动续跑。  
- 后续任务失败 **不回滚** timeline；独立失败原因与重试。  
- 数据层差异 ≠ UI 层差异：无 published 版本 vs 候选事件数 ≤ 阈值误进“局部图”——应用统一先概览。

### 3.5 UI = 层级的人机接口

- 不要在全书视图展示每个节点标题。  
- 列表默认只渲染 **当前视窗**；搜索/筛选/按章浏览补可达性。  
- 面包屑：`全书 > 第 7–10 章`。

---

## 4. Implications for personal-knowledge（pk-sync / pk-ku / canary）

会话在 novel-mind，但分层心智与本仓库产品硬规则 **同构**。建议这样对照，而不是照搬小说领域名词。

| novel-mind（会话） | personal-knowledge（本仓库） |
|--------------------|-----------------------------|
| 原文 / dialogue 源 | **Dialogue SSOT**（`pk-sync conversations` → `agent_conversations.sqlite`） |
| Phase 07 hierarchy build + checksum | 冻结证据 / inventory lineage；**只抽 delta**（`pk-ku prepare` 队列） |
| Knowledge 真值 ≠ 并行模块输出 | **Knowledge SSOT = KU + active index**，不是 memory / personal_events 实验层 |
| candidate vector / dry-run | `pk-ku vector --write`（**不**碰 active） |
| canary / eval labels | `pk-ku canary`（`--strict` 需 labels） |
| CAS promote active pointer | `pk-ku promote`（`--require-eval-pass`） |
| watermark after promote | `pk-ku watermark` |
| 不整本重分析 | **禁止**日常全量 inventory + `prod --start`；inspect 有 delta 而 prepare `no_op` → **停**，不换全量路径 |
| 编排依赖队列 | 对话先 sync，再 KU 增量；策略用 **CLI flags**，不为日常改代码 |
| score ≠ truth | 检索分数不替代 KU 规范事实；labels 与 knowledge units 分离 |

**可直接沿用的治理句：**

1. **底向上建、顶向下查、叶子可回落。**  
2. **candidate 先验证，active pointer 最后动。**  
3. **上层是 sidecar/增量，不推翻合格底层。**  
4. **派生层（memory 实验、旧 pipeline）永远不是 SSOT。**  
5. **产品入口收敛：`pk-sync` + `pk-ku`；`rag-pipeline` 已退役。**

---

## 5. What NOT to conflate（anti-patterns from the session）

| # | 不要混淆 | 会话依据 |
|---|----------|----------|
| 1 | **模型层数 ≠ 知识层数** | 09:11：不是简单模仿“大模型层数” |
| 2 | **UI 聚合层 ≠ 数据事实层** | 密度带/阶段是展示；evidence 仍在叶子 |
| 3 | **并行分析模块 ≠ 分层理解管线** | 时间线/关系/线索并行读证据，尚未成为 L4→L6 输入链 |
| 4 | **严格 top-down 剪枝 ≠ 好的 hierarchical RAG** | 应 adaptive + leaf fallback（RAPTOR collapsed-tree 直觉） |
| 5 | **摘要/similarity ≠ evidence** | claim 必须 evidence closure 到原文 hash |
| 6 | **塞进同一 hierarchy 表的更多 level ≠ 扩展能力** | 会破坏 Phase 07 invariant / 增量 rebuild |
| 7 | **挂到 timeline AnalysisVersion 的 memory ≠ 独立 memory version** | 扩大发布语义，dry-run 难隔离 |
| 8 | **点击“开始”= 三任务真并发** | 依赖 promoted version；先排队后续跑 |
| 9 | **无 published 版本 = UI bug** | 可能是候选取消/未 promote；先审计数据层 |
| 10 | **事件少就跳过概览层** | 会破坏一致的信息层级；应统一先概览 |
| 11 | **整本重跑 = 升级架构** | 应复用底层 + 增量上层 + 局部失败恢复 |
| 12 | **candidate 构建中写 active pointer** | dry-run / vector 路径零 pointer 写入 |
| 13 | **可选派生缺失 = 底层失败** | optional 源应降级计数，不伪装成空或阻断 required |
| 14 | **（PK 映射）memory 层 / rag-pipeline = knowledge SSOT** | 与本仓库 Agents.md 硬规则一致：禁止 |
| 15 | **（PK 映射）labels 过 canary 与 KU 单元本身** | canary 是索引/评测门；KU 是规范知识单元；promote 才切换 active |

---

## 6. Short quote bank（approx. time）

| Time (UTC) | Speaker | Paraphrase / quote |
|------------|---------|-------------------|
| 04:19 | Assistant | 「核心不是把文字缩小，而是把概览和阅读详情**分层**。」 |
| 04:19 | Assistant | 全书概览 → 章节范围 → 近距离阅读 三档缩放。 |
| 04:33 | Assistant | 「全书聚合 → 章节下钻 → 事件详情」。 |
| 06:58 | Assistant | 不能三任务真同时跑；时间线先，关系/线索等版本后续跑。 |
| 09:10 | User | 大模型一层层下来 → RAG 也可以一层层。 |
| 09:11 | Assistant | 方向对；建的是**分层叙事记忆**，不是模仿模型层数；要双向。 |
| 09:13 | Assistant | 「不需要整本从头重新分析…**复用底层、增量补建上层**。」 |
| 09:29 | research_architecture | Sidecar `Chapter State → Arc → Global`；evidence closure；dry-run 禁改 active pointer；adaptive coarse-to-fine。 |

---

## 7. Artifact pointers（session-produced hierarchy design）

- `<sibling-repo>\.planning\research\ARCHITECTURE.md` — 分层记忆 + dry-run + retrieval 完整架构研究  
- Session rollout：见 §1 path（~4066 lines jsonl）  

---

*Extracted 2026-07-16 for `数据分析` cleanup / mind-model archive. Scope: hierarchy thinking only.*
