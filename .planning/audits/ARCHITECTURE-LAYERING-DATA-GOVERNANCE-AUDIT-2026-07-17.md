---
audit_type: architecture-layering-data-governance
project: Personal Knowledge System
date: 2026-07-17
status: gaps_found
method: gsd-docs-update + gsd-audit-milestone + gsd-health + live code/database/CLI/test verification
scope:
  - data zones and semantic layers
  - SSOT and active index authority
  - lineage, lifecycle, incremental refresh and promotion
  - schema integrity and repository governance
  - documentation and runtime drift
non_goals:
  - feature completion percentage
  - deleting or reconciling private data
  - promoting or changing the active collection
  - fixing source code in this audit
---

# 个人数据分析项目：架构、分层与数据治理完整审计

## 1. 审计结论

### 2026-07-17 修复后快照

本报告正文保留首次审计时的原始事实。其 P0 修复结果如下：

- `PK-GOV-001/002/003/004`：知识生产写连接统一启用 FK；Full/Delta Inventory 已迁移到统一 registry；生产库 `foreign_key_check=0`；doctor 与 publish/promote gate 均 fail-closed。
- 增量安全：`pk-ku inspect` 默认读取 committed watermark；执行集合与 preview 分离，超过 100/500 条的增量测试通过。
- 环境与治理：Python 3.12 声明依赖已安装；`rag-search stats --json` 正常；治理 preflight 12/12 PASS（shim 只降不升，合成 secret fixture 显式标记）。
- 验证：Python 全量 pytest PASS（2 skipped）；Node 11/11 PASS；Active collection、Watermark 和 32,184 条 SQLite/Chroma KU 保持一致。

仍未关闭的主要差距是 Phase 17 人工 gold/judge/UAT、检索质量指标、SQLite/Chroma 跨存储原子发布协议、D/S/R/A 类型化注册表和真实 lifecycle adoption。这些不应被本轮 P0 修复描述为已完成。

项目已经建立了正确的主线：

```text
Raw sources
→ Canonical conversations/events
→ Turn narratives / draft Knowledge Units
→ Canonical Knowledge Units
→ Candidate vector index
→ Eval/Canary
→ Active index
→ Layered retrieval / REST / MCP
```

其优势是：原始记录与知识分离、KU 有证据链、Candidate/Active 分离、Active Pointer 可回滚、AgentsView 只读、隐私门禁存在、历史知识不物理删除。

当前最关键的问题不是增加新的“记忆层”，而是底层治理契约仍有若干高风险缺口：

1. **SQLite 声明了外键但默认未启用，现库存在 18,859 条外键违规。**
2. **Delta Inventory 使用独立表，却被 `knowledge_run_items.inventory_id` 外键错误地指向 Full Inventory 表。**
3. **`pk-ku inspect` 默认不读取 Watermark，直接运行会产生大规模虚假增删差异。**
4. **KU SQLite current 与 Active Chroma 是组合 SSOT，但两者的原子发布边界仍需更严格定义。**
5. **生命周期机制已实现，但真实数据几乎全部仍为 current，成长线尚未真正治理。**
6. **综合评测 FAIL 与小规模 Canary PASS 并存，二者不能互相替代。**
7. **软件架构、数据成熟度、语义层、发布生命周期仍需要分开命名。**
8. **规划、运行、评测和兼容层文档存在多处状态漂移。**

建议固定四个正交命名空间：

| 命名空间 | 解决的问题 | 本项目示例 |
|---|---|---|
| `D*` Data maturity | 数据成熟度 | Raw → Staging → Canonical → Derived → Serving |
| `S*` Semantic | 语义抽象 | Message → Turn → KU → Profile/Growth interpretation |
| `R*` Release lifecycle | 发布状态 | Draft → Candidate → Evaluated → Active → Rolled back |
| `A*` Architecture | 软件依赖 | Delivery → Application → Core/contracts → Adapters |

禁止再把目录层、知识层和发布状态统一称作普通 `L1/L2/L3`。

---

## 2. 建议固定的数据与语义模型

### 2.1 数据成熟度

