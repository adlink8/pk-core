# 检索三层 SSOT 与 hybrid 路由

> Phase 15 治理文档。与 `get_knowledge_status()` 返回的 `ssot` / `fallback_policy` 字段对齐。
> 实现主路径：`src/personal_knowledge/retrieval/unified_search.py`（CLI / REST / MCP 共用）。

## 1. 三层 SSOT

| 层 | SSOT | 用途 |
|---|---|---|
| **对话采集** | AgentsView `sessions.db`（**只读**）→ canonical `data/canonical/agent/structured/db/agent_conversations.sqlite` | 允许保留的原文、工具元数据、provenance 与 message 级证据；secret、排除或删除会话不进入 canonical messages |
| **个人知识** | `var/db/personal_system.sqlite` 中 `canonical_knowledge_units` + active KU Chroma collection | 偏好 / 决策 / 可答断言；供 career-os 取证 |
| **跨源非对话** | `unified_events` / `personal_events`（过渡；库在 `var/db/`） | Google 等非对话；遗留语义兜底 |

补充约束：

- `memory_items` 是实验层，**不得**与 KU 并列作为消费 SSOT。
- AgentsView `insights` 为可选旁路，**不得**覆盖 KU。
- **禁止**写入 AgentsView live DB（`$HOME\.agentsview\sessions.db`）。

### 1.1 状态 API 字段

`get_knowledge_status()` 固定暴露：

```json
{
  "ssot": {
    "dialogue": "agentsview_canonical",
    "knowledge": "canonical_knowledge_units",
    "non_dialogue_raw": "personal_events"
  },
  "fallback_policy": "layered",
  "allow_legacy_pad": true
}
```

| 键 | 含义 |
|---|---|
| `ssot.dialogue` | 对话全文 / message 证据真相源 = AgentsView → canonical dialogue |
| `ssot.knowledge` | 结构化知识真相源 = `canonical_knowledge_units` + active index |
| `ssot.non_dialogue_raw` | 非对话 raw 过渡层 = `personal_events`（及 unified 投影） |
| `fallback_policy` | 当前 hybrid 补洞策略（默认 **layered**；可用 env/CLI 改 `legacy`） |
| `allow_legacy_pad` | layered 仍不足时是否用非 Google `personal_events` 填充（默认 true；见 §2.2 滚动决策） |

## 2. 目标 hybrid 路由

`search_knowledge_units` 的**目标**分层（Wave 2 起落地；在 dialogue_fallback 达标前允许过渡）：

```text
search_knowledge_units (layered 默认):
  0a) wiki_page                         # subject 主题投影（可再生，非事实 SSOT）
  0b) semantic_card                     # 会话卡 + active ku_facts
  1)  active KU collection              # 正式 KU 向量（current-only）
  2a) canonical_messages 片段/词检索    # message 级 dialogue（W4；code-literal 主力）
  2b) conversation_turns 向量           # 叙述级 dialogue（辅）
  3)  personal_events source=Google     # 非对话 raw
  4)  optional legacy_pad               # 非 Google PE 填充（可关）
```

Wiki 与会话卡只读；命中不写回 KU / Chroma。`legacy` 政策不含 0a/0b，保持 KU 后全量 raw 补洞。

Wave 4 frozen 评测（gold evidence）：**layered R@5 = 1.00**（legacy 约 0.65；dialogue_only 1.00）。

### 2.1 `fallback_policy` 取值

| 值 | 行为 | 何时 |
|---|---|---|
| **`layered`**（**当前默认**） | wiki → cards → KU → `canonical_messages` → `conversation_turns` dialogue → `non_dialogue_raw`（含 Google）→ 可选 legacy_pad | 当前分层检索链路 |
| **`legacy`** | KU 后全量 `personal_events` 补洞 | 回滚 / 对比评测：`--fallback-policy legacy` 或 env `PERSONAL_DATA_FALLBACK_POLICY=legacy` |

`route_policy` 字符串是人类可读摘要：默认分层策略返回 `wiki-first + layered fallback (cards→KU→dialogue→non_dialogue_raw)`；仅 legacy 策略返回 `knowledge-first + raw fallback`。它与 `fallback_policy` 机器枚举并存。

### 2.2 `allow_legacy_pad` 默认与滚动（Phase 15-02）

| 阶段 | 策略 | 门禁 |
|---|---|---|
| **当前（transition_observable）** | 默认 **`true`** | 每次 `search_knowledge_units` 返回 `telemetry.pad_used` / per-layer latency |
| **Canary** | holdout 对比 pad on vs off | pad-off 在 google+paraphrase 上 R@5 跌幅 ≤5pp，且 pad_used_rate 足够低 |
| **Default flip** | 默认改为 **`false`** | 文档记日；紧急回滚 `PERSONAL_DATA_ALLOW_LEGACY_PAD=1` |
| **Retire** | pad 仅保留在 `fallback_policy=legacy` | 观测期无回归后 |

原则：

1. Pad 不是对话 gold 的主救援路径（主路径是 KU / canonical_messages / turns）。
2. Pad 用于 KU+dialogue+Google 仍不足时的过渡填充；必须可观测，不可静默。
3. 全量 PE 回滚用 `fallback_policy=legacy`，不要依赖默认 pad。

`search_knowledge_units` 响应（15-02 起）固定含：

```json
{
  "fallback_policy": "layered",
  "allow_legacy_pad": true,
  "telemetry": {
    "layers": [
      {"name": "knowledge_unit", "attempted": true, "hits": 1, "latency_ms": 12.3}
    ],
    "first_contributing_layer": "knowledge_unit",
    "pad_allowed": true,
    "pad_used": false,
    "total_latency_ms": 40.0
  }
}
```

