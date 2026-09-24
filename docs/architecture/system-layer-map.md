<!-- generated-by: gsd-doc-writer -->
# 系统分层地图（System Layer Map）

一页读懂整个系统：**四层数据 × 五级知识 × 两类组件**。"层"字在本系统里被三套东西各用一遍，容易迷糊——本地图把三条轴分开讲清，最后给一张名词防混淆表。

- 生成日期：2026-08-29。除标注 `VERIFY` 外，全部路径与数字均为当日只读实测（sqlite `mode=ro` / 文件系统计数 / 源码定位），非照抄底账。
- 配套底账：`.planning/codebase/RUNTIME_MAP.md`（在线运行时）、`.planning/codebase/PIPELINE_MAP.md`（离线流水线）、`docs/architecture/overview.md`（架构总览）。

## 0. 一句话总览

**组件是车间，数据四层是仓库，知识五级是成品等级。**

原始层只进不改；canonical 库是唯一事件权威，"谁能看见"由代码谓词决定而非数据库视图；知识层由离线管线生产（MVP 语义管线在产，正式轨道被成本门封存）；在线组件经门面与域网关访问数据层，不绕过契约直接摸库。

---

## 1. 轴一：数据四层（物理位置，自下而上）

```text
④ 应用层   REST :8000 / MCP :8789 / 检索 / wiki 读取        ← 人与客户端从这里进来
              ↑ 经域网关五道闸 / 代码谓词，只读为主
③ 知识层   var/db/personal_system.sqlite（正式 KU current 7,402）
           var/db/semantic_mvp_v3.sqlite（1,108 卡）+ Chroma 8,510 文档 + wiki 1,595 页
              ↑ 离线管线写入（tools/semantic 七脚本在产；pk-ku 正式轨道封存）
② 中间层   data/canonical/agent/structured/db/agent_conversations.sqlite
           可见投影 = 代码谓词 → 1,267 会话 / 78,841 消息
              ↑ shadow → activation（事件权威，多代累积）
① 原始层   data/staging/v2/native/*.jsonl（16 个 family，715 个文件）只进不改
```

### ① 底层原始层 — `data/staging/v2/native/`

- 位置：`data/staging/v2/native/`，16 个 family 目录（antigravity / chatgpt / claude / codex / copilot / cursor / gemini / grok / kimi / kimi-work / mimo / opencode / pi / qoder / workbuddy / zcode），family 下按项目目录镜像源文件，并配 `.hashes.json` 哈希清单。
- 规模（2026-08-29 实测）：**715 个 `.jsonl` 文件、共 381,659 行**（其中 codex 356 文件 / 209,510 行，claude 62 文件 / 20,881 行）。
  <!-- VERIFY: tmp/mvp_compression_report_v3.md:124 与历史口径称原始层"174,280 条"（约 17.4 万）；当日全量实测为 381,659 行，"174,280"的统计口径无法复现，引用时注意差异 -->
- 只进不改：内容寻址暂存 + 哈希清单；同步默认 metadata-only shadow（不写 canonical），门禁全部通过后自动合入（人工 `--v2-activate` + 批准语入口已退役）。
- 上游：各 family 工具的本地会话存储；其中 AgentView daemon 库 `%USERPROFILE%/.agentsview/sessions.db` 是 protected-external 只读源（`core/project_paths.py:98-101`）。

### ② 中间层结构化 canonical — `data/canonical/agent/structured/db/agent_conversations.sqlite`

- 全量表（多代事件累积）：`canonical_sessions` **2,426** / `canonical_messages` **174,269**；另有 ce_* 事件权威（`ce_events` 300 万级，见 PIPELINE_MAP §1.1）。
- **可见投影不是表/视图**，而是代码谓词 `canonical_projection_predicate`（`core/canonical_visibility.py`）：当 `ce_generation_authority` 存在 `active=1` 代际时，只放行 `v2|%` 前缀 ID，否则放行全量。实测（谓词生效）：**可见会话 1,267 / 可见消息 78,841**；非空正文消息 73,456；MVP 压缩报告的"内容消息 73,939"是剥离 system-reminder 注入后的自有统计口径（`tmp/mvp_compression_report_v3.md:15`）。
- 谁写/谁读：写入方 `pk-sync conversations` → `application/conversation/v2_sync.py`（shadow → activation，经 `event_generations.py`/`event_repository.py` 写 canonical，D-15 零付费）；读取方为谓词消费者（`application/knowledge/eligibility.py`、`delta_build.py`、`tools/semantic/mvp_semantic_compress.py`）、内核会话历史 provider（`services/harness_conversation_service.py`）、检索层（`retrieval/unified_search.py`）与域网关 `conversation.thread.*` 操作。