```text
D0 Raw
  外部原始导出、AgentsView live、Google 原始数据；只读/保真

D1 Staging
  临时清洗、批次和候选输入；可丢弃重建

D2 Canonical
  canonical_sessions/messages、规范 Google 结构；事实 SSOT

D3 Derived Knowledge
  Turn narrative、draft KU、canonical KU、关系/画像派生物

D4 Serving
  Active Chroma、REST、MCP、只读报表；可由 D2/D3 重建
```

### 2.2 语义抽象

```text
S0 Message / Event
  原始事实记录

S1 Turn
  一次用户发起到下一次用户发起前的任务叙述单元

S2 Knowledge Unit
  可复用的事实、偏好、能力、习惯、决策或工具使用知识

S3 Growth/Profile Interpretation
  多个 KU 的时间变化、冲突、趋势和高层解释
```

S3 不是新的 SSOT。画像和成长解释必须回到 S2 KU，再回到 S0/S1 证据。

### 2.3 发布生命周期

```text
R0 Draft/Staging
→ R1 Canonical Candidate
→ R2 Vector Candidate
→ R3 Evaluated/Canary
→ R4 Active
→ R5 Rolled back / Deprecated / Historical
```

`current` 是知识内容生命周期，`active` 是索引发布生命周期，两者不能混为一类状态。

---

# 3. 完整问题清单

状态说明：

- **OPEN**：当前需要处理。
- **P0**：会破坏 SSOT、增量安全或数据完整性。
- **DESIGN DEBT**：方向正确但契约未收口。
- **DOC DRIFT**：文档与当前代码/运行事实不一致。
- **OBSERVED ADOPTION GAP**：能力存在但真实数据尚未使用。
- **INTENTIONAL BOUNDARY**：有意隔离，不应错误移除。

## 3.1 Schema 与数据完整性

### PK-GOV-001 — SQLite 外键默认未启用

**状态：OPEN / P0 DATA INTEGRITY**

实际连接验证：

```text
PRAGMA foreign_keys = 0
```

项目多数代码使用直接 `sqlite3.connect(...)`，搜索只发现少数路径显式执行 `PRAGMA foreign_keys=ON`。因此 schema 中的 `REFERENCES` 默认不具有运行时约束力。

风险：

- orphan rows 可持续写入；
- 删除/迁移不会被数据库阻止；
- 测试可能在无 FK enforcement 的环境中错误通过；
- “证据链完整”依赖应用逻辑，而不是数据库保证。

**治理要求：**统一 connection factory，每次连接启用 FK；CI 必须执行 `foreign_key_check`；迁移前先分类历史违规，禁止直接开启后让生产写入随机失败。

### PK-GOV-002 — 当前数据库存在 18,859 条外键违规

**状态：OPEN / P0 DATA INTEGRITY**

实际执行：

```text
PRAGMA foreign_key_check
```

结果：

| 表 | 违规数 |
|---|---:|
| `knowledge_run_items` | 18,858 |
| `knowledge_extraction_gates` | 1 |
| 合计 | 18,859 |

违规不是随机脏数据，而是集中指向同一个 Delta Inventory：`di_9e002cdac7af1460`。

### PK-GOV-003 — Delta Inventory 与 Full Inventory 的外键模型错误

**状态：OPEN / P0 SCHEMA DESIGN BUG**

Schema 定义：

```text
knowledge_run_items.inventory_id
  REFERENCES knowledge_inventory(inventory_id)

knowledge_extraction_gates.inventory_id
  REFERENCES knowledge_inventory(inventory_id)
```

但增量流程实际使用：

```text
knowledge_delta_inventories.delta_inventory_id = di_...
```

因此 `di_9e002cdac7af1460` 的 18,858 个 work items 和 1 个 gate 在 schema 语义上全部是 orphan。

这是典型的“多种 Inventory 类型共用一个字符串字段，但外键只指向其中一种表”的问题。

**可选修复方向：**

1. 建立统一 `knowledge_inventories` 父表，full/delta 作为 subtype；或
2. 将 run 明确拆成 `full_inventory_id` 与 `delta_inventory_id`，CHECK 恰好一个非空；或
3. 建立 `inventory_kind + inventory_id` 并由应用/trigger 强约束，但不推荐无真实 FK 的多态引用。

