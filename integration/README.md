# integration

integration 对应跨源统合层：消费各来源已清洗的 structured 证据，建立统一事件/实体/关系，并支撑知识单元索引与分发接口。

> **Phase 20（2026-07-13）：** 运行时数据库、分析报告与私有 runtime 已物理迁到 `var/`；Agent/Google/imports 迁到 `data/`。本目录保留脚本兼容层、evals 与说明文档。路径请以 `personal_knowledge.core.project_paths` 为准。

## 角色

```text
Google / Agent 数据（data/canonical|raw）
  -> integration 管线（src/personal_knowledge + 兼容 scripts）
  -> 统一事件 / 实体 / 关系 / 综合分析（var/db + var/reports）
  -> 知识单元索引 (Chroma active) + 分发接口
```

## 数据分发接口 (CLI / REST / MCP)

统一后端：`src/personal_knowledge/retrieval/unified_search.py`（console：`rag-search`；兼容 shim 仍可用）。

| 能力 | CLI | REST | MCP |
|------|-----|------|-----|
| 语义检索 (knowledge-first) | `rag-search semantic` | `POST /search/semantic` | `search_semantic` |
| 知识索引状态 | `rag-search knowledge` | `GET /knowledge` | `knowledge_status` |
| 统计 (含 knowledge) | `stats` | `GET /stats` | `stats` |
| 事件/记忆分页导出 | — | `/data/*` | `data_*` |
| 精确查询 | `query` | `POST /search/query` | `query_events` |

Active 指针：`var/db/knowledge_index_active.txt`。  
promote/rollback **不**经分发接口，使用 `promote_knowledge_index` / `rollback_knowledge_checkpoint`。

- 检索三层 SSOT / hybrid：[`docs/architecture/retrieval-ssot.md`](../docs/architecture/retrieval-ssot.md)
- 物理分区：[`docs/architecture/repository-zones.md`](../docs/architecture/repository-zones.md)

## 下游消费：Career OS（LLM 中介）

本仓库 = **个人数据仓库**，给 LLM 提供只读证据；**不**直接写 Career OS 文件。

```text
本仓库 MCP/REST → LLM → 更新 Career OS（profile / 简历素材等）
```

启动供数：

```powershell
rag-api
# 或: python -m personal_knowledge.services.api_server
rag-mcp
# 或: python -m personal_knowledge.services.mcp_server
```

## 当前物理 I/O（Phase 20）

### 输入

| 源 | 路径 |
|----|------|
| Agent 会话证据 | `data/canonical/agent/structured/db/`（`agent_conversations` / `agentsview_normalized` / `agent_data`） |
| Google structured | `data/canonical/google/structured/db/google_data.sqlite` |
| Google raw | `data/raw/google/` |
| Imports | `data/imports/` |
| AgentsView live | `%USERPROFILE%/.agentsview/sessions.db`（**只读，永不搬迁**） |

### 输出

| 产物 | 路径 |
|------|------|
| 统一库 | `var/db/personal_system.sqlite` |
| Active KU 指针 | `var/db/knowledge_index_active.txt` |
| DuckDB 对话图 | `var/db/conversation_graph.duckdb` |
| 统一 CSV | `var/db/structured/`（原 `integration/structured`） |
| 画像/报表 | `var/reports/analysis/` |
| ai_context | `var/reports/analysis/ai_context/` |
| eval runs | `var/reports/analysis/evaluations/` |
| runtime private | `var/runtime/` |
| 日志 | `var/logs/` |

## 本目录残留（兼容）

```text
integration/
  scripts/           # 兼容 shim + 部分 governance 入口；权威实现在 src/personal_knowledge
  evals/             # 私有 frozen 等（gitignore）；公共 synthetic 也在 assets/evals
  README.md
  # db/ runtime/ analysis/ 等已迁出，仅可能残留 .bak-phase20
```

## 重建（推荐新入口）

```powershell
# 从仓库根目录
python -m personal_knowledge.application.build_integrated_system
python -m personal_knowledge.application.build_deep_profiles
```

兼容：

```powershell
python integration\scripts\build_integrated_system.py
```

## 核心表（位于 `var/db/personal_system.sqlite`）

- `source_modules`、`unified_events`、`entities`、`event_entities`、`entity_links`
- `memory_items` / `memory_links` / `memory_relations`
- `canonical_knowledge_units` / `knowledge_units` / index 版本表
- `module_summaries`、`cross_module_insights`
- `memory_evidence_bundles`

## 深度画像与记忆层

深度画像基于 `personal_system.sqlite` 生成，不改变 raw。报告写在 `var/reports/analysis/`。

- profile：`var/reports/analysis/stage1_profile/profile.*`
- 记忆/向量/关系评测：`var/reports/analysis/ai_context/`
- Phase 06 深挖结果不自动写回 `memory_items`

## 相关

- 路径解析：`src/personal_knowledge/core/project_paths.py`
- 迁移记录：`.planning/phases/20-physical-data-runtime-relocation/`