### ③ 知识层压缩产物 — 两处落点 + 两类派生物

| 落点 | 内容 | 实测（2026-08-29） |
|---|---|---|
| `var/db/personal_system.sqlite`（正式层） | `knowledge_units` 7,685 行（**lifecycle='current' 7,402** / superseded 283）、`canonical_knowledge_units` 7,059、`knowledge_unit_evidence` 14,031 | 本次实测 |
| `var/db/semantic_mvp_v3.sqlite`（语义管线） | `session_cards` **1,108**、`ku_facts` 7,687（active 7,403 / superseded 284）、`chunk_summaries` 1,710 | 本次实测 |
| Chroma 语义索引（localhost:8001） | active build `sem_20260829123152`：8,510 文档（bge-small-zh-v1.5，512 维），登记于 `var/db/semantic_index_registry.json` | 本次实测 |
| `var/db/personal_wiki_projection.sqlite` | 主题页 1,595（全部 `topic_type='subject'`） | 本次实测 |

- 关键现状：在役检索索引指针 `var/db/knowledge_index_active.txt` 仍指向 `knowledge_units_empty_kg_20260812T025401Z_live`（空索引）。正式 KU 由升格脚本写入并登记 `status='candidate'` 的索引版本，**不切换在役索引**——serving 切换是独立决策。
- 另在库：D-30 封存的消息级提取队列 `knowledge_run_items` 24,487 条（全部 pending，不许提取）。

### ④ 应用层 — 在线服务与读取面

REST :8000（门面 + 域网关）、MCP :8789、检索 / wiki 读取、内核 :8790。结构与职责见轴三。

---

## 2. 轴二：知识五级（压缩金字塔，每级带证据指针可下钻）

```text
L4 跨主题时间线                     （未建）
L3 主题页      wiki 1,595 页        （subject 主题页，可读）
L2 会话卡+KU   1,108 卡 / 7,687 事实 → 正式 current KU 7,402   ★ 当前主战场
L1 块摘要      chunk_summaries 1,710（大会话 map-reduce，约 12k 字符/块）
L0 消息        78,841 条可见消息（canonical 投影）
```

| 级 | 名称 | 物理载体 | 当前进度（2026-08-29） | 状态 |
|---|---|---|---|---|
| L0 | 消息原文 | ② canonical 可见投影 | 1,267 会话 / 78,841 消息 | 完成（只读基底） |
| L1 | 块摘要 | `semantic_mvp_v3.sqlite` `chunk_summaries` | 1,710 条（仅大会话产生） | 在产 |
| L2 | 会话卡 + KU | `session_cards` / `ku_facts` → 正式 `knowledge_units` | 1,108 卡、7,687 事实 → current KU 7,402 | 在产 |
| L3 | 主题页 | `var/db/personal_wiki_projection.sqlite`（`subject:` 主题页） | 1,595 页（`tools/semantic/materialize_wiki.py` 物化） | 在产 |
| L4 | 跨主题时间线 | 未建 | — | 未建（`tools/semantic/` 与 `application/wiki/` 无对应代码） |

证据指针下钻链：KU → `knowledge_unit_evidence`（14,031 行 / 唯一引用 10,432 / **悬空 0**，本次实测全部解析到 canonical 消息 `v2|cm|…` ID）→ ① 原始 JSONL。每条事实可回溯到真实消息，不允许编造出处。

---

## 3. 轴三：两类组件（车间）

### 在线（`ops/runtime/start-agent-stack.ps1` 一键拉起 rest / pi-kernel / mcp[+tunnel]）