Holdout 套件（独立于 frozen 20）：`assets/evals/knowledge_units/holdout_15_02.synthetic.jsonl`
评测：`python tools/forensics/phase15_02_holdout_eval.py`

## 2.3 Dual-view: current vs growth line (Phase 22)

| View | Surface | Lifecycle | Mutates data? |
|------|---------|-----------|---------------|
| **Current (retrieval)** | `search_knowledge_units` / MCP semantic / default canary | **`lifecycle=current` only** | No |
| **Growth line (history)** | `pk-ku history --subject S` | current + superseded + deprecated + conflict | No (read-only) |

Principles (D-22-01…03):

1. **Never physical DELETE** of KU/canonical rows for “cleanup”.
2. Growth line keeps multi-version units by subject + time; `lifecycle` / `supersedes_id` mark succession.
3. Default retrieval and MCP stay **current-only**; archive/growth is an **explicit** CLI/API (`pk-ku history`).
4. Layered fallback order remains **wiki → cards → KU → dialogue → Google PE → optional legacy pad** (`LAYERED_FALLBACK_ORDER` in `retrieval/_constants.py`); scores sort hits, they are not truth. Wiki/cards are projections and never become fact SSOT.

```powershell
pk-ku history --subject "Shell" --limit 20
pk-ku history --subject "Shell" --include-all-lifecycle --json
```

## 3. 明确边界

### 3.1 `personal_events` ≠ 全量 View 消息流

- `personal_events` / `unified_events` 是**跨源事件级**汇总，粒度与 AgentsView 的 **message 级**对话流不同。
- 实测量级差约一个数量级（Agent 事件级数千 vs View 消息级数万）。
- **不得**把 `personal_events` 当作对话全文库或 AgentsView 等价物。
- 对话补洞应优先 canonical / View 只读路径（`agent_conversations`、已发布 snapshot、可选 FTS 探针）。

### 3.2 Google 轻量结构化（Phase 16）

- 明细：`activities` + FTS；跨源 raw：`unified_events` / `personal_events`。
- **规范化事件**：`normalized_events`（`event_id` 前缀 **`g|`**，与对话 `cm|` 分离）。
- **轻断言**：`google_light_assertions`（主题/服务/频道/域名聚合信号，**不是**对话 knowledge unit）。
- **禁止**对 Google 跑对话 `knowledge_unit_extractor`。
- 隐私策略（**service + category/content**，2026-07-12 审计锁定）：
  - **可进** `normalized_events`：全部 activities（含 Maps / 支付 / 地点主题）。
  - **不进** light assertions：`Maps` 服务；类别/文本含 `支付|金融|卡`；类别/文本含 `地图|地点|位置|导航`（含 Search/Gemini 上的「地图 / 地点 / 本地生活」聚合）。
  - 仅 service 过滤不够：Search/Gemini 地点主题不得伪装成 `interest_topic`。
- 构建（16-02 lifecycle：manifest + stage→gate→promote；orphan 删除）：
  ```text
  python -m personal_knowledge.application.build_google_normalized_events --write
  python -m personal_knowledge.application.build_google_light_assertions --write
  python -m personal_knowledge.application.build_google_light_assertions --rollback  # ops only
  ```
- 只读消费（**不是** KU）：
  - CLI/backend：`list_google_light_assertions` / `get_google_light_assertion`
  - REST：`GET /google/assertions`、`GET /google/assertions/<id>`
  - MCP：`list_google_assertions`、`get_google_assertion`
  - 响应含 `not_knowledge_unit: true`、`kind: google_light_assertion`
- 报告：`var/reports/analysis/ai_context/google_light_structure_report.json`
- 活跃指针：`data/canonical/google/structured/db/google_structure_active_run.txt`
  （路径由 `project_paths` 解析；Phase 20 后物理位置在 `data/`）

### 3.3 career-os 消费方式

- 本仓库是**个人数据仓库**，通过 MCP / REST / CLI 提供只读证据。
- **career-os 经 LLM 中介消费 KU**（取证 → 提案 → 确认 → 写入 career-os），**不做**批量同步写库。
- 详见 [integration/README.md](../../integration/README.md)「下游消费：Career OS」。

## 4. 消费入口（只读）

| 能力 | CLI | REST | MCP |
|------|-----|------|-----|
| 语义检索 | `unified_search.py semantic` | `POST /search/semantic` | `search_semantic` |
| 知识状态（含 `ssot` / `fallback_policy`） | `unified_search.py knowledge` | `GET /knowledge` | `knowledge_status` |

Active authority：`var/db/personal_system.sqlite` 的 `serving_authority` →
immutable `serving_snapshots`。`var/db/knowledge_index_active.txt` 仅为旧入口
兼容投影；发生漂移时 SQLite 胜出，检索响应暴露 snapshot id/hash/drift，
doctor 返回非零。promote / rollback **不**经分发接口。

## 5. 相关代码

- 路径 SSOT：`src/personal_knowledge/core/project_paths.py`
- 状态与检索 backend：`src/personal_knowledge/retrieval/unified_search.py`
  - `get_knowledge_status`
  - `search_knowledge_units`（layered fallback 契约见上文）
- 契约测试：`tests/contract/test_knowledge_distribution_contracts.py`
- Phase 上下文：`.planning/phases/15-retrieval-ssot-governance/15-CONTEXT.md`
- 物理布局：`docs/architecture/repository-zones.md`（Phase 20）