### PK-GOV-004 — Foreign Key 声明与运行事实不一致，治理报告未覆盖

**状态：OPEN / GOVERNANCE GAP**

全量 pytest 当前通过，但未阻止 18,859 条 FK 违规；`pk-ku doctor` 也未执行 FK check。现有“healthy”只证明路径、文件、Active Pointer 和 Watermark，不证明关系完整性。

`doctor` 应至少报告：

- `PRAGMA foreign_keys`；
- `foreign_key_check` 按表计数；
- canonical member/evidence orphan；
- active index version 与 build/run lineage。

### PK-GOV-005 — Canonical KU 的证据链是间接链，缺少统一查询契约

**状态：DESIGN DEBT**

当前正确链路是：

```text
canonical_knowledge_units
→ canonical_unit_members
→ knowledge_units
→ knowledge_unit_evidence
→ canonical_message
```

本次验证 32,184 个 Canonical KU 均可通过该链回到 evidence，覆盖率 100%。但直接将 `canonical_unit_id` 与 `knowledge_unit_evidence.unit_id` 连接会得到 0，容易被新代码、Agent 或审计脚本误判为“全部无证据”。

**建议：**提供权威 SQL view/repository，例如 `canonical_knowledge_evidence_view`，禁止消费者自行拼接链路。

### PK-GOV-006 — `evidence_ref` 未声明到 Canonical Message 的数据库外键

**状态：DESIGN DEBT**

`knowledge_unit_evidence.evidence_ref` 只是 TEXT 注释为 canonical_message_id。由于 canonical conversation 位于另一 SQLite DB，无法使用普通跨库 FK。当前完整性完全依赖应用回查。

需要明确：

- evidence validation job；
- source DB checksum；
- missing evidence lifecycle；
- canonical store 重建后 ID 稳定性；
- evidence tombstone，而非静默丢失。

---

## 3.2 增量更新与 Watermark

### PK-INC-001 — `pk-ku inspect` 默认不读取 Watermark

**状态：OPEN / P0 OPERATIONAL SAFETY**

文档和 `pk-ku workflow` 要求直接执行：

```text
pk-ku inspect
```

但 `_cmd_inspect` 只有操作者显式传 `--source-checksum` 才把 checksum 传给 refresh。默认值为空，导致代码跳过 checksum no-op。

本次实际结果：

```text
pk-ku doctor:
  watermark matches source checksum

pk-ku inspect:
  source_changed=True
  new_refs=1,534
  deleted_refs=31,817

pk-ku inspect --source-checksum <watermark>:
  source_changed=False
  no_op=True
  new_refs=0
  deleted_refs=0
```

因此当前推荐命令会产生错误的大规模变更判断。

### PK-INC-002 — Watermark 与 Inventory Diff 是两个概念，但 CLI 没有正确编排

**状态：OPEN / DESIGN DEBT**

Watermark 表示已发布源快照；Inventory Diff 表示当前 canonical evidence 与冻结 inventory 的差异。正确流程应由系统自动读取 committed watermark，再决定是否需要做 diff。当前 CLI 把 checksum 作为可选人工参数，导致安全性依赖操作者知识。

### PK-INC-003 — Refresh detail 将 `new_refs/deleted_refs` 截断为前 100 条

**状态：OPEN / P0 WRITE-PATH BUG**

`find_affected_evidence()` 返回：

```python
"new_refs": list(new_refs)[:100]
"deleted_refs": list(deleted_refs)[:100]
```

后续 write path 和 pipeline command builder 使用的正是这些截断后的列表。若直接执行 refresh write：

- 最多只会处理前 100 个 deleted refs；
- 最多只会为前 100 个 new refs生成命令；
- 统计数字与实际处理范围不一致。

用于展示的采样字段不应同时作为执行输入。必须分成 `*_count`、完整内部集合和 `*_preview`。

### PK-INC-004 — 受影响 Subject 只检查最多 500 个 deleted refs

**状态：OPEN / REPORTING ACCURACY**

`affected_subjects` 查询使用 `deleted_refs[:500]`。当变更规模大于 500 时，报告不能代表全部影响范围，却没有 `truncated=true` 或 omitted count。