```text
客户端（Electron desktop / MCP 客户端 / 本地 CLI rag-search·rag-mcp·rag-dashboard）
   │
   ├─▶ MCP :8789 ──▶ REST :8000 门面 ──▶ 域网关五道闸 ──▶ ②③ 数据层
   │
   └─▶ pi 内核 :8790 ──▶ 模型通道（hy3，var/config/pi-provider.json）
            │  模型工具调用经 domain-bridge
            └────▶ POST /internal/pi-domain/dispatch（落回 REST，同一网关）
```

- **REST :8000**（`services/api_server.py`，纯标准库 ThreadingHTTPServer）：`/search/*`、`/ui/topic*`（wiki 统合层，当前产品方向）、`/intelligence/*`、`/decision/*`、`/proactive/*`、`/agent/*`、`/search/cards` 等；`/internal/pi-domain/dispatch` 是域网关唯一入口。**五道闸**（`services/pi_domain_gateway.py`）：能力头 → 操作注册表 → 参数白名单 → 绑定三元组（task_id + idempotency_key + binding，`domain.*` 免 task_id 例外）→ 隐私分级 R0/R1/R2。注册表规模：61 个操作名 = 45 capability + 16 额外，registry↔gateway 零缺口（RUNTIME_MAP §4.2 实测核对，本次未重数）。
- **pi 内核 :8790**（`apps/personal_intelligence_kernel/src/server.mjs`，Node）：任务账本（`tasks/ledger.mjs` → `var/db/pi_kernel_tasks.sqlite`，7 状态机 + lease + outbox + 任务响应持久化）、SkillEngine（`skills/engine.mjs`，清单 11 技能全 active——本次实测；覆盖 45 个 capability 操作，见 RUNTIME_MAP §3.2）、模型路由（`models/routes.mjs`，budget 优先级 env > pi-provider.json > 全局键 > 清单 > 内嵌常量）。运行时模式 **primary**（`var/db/pi_runtime_activation.pointer.json`，2026-08-12 起）。
- **MCP :8789**（`apps/personal_data_chatgpt/server.mjs`，ChatGPT Apps 面，`/mcp`）：capability descriptors + checksum 校验，后端固定指向 REST :8000。另有 stdio `rag-mcp`（`services/mcp_server.py`，56 工具）并存——同一能力 bundle 的两条暴露通道。

### 离线（知识生产流水线）

- **正式管线（封存中）**：`application/run_pipeline.py`（v1 统合编排，deprecated，需 `PK_ALLOW_LEGACY_PIPELINE=1` 门禁，仅取证）；`application/ku.py`（pk-ku 权威 CLI）——**D-30** 封存 24,487 条消息级提取队列（估值 USD 24.487，不许提取）；**D-31** 零付费门（`ku.py:613-637`：view-policy run 一律拒绝 extract；任何 LLM 试点需单独成本审批）。
- **MVP 语义管线（在产）**：`tools/semantic/` 七脚本，除 LLM 调用外全部本地确定性执行，幂等可重跑：

```text
mvp_semantic_compress ──▶ export_ku_staging ──▶ classify_ku_staging ──▶ promote_ku_formal
  读② canonical(只读)        ku_facts → staging 库      九类枚举分类        写③ personal_system.sqlite
  写 semantic_mvp_v3.sqlite                                                （KU + evidence，索引登记 candidate）
      ├─▶ dedup_canonical_ku（canonical 层语义收敛，只重写 promote 产出行）
      ├─▶ build_semantic_vector_store（→ Chroma :8001 + semantic_index_registry.json）
      └─▶ materialize_wiki（→ personal_wiki_projection.sqlite，subject 主题页）
```

三轴关系举例：MVP 压缩一个会话 = 离线管线（车间）借 pi 内核的模型通道，把 ② 中间层的可见消息加工成 ③ 知识层的 L2 卡，再物化为 L3 主题页。

---

## 4. 名词防混淆表

| 名词 | 出处 | 实际含义 | 状态 |
|---|---|---|---|
| **L1/L2/L3（merge layer）** | `application/graph/build_merge_layer.py` | 遗留统合管线的**事件去重分级**：L1 真重复 / L2 同主题聚类 / **L3 = 保留不动**；合并 = 折叠不删除，原始统合表一字不改。与知识五级 L1–L4 **无关** | 已运行过：`merge_clusters` 470 / `merge_members` 1,628 / `merge_build_meta` 21（本次实测在库） |
| **知识五级 L0–L4** | 本地图 §2 / 语义管线设计 | 压缩金字塔（消息 → 块摘要 → 卡+KU → 主题页 → 时间线） | L0–L3 在产，L4 未建 |
| **view-policy 七类视图** | `application/conversation/extraction_views.py:81` `ViewType`（Phase 62 D-21） | `turn / native_trace / episode / compaction_window / session / topic / cross_session` 七类派生视图 ≈ 五级金字塔的**正式可审计版** | 代码完成未跑（`ce_candidate_*` 台账表从未建；D-31 成本门锁定） |
| **MVP v1/v2/v3** | `tmp/mvp_semantic_compress.py` → `tools/semantic/`；`var/db/semantic_mvp{,_v2,_v3}.sqlite` | **同一个压缩器的三代代码版本**，不是系统层级；v1/v2 库保留为对照证据，永不重写 | v3 现行 |

---

## 5. 谁在读 / 谁在写（按层对照）

| 层 | 写入者 | 读取者 |
|---|---|---|
| ① 原始 `data/staging/v2/native/` | `pk-sync conversations --v2-native`（`v2_sync.py` 原生暂存；`tools/register-native-sync.ps1` 每日 23:00 计划任务，metadata-only shadow） | v2 同步 adapter（family 解析入②）、`tools/forensics/` 取证脚本 |
| ② canonical `agent_conversations.sqlite` | `pk-sync conversations [--write]` → `v2_sync.py` shadow→activation（`event_generations.py` 写入，显式激活） | 谓词消费者（`eligibility.py` / `delta_build.py` / 语义管线压缩器）、内核会话历史（`harness_conversation_service.py`）、检索 facade（`unified_search.py`）、域网关 `conversation.thread.*` |
| ③ 知识（personal_system / semantic_mvp_v3 / Chroma / wiki 投影） | `tools/semantic/` 七脚本（在产）；pk-ku 正式轨道（D-31 封存，仅 freeze/audit 台账） | `retrieval/semantic_cards.py`（向量优先、关键词回退）→ MCP `search_semantic_cards` + REST `POST /search/cards`；`unified_search` 的 KU 槽（当前落在空索引 active 上）；wiki 物化与 `/ui/topic*` 统合层；网关 `wiki.page/directory`（读 `WIKI_PROJECTION_DB`） |
| ④ 应用（REST / MCP / 内核） | 服务进程自身不产知识数据；内核运行时台账写 `var/db/pi_kernel_{tasks,events,sessions}.sqlite`（metadata-only） | 人与客户端：Electron desktop（固定 loopback 路由）、MCP 客户端、本地 CLI；模型（hy3）只经内核通道被调用 |

---

## 6. 数字抽验记录（2026-08-29）

- 方法：全部 sqlite 以 `file:...?mode=ro` 只读打开计数；JSONL 用文件系统统计；代码论断直接定位到源文件行。
- 实测项：① 715 文件 / 381,659 行；② 全量 2,426 / 174,269，可见 1,267 / 78,841（非空 73,456），`ce_generation_authority` active=1；③ KU 7,685（current 7,402）、canonical KU 7,059、evidence 14,031 / 唯一 10,432 / 悬空 0、cards 1,108、facts 7,687、chunks 1,710、wiki 1,595、active 向量 build `sem_20260829123152` 8,510 文档、D-30 队列 24,487 pending、merge 层 470/1,628/21；④ 端口 8000/8789/8790/8001、11 技能、指针 mode=primary、`tools/semantic/` 7 脚本、`ViewType` 7 类。
- 底账差异修正：`tmp/mvp_compression_report_v3.md` 附录称 build_merge_layer "从未运行"，与库中 merge 表 470 簇矛盾（PIPELINE_MAP §0 已修正）；原始层"174,280 条"口径无法复现（见 §1 VERIFY）。