### PK-INC-005 — Canonical rebuild/ID 变化可能制造大规模删除假象，缺少 ID continuity audit

**状态：DESIGN DEBT**

本次无 Watermark 的 diff 显示 31,817 deleted。虽然主要根因是未传 checksum，但系统仍应区分：

- 真正消息删除；
- canonical ID 算法变化；
- session merge/rebuild 导致 ID 替换；
- eligibility policy 变化；
- source snapshot 切换。

否则 lifecycle reconcile 可能把“身份迁移”误判为“知识失效”。

### PK-INC-006 — Turn Summary / Turn Vector 不在主日常增量 CLI 中

**状态：OPEN / LAYER STALENESS RISK**

`pk-sync conversations` 负责 Canonical Conversation，`pk-ku` 负责 KU；Conversation Summary 和 `conversation_turns` vector 仍是模块/独立流程。结果可能出现：

```text
Canonical messages 已更新
KU 已更新
conversation_turns 仍是旧版本
```

由于 layered retrieval 会 fallback 到 conversation turns，这会形成跨层快照不一致。

需要为 Turn 层定义独立 watermark/version，或将其纳入 `pk-sync` 可选但可审计的阶段。

### PK-INC-007 — Google Light 更新仍未进入统一产品 CLI

**状态：OPEN / CROSS-SOURCE GOVERNANCE**

Google normalized events/assertions 仍依赖 module commands。它既不是 KU，又可能参与 fallback。缺少统一的：

- source watermark；
- lifecycle stage/gate/promote；
- product CLI；
- 与 conversation/KU 的一致快照声明。

---

## 3.3 SSOT、发布与索引一致性

### PK-SSOT-001 — Knowledge SSOT 实际是复合权威

**状态：DESIGN DEBT**

文档定义 Knowledge SSOT 为：

```text
canonical_knowledge_units + active Chroma collection
```

这不是单一存储，而是“事实内容 + 当前服务索引”的复合权威。必须明确查询语义：

- SQLite current 中存在但 Active index 未包含的 KU，算不算当前知识？
- API `/knowledge` 的 unit count 来自哪边？
- Active index 落后时是否 fallback SQLite lexical？
- Promotion 前 publish additive 已改变 SQLite current，是否形成短暂 split-brain？

### PK-SSOT-002 — Publish SQLite 与 Promote Chroma 不是原子事务

**状态：OPEN / CONSISTENCY GAP**

增量链先将 staging KU publish 为 SQLite current，再构建 Candidate vector、Canary、Promote。期间：

```text
SQLite current = 新知识
Active Chroma = 旧索引
```

项目通过流程顺序和 Watermark 控制缓解，但仍需显式定义“Serving SSOT 只认 Active generation snapshot”，并记录 Active generation 对应的 canonical build/version，而不是直接读取所有 current rows。

### PK-SSOT-003 — Active Pointer 是文本文件，数据库也有 index version 状态

**状态：DESIGN DEBT**

当前 Active Pointer 位于 `var/db/knowledge_index_active.txt`，同时 `knowledge_index_versions.status` 也能表达 active/candidate/rolled_back。双重状态需要 CAS 与 reconcile，避免：

- 文件指向 A，DB active 标记 B；
- 写文件成功但 DB journal 失败；
- rollback 只恢复一侧；
- 多进程竞争覆盖。

建议固定一个提交权威，另一个只作可重建 cache/兼容指针。

### PK-SSOT-004 — Chroma 存在大量历史/空/小说集合，缺少强类型 Collection Registry

**状态：OPEN / INDEX GOVERNANCE**

实际 Chroma 同时包含：

- 多代 `knowledge_units_*`；
- `conversation_turns`；
- `personal_events`；
- 多个 `novel_*`；
- 空测试/旧 run collection。

Collection 名称承载类型和生命周期，但缺少一个统一 registry 强制记录 owner、purpose、source snapshot、embedding model、dimension、status、retention 和可删除条件。

### PK-SSOT-005 — `memory_items` 和旧 Memory pipeline 仍可能被误认为知识权威

**状态：OPEN / LEGACY SURFACE RISK**

文档已明确 memory experiment 不是 KU SSOT、`rag-pipeline` 默认退出。但数据库表、模块、图谱、compat shims 和旧报告仍大量存在。需要继续保证：

- product retrieval 默认不消费 memory_items；
- REST/MCP 字段命名不把 experimental memory 称为 authoritative knowledge；
- 旧 pipeline 不能重建或覆盖当前 DB；
- 历史能力有明确 forensics namespace。

### PK-SSOT-006 — Layered fallback 需要版本一致性，而不仅是优先级

**状态：DESIGN DEBT**

当前优先级是 KU→dialogue→Google，但不同层可能来自不同 source checksum。返回结果需要包含每层 source/version，并禁止在同一答案中无提示混合不一致时间快照。

---

## 3.4 生命周期与成长线

### PK-LIFE-001 — 生命周期机制已实现，但真实治理采用率极低

**状态：OBSERVED ADOPTION GAP**

当前 Canonical KU：

| lifecycle | 数量 |
|---|---:|
| current | 32,182 |
| deprecated | 2 |
| `supersedes_id` 非空 | 0 |

32,184 个 KU 中几乎没有发生 supersede/conflict/history 关系。说明结构和 CLI 已存在，但真实知识仍接近“全量当前事实集合”。

### PK-LIFE-002 — `current`、`status`、`active` 三种状态语义容易混淆

**状态：DESIGN DEBT**

- `lifecycle=current/deprecated/...`：知识内容是否当前；
- `status=staging/current/review/rejected`：KU 发布/审核状态；
- index `status=active/candidate/rolled_back`：向量版本状态。

同一个单词 `current` 同时出现在不同状态机中。需要 schema glossary 和类型化枚举，API 不应只返回无命名空间的 `status`。

### PK-LIFE-003 — Supersede 方向需要统一

**状态：DESIGN DEBT**

当前 reconcile action 中需要明确：`new.supersedes_id=old` 还是 `old.supersedes_id=new`。历史代码/字段命名容易产生方向误读。建议使用明确关系表：

```text
knowledge_lifecycle_edges(
  predecessor_id,
  successor_id,
  edge_type,
  evidence,
  decided_at
)
```

而不是单个自引用字段承担 supersede、conflict、correction 多种语义。

### PK-LIFE-004 — 不物理删除是正确原则，但需要 Tombstone 与源删除分类

**状态：DESIGN DEBT**

“删除证据”可能代表：

- 用户真的删除原始内容；
- canonical rebuild 改 ID；
- source 暂时不可用；
- privacy policy 重新分类；
- 导入失败。

不能统一标为 deprecated。需要 reason code、source tombstone 和可逆/不可逆分类。

### PK-LIFE-005 — Growth History 与默认 Retrieval 分离正确，但需要泄漏测试

**状态：MONITOR**

`pk-ku history` 可读取所有 lifecycle，默认检索应 current-only。应持续验证 superseded/conflicting/deprecated 不会经：

- Chroma metadata 缺失；
- conversation fallback；
- legacy pad；
- cache；
- MCP fetch

重新泄漏为当前事实。

---

## 3.5 评测与 Promotion 治理

### PK-EVAL-001 — 综合评测结论为 FAIL

**状态：OPEN / QUALITY EVIDENCE**

现有完整评测 `82221b6a8c91ed51` 的关键结果：

| 指标 | Hybrid |
|---|---:|
| Recall@5 | 0.1164 |
| MRR@5 | 0.0765 |
| NDCG@5 | 0.0866 |
| No-answer FP rate | 0.90625 |
| Privacy hit | 1 |
| Gate | FAIL |

主要主张“KU 相比 Raw 至少提升 10pp”未通过；实测约 8.22pp，且隐私/secret gate 存在命中。

### PK-EVAL-002 — 新 Canary PASS 不能替代完整评测

**状态：OPEN / EVIDENCE SCOPE**

新 Active Candidate 的 Canary：

- 30 queries；
- helpful_rate=96.67%；
- p95=152ms；
- strict PASS。

但它不是 Raw/L1/L2/Hybrid 同源对照，也不覆盖 178 条综合集。只能证明“小范围上线检查通过”，不能证明整体召回、abstain、隐私和相对提升已经达标。

### PK-EVAL-003 — Canary 标签包含模型与人工 Triage，证据类型需分开

**状态：DESIGN DEBT**

报告中 28 条已有标签，2 条由 LLM 补标，并记录一次 `wrong -> helpful` 人工修正。审计轨迹存在，这是优点；但 promotion gate 应分别报告：

- human gold；
- calibrated judge；
- uncalibrated LLM label；
- operator override。

不能将四种证据合并成单一 helpful_rate 而不显示置信等级。

### PK-EVAL-004 — `latest.txt` 不是有效评测指针

**状态：OPEN / ARTIFACT GOVERNANCE**

`var/reports/analysis/evaluations/latest.txt` 当前内容只有 `run`，没有指向真实目录 `82221b6a8c91ed51`。工具或人按 latest 查找会失败或读取错误报告。

### PK-EVAL-005 — 评测 Active Collection 已过期

**状态：DOC/ARTIFACT DRIFT**

完整评测绑定旧 collection `knowledge_units_205bff...`，当前 Active 是 `knowledge_units_ir_4cd8af...`。新 Active 尚缺同等范围的完整评测证据。

### PK-EVAL-006 — Phase 17 人工 Gold/Judge/UAT 仍开放

**状态：OPEN**

自动化代码完成，但仍缺：

- operator-labeled cross-turn gold；
- judge calibration artifact；
- signed UAT promote/rollback。

---

## 3.6 软件架构与依赖方向

### PK-ARCH-001 — Repository Zones 文档的依赖方向自相矛盾

**状态：OPEN / DOC DRIFT**

文档前部仍描述：

```text
delivery → application → domain → foundation
```

后部又声明 `domains/` 只是 compatibility re-export，且 application→domains imports 已为 0。当前真实方向应是：

```text
Delivery
→ Application use cases
→ Core/domain contracts
→ Adapters/retrieval/infrastructure through contracts
```

不能继续把兼容 facade 写成应用必经层。

### PK-ARCH-002 — `domains/` facade 与 `tools/compat` 双重跳转仍存在

**状态：OPEN / TECHNICAL DEBT**

当前应用代码已不再导入 domains，这是进展；但约 85 个 compatibility shims 仍存在，部分路径形成：

```text
tools/compat → domains facade → application/evaluation
```

这增加模块身份、import cache、文档和退役复杂度。

### PK-ARCH-003 — Retired `rag-pipeline` 底层模块仍可通过 Forensics 开关执行

**状态：INTENTIONAL BOUNDARY / RISK**

保留 forensics 能力合理，但必须继续隔离生产 DB，避免旧 pipeline 重建 personal events/memory 并影响当前 KU 数据。

### PK-ARCH-004 — Turn、KU、Profile 的消费契约未完全统一

**状态：DESIGN DEBT**

Turn 是行为叙述，KU 是长期知识，Profile/Growth 是解释。REST/MCP 的 `memory`、`knowledge`、`event`、`relation` 命名仍容易让消费者绕过 KU，直接把实验记忆或 profile 当事实。

### PK-ARCH-005 — Google Light 是独立侧面，尚未进入统一个人语义模型

**状态：DESIGN DEBT**

Google assertions 是 privacy-filtered light signals，不是 KU。当前边界正确，但跨源个人画像若直接合并 KU 与 Google signals，需要清楚区分：事实、自述、行为观察、模型推断。

---

## 3.7 CLI、运行环境与可操作性

### PK-OPS-001 — Python 3.12 与 3.14 环境分裂

**状态：OPEN**

- 全量 pytest 使用 Python 3.14：通过；
- 系统 console scripts 使用 Python 3.12；
- `rag-search stats --json` 在 3.12 环境因缺少 `requests` 失败。

测试环境绿不代表已安装产品环境可用。需要统一 lock/install/smoke matrix。

### PK-OPS-002 — `rag-search` 产品命令当前不可运行

**状态：OPEN / PRODUCT PATH**

实际错误：

```text
ModuleNotFoundError: No module named 'requests'
```

说明依赖未安装到 console script 所绑定的解释器，或 packaging 未声明 runtime dependency。

### PK-OPS-003 — Windows 控制台中文输出乱码

**状态：OPEN / OPERABILITY**

`pk-sync`、`pk-ku inspect/doctor` 输出路径、中文标题和状态时出现 mojibake。虽然数据未必损坏，但会破坏人工审计、日志搜索和故障处理。

### PK-OPS-004 — REST 与 MCP 服务未运行

**状态：WARNING / OPERABILITY**

`pk-ku doctor` 报告 8000 和 8789 未监听。不是数据错误，但当前服务消费层依赖手工启动，没有常驻、健康恢复或统一生命周期管理。

### PK-OPS-005 — `pk-ku doctor` 健康定义过窄

**状态：OPEN**

当前 doctor exit 0，即使：

- 外键违规 18,859；
- `rag-search` 缺依赖；
- latest eval pointer 无效；
- lifecycle adoption 几乎为零；
- preflight CI 失败。

Doctor 应区分 storage integrity、product CLI、serving、governance 四种健康，而不是单一 OK。

### PK-OPS-006 — Full Inventory 仍可通过模块路径误执行

**状态：OPEN / SAFETY RISK**

`pk-ku` 不暴露 full inventory 是正确设计，但底层 modules 仍可直接执行。历史上已发生误跑。应在生产 DB 路径增加双确认或 explicit forensics/backfill token，而不只依赖文档。

---

## 3.8 Repository Governance 与文档一致性

### PK-DOC-001 — ROADMAP 与 STATE/PRODUCT-READINESS 的 Phase 22 状态冲突

**状态：OPEN / DOC DRIFT**

- `STATE.md`：Phase 22 01-04 code/ops complete；
- `PRODUCT-READINESS.md`：已完成并提升至约 89；
- `ROADMAP.md`：仍为 Planned，0/4 unchecked。

`.planning` 自称权威，但内部没有单一事实。

### PK-DOC-002 — PRODUCT-READINESS 分数内部不一致

**状态：OPEN / DOC DRIFT**

同一文档出现：

- Overall simple average ~88；
- weighted ~89；
- estimated “Now” ~87；
- STATE 又写 ~87；
- score delta 中 Eval/Canary 先 70→72，后又 72→88；
- Facade/debt delta 写 60→68，但当前表写 88。

分数缺少可重算公式和版本化输入，不应作为权威门禁。

### PK-DOC-003 — Cleanup 中保存的旧全量失败报告会误导当前状态

**状态：OPEN / ARTIFACT LIFECYCLE**

`_test4_full_pytest.txt` 记录 16 项失败，但本次用 Python 3.14 重跑全量 pytest 已通过（1 skipped）。旧报告没有 `superseded` 标记或最新指针。

### PK-DOC-004 — Governance Preflight 当前失败

**状态：OPEN**

本次执行 `personal_knowledge.governance.preflight --ci`：

| Gate | 结果 |
|---|---|
| inventory/privacy/path/architecture/planning | PASS |
| shim-budget | FAIL：85 shims / 16 tools |
| docs-coverage | FAIL：`src/personal_knowledge/governance` 缺 README |
| secret-scan | FAIL |

功能测试全绿不代表仓库治理门禁全绿。

### PK-DOC-005 — Secret Scan 失败信息不够可操作

**状态：OPEN / GOVERNANCE UX**

Preflight 只输出 `FAIL secret-scan: safe source roots only; private zones excluded`，没有在汇总中显示文件、规则、是否真实 secret 或扫描工具不可用。Fail-closed 是正确的，但必须产生可审计、脱敏的 reason artifact。

### PK-DOC-006 — Shim manifest 与真实目录/数量漂移

**状态：OPEN**

已有 Concerns 记录 `entrypoints.yaml` 仍使用旧 root/count；当前 preflight 继续 shim-budget FAIL。兼容窗口不是允许 manifest 长期错误的理由。

### PK-DOC-007 — `docs/architecture/repository-zones.md` 与 Phase 21 后事实未完全同步

**状态：OPEN / DOC DRIFT**

架构依赖图仍保留旧 domain 中心叙述，应更新为 canonical application/evaluation/core 结构，并明确 domains 仅 shim。

### PK-DOC-008 — 评测、规划、运行报告缺少统一 supersedes/latest 规则

**状态：DESIGN DEBT**

当前存在：

- 多份 canary/eval；
- latest.txt 无效；
- cleanup 旧测试报告；
- STATE/ROADMAP/READINESS 冲突；
- 历史报告仍可被 Agent 搜到。

建议所有运行文档包含：`artifact_id`、`generated_at`、`source_snapshot`、`supersedes`、`status=current|historical|invalidated`。

---

# 4. 不应误判为问题的正确设计

以下边界应保留：

1. AgentsView live 数据只读，不迁移、不写入。
2. Raw/Canonical/KU/Active Index 分离。
3. Knowledge Unit 不替代原始消息；证据必须可追溯。
4. `memory_items` 不等于 KU SSOT。
5. Candidate index 在 Eval/Canary 前不得成为 Active。
6. 生命周期更新不物理删除知识行。
7. Growth History 与 current-only retrieval 分离。
8. Google Light Assertion 不是用户自述知识。
9. Chroma 是可重建索引，不是唯一事实库。
10. Full inventory 不进入日常产品 CLI。

---

# 5. 建议收口顺序

## P0 — 数据完整性与增量安全

1. 设计并迁移统一 Inventory 父模型，修复 Delta Inventory 外键错误。
2. 建立统一 SQLite connection factory，启用 FK，并先处理 18,859 条历史违规。
3. 将 `foreign_key_check` 加入 doctor、CI 和 publish/promote gate。
4. 修复 `pk-ku inspect` 默认读取 Watermark。
5. 分离完整执行集合与前 100 条 preview，禁止截断执行输入。
6. 定义 SQLite publish 与 Chroma active 的一致性/快照协议。

## P1 — 层级和生命周期

1. 建立 `D/S/R/A` Layer Registry。
2. 为 Turn 层增加 version/watermark 并纳入产品同步。
3. 执行高价值 Subject 的 lifecycle reconcile，建立真实 supersede/conflict 样本。
4. 统一 `lifecycle/status/index_status` 命名和 API schema。
5. 提供 Canonical KU→Evidence 权威 view/repository。
6. 为跨 DB evidence 增加完整性审计和 tombstone reason。

## P2 — 质量证据与交付

1. 使用当前 Active collection 重跑 Phase 17 全量评测。
2. 重点降低 no-answer FP 和 privacy hit。
3. 将 Human Gold、Judge、LLM Label、Operator Override 分开计量。
4. 修复 eval latest pointer 和 artifact supersedes。
5. 统一 Python 运行环境和 console script smoke。
6. 修复 UTF-8 输出、rag-search 依赖和服务启动治理。

## P3 — Repository Governance

1. 同步 ROADMAP/STATE/PRODUCT-READINESS。
2. 修复 preflight 的 shim/docs/secret gate。
3. 更新 repository-zones dependency graph。
4. 为旧测试/报告添加 historical/superseded 状态。
5. 收口 compat→domains→application 双跳，但不绕过兼容窗口和删除审批。

---

# 6. 验证证据摘要

本次实际读取或执行：

- `AGENTS.md`、README、repository-zones、retrieval-ssot、data tree；
- `.planning/ROADMAP.md`、`STATE.md`、`PRODUCT-READINESS.md`、Phase 17 Verification；
- 全量 Python 3.14 pytest：通过，1 skipped；
- MCP App tests：11/11 passed；
- `pk-ku doctor/workflow/inspect`、`pk-sync conversations`；
- `rag-search stats --json`：失败，缺 `requests`；
- Governance preflight：部分失败；
- SQLite schema、KU/evidence/member/lifecycle 实查；
- `PRAGMA foreign_keys` 与 `foreign_key_check`；
- Chroma collection inventory；
- 完整评测和 Canary 报告。

本报告只记录问题，不授权：

- `reconcile --write`；
- 修改 lifecycle；
- Promotion/rollback；
- Watermark advance；
- 删除 orphan rows、历史 collection 或私有数据；
- 清理 compat/archive。
