# Product sync runbook

**Status:** supported (2026-07-17)
**Audience:** humans and coding agents operating this repo

## Goal

Keep **local conversation evidence** and (separately) **knowledge units** current
without running the retired integrated batch (`rag-pipeline` steps 1–12).

## Canonical product entry

```powershell
cd <project-root>
pip install -e .   # once / after entrypoint changes
$env:PYTHONPATH = "<project-root>\src"   # if not using installed package

# Dialogue SSOT: AgentsView live → normalized → canonical
pk-sync conversations           # dry-run (default)
pk-sync conversations --write   # publish DBs

# Other source products (all dry-run by default)
pk-sync turns                    # preview Turn vector rebuild
pk-sync turns --write            # publish only after retrieval probe succeeds
pk-sync google                   # preview normalized + assertion lifecycle
pk-sync google --write           # publish only after privacy gate succeeds
pk-sync status --json            # read-only versions/watermarks/drift
```

Equivalent module form:

```powershell
python -m personal_knowledge.application.sync conversations --write
```

### What `pk-sync conversations` does

| Step | Module | Writes |
|------|--------|--------|
| 1 Inventory | `application.conversation.import_agentsview_sessions` | report only under `var/reports/analysis/ai_context/` |
| 2 Normalized | `application.conversation.build_agentsview_normalized` | `data/canonical/agent/structured/db/agentsview_normalized.sqlite` (with `--write`) |
| 3 Canonical | `application.conversation.build_canonical_agent_conversations` | `data/canonical/agent/structured/db/agent_conversations.sqlite` (with `--write`) |

- **Never writes** `%USERPROFILE%\.agentsview\sessions.db` (protected-external, read-only).
- Privacy gates: secret sessions get no message bodies; local PII/credential scan.
- A successful `--write` appends immutable artifact versions and source
  watermarks. Repeating unchanged input is a metadata no-op. Dry-run, build
  failure, privacy failure, or retrieval-probe failure does not advance them.
- None of these commands calls paid KU extraction or activates a composite
  serving snapshot.

## Composite serving authority (Phase 23)

`var/db/personal_system.sqlite` is the authority for the active immutable
snapshot. `var/db/knowledge_index_active.txt` is only a compatibility
projection. When they differ, SQLite wins and `doctor` fails closed.

### Schema and safe current-state bootstrap

```powershell
# Inspect first; no write when --write is absent
python -m personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables --inspect
python -m personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables --write

# Read-only proof inventory; does not create or activate a snapshot
python -m personal_knowledge.application.serving.snapshots bootstrap `
  --eval-gate <passing-eval-gate.json>

# Explicitly write a complete DRAFT only; still no active change
python -m personal_knowledge.application.serving.snapshots bootstrap `
  --eval-gate <passing-eval-gate.json> --write
```

Bootstrap fails with `missing_proofs` until Conversation, Turn, Google and KU
publications all have version-bound watermarks. It also records the tracked
retrieval contract and named evaluation artifact only on explicit `--write`.

### Validate, activate, roll back, recover

```powershell
python -m personal_knowledge.application.serving.snapshots validate --snapshot <ss_id>
python -m personal_knowledge.application.serving.snapshots activate --snapshot <ss_id>
python -m personal_knowledge.application.serving.snapshots status

# Roll back by reactivating an existing validated immutable snapshot
python -m personal_knowledge.application.serving.snapshots rollback --snapshot <prior_ss_id>

# Inspect pointer drift, then explicitly repair projection from SQLite authority
python -m personal_knowledge.application.serving.snapshots repair-pointer
python -m personal_knowledge.application.serving.snapshots repair-pointer --write

# Full read-only product integrity gate; critical failure exits 1
pk-ku doctor --json --skip-ports
python -m personal_knowledge.governance.preflight --ci
```

Validation verifies all required registry roles, Chroma count/checksum, a
passing eval gate, typed evidence resolution and watermark ordering. Failed
validation never changes active authority. Activation commits SQLite first;
pointer projection failure is reported as drift and is repaired separately.
Normal `status`, `doctor`, validation diagnostics and repair dry-runs are
read-only.

### After conversation sync (optional)

| Need | Command / module (not via rag-pipeline) |
|------|----------------------------------------|
| Conversation source pointer | `python -m personal_knowledge.application.conversation.rollback_agent_conversation_source --to canonical --write` |
| Session summaries (LLM) | `python -m personal_knowledge.application.conversation.summary --write` |
| Turn vectors | `pk-sync turns --write` |
| **Knowledge unit incremental** | **See [ku-incremental.md](ku-incremental.md)** — start with `pk-ku inspect`; full chain: prepare → extract → extract-gate → canonical → publish → vector → canary → promote → watermark |
| Promote KU | After eval; see ku-incremental.md Step E (`pk-ku promote` / `pk-ku watermark`) |

**Do not** chain `pk-sync` into `build_knowledge_inventory --write` + `build_knowledge_units_prod --start`.  
That freezes the **full** eligible set and re-queues old evidence (banned for daily use).

### Phase 42：稳定会话键改造后的顺序与复检

涉及会话去重键或 evidence 口径的改动，必须先用现行代码执行一次常规
`pk-sync conversations --write` 消化 normalized 数据积压，再切换新键重建
canonical；否则 `pk-ku inspect` 的 delta 无法归因。改键首轮之后，`deleted_refs`
突增属于 superseded/合并副本退出 eligible 集的真实口径修正，双 watermark 轨
（`committed` / `committed_assistant`）各执行一次受控 `inspect → prepare` 即可。
只有“inspect 有 delta 而 prepare 为 no_op”才是 Gate B 真异常，应立即 STOP。

重建后日常复检可运行 `pk-ku doctor --skip-ports`；其中 `session_dedup` 是
warn-only 观测项，不阻断产品健康检查。正式 canonical 库可执行以下三条零重复 SQL：

```sql
-- A. 一个源会话只归属一个 canonical session
SELECT COUNT(*) FROM (SELECT source, source_session_id FROM session_source_links
  GROUP BY 1,2 HAVING COUNT(DISTINCT canonical_session_id)>1);
-- B. active 稳定键唯一
SELECT COUNT(*) FROM (SELECT s.source, s.source_session_id FROM session_source_links s
  JOIN canonical_sessions c USING(canonical_session_id)
  WHERE c.lifecycle IS NULL OR c.lifecycle='active'
  GROUP BY 1,2 HAVING COUNT(DISTINCT s.canonical_session_id)>1);
-- C. 消息键唯一
SELECT COUNT(*) FROM (SELECT canonical_session_id, ordinal FROM canonical_messages
  GROUP BY 1,2 HAVING COUNT(*)>1);
```

## Retired: integrated pipeline

| Entry | Status |
|-------|--------|
| `rag-pipeline` | **Retired product entry** — prints redirect, **exit 2** |
| `run_pipeline` steps 1–12 | **Blocked** without `--legacy-integrated` |
| Emergency only | `PK_ALLOW_LEGACY_PIPELINE=1` + `--legacy-integrated` |

Those steps rebuild `personal_system.sqlite` / memory / `personal_events` vectors.
They are **not** the knowledge SSOT path (KU is). Prefer never for day-to-day work.

## Verify after sync

```powershell
# Counts / presence
python -c "from pathlib import Path; from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB, AGENTSVIEW_NORMALIZED_DB; print(AGENT_CONVERSATIONS_DB, Path(AGENT_CONVERSATIONS_DB).exists())"

# Optional: services for MCP/search
curl.exe --noproxy "*" http://127.0.0.1:8000/health
curl.exe --noproxy "*" http://127.0.0.1:8789/health
```

## Canonical conversation v2 (Phase 62)

The canonical conversation authority now supports typed v2 events alongside the
legacy `canonical_sessions/messages/tool_events` rows. v2 projection rows carry
a `v2|` id prefix and `source='legacy'` (the live CHECK constraint only admits
`agentsview|legacy`); activation **preserves** pre-existing legacy rows and
replaces only the v2 projection of the switched generation.

### Dry-run / shadow (zero paid calls, D-31)

```powershell
# probe every family capability + snapshot/event estimate (metadata-only)
pk-sync conversations --v2-dry-run --v2-source <source-root>

# capture + adapt + stage NON-active generations + metadata-only report
pk-sync conversations --v2-shadow --write --v2-source <source-root>
# shadow DB default: data/staging/v2 (never the live canonical store)
# explicit shadow target:  --v2-db <path>
```

Shadow output is metadata-only: 17-family counts/fidelity, artifact/generation
digests, compatibility parity, view counts, deterministic-gate counts, cost
estimate, old-run supersession readiness, source fingerprints, exact rollback
target. Never activates, never advances watermarks, never calls a provider.

### Fidelity report and activation gate

`pk-sync conversations --event-v2-shadow --report ...` (62-07) produces the
per-family fidelity evidence and the activation recommendation. Activation is
permitted only after an explicit human approval recorded in
62-07-SUMMARY/62-VALIDATION; `paid_calls=0` and every native-available session
is captured or explicitly blocked.

### Activate / status / rollback

```powershell
# 1) stage into the live canonical DB (non-active)
pk-sync conversations --v2-shadow --write --v2-source <source-root> `
    --v2-db data/canonical/agent/structured/db/agent_conversations.sqlite

# 2) activate the approved generation (delegates to event_generations only)
pk-sync conversations --v2-activate <generation-id> --write `
    --v2-db data/canonical/agent/structured/db/agent_conversations.sqlite

# 3) health
pk-sync status --json
pk-ku doctor --json
rag-search stats --json
python -m pytest -q tests/contract/test_conversation_v2_compatibility.py `
    tests/integration/test_conversation_v2_sync.py

# 4) rollback (clears the v2 projection rows + demotes authority; legacy kept)
python -c "import sqlite3; from personal_knowledge.application.conversation.compatibility_projection import clear_compatibility_projection; \
con=sqlite3.connect('data/canonical/agent/structured/db/agent_conversations.sqlite'); clear_compatibility_projection(con); \
con.execute('UPDATE ce_generation_authority SET active=0 WHERE active=1'); con.commit(); con.close()"
```

### Partial-family interpretation

- `no_source` = no native artifact discovered for that family in the cohort
  (not an error; the `native_available_captured_or_blocked` gate only counts
  families with discovered sessions).
- `partial` = adapted with honest fidelity loss (e.g. claude staged from a
  live export with unknown record kinds preserved as `unknown_native`).
- `blocked` = capture or staging failed closed; fix the adapter/staging error
  and re-run shadow before activation.

### Old-run supersession and paid extraction

The two legacy message-level prepare runs (3,224 user + 21,263 assistant
items) remain **audit-only/non-executable**; `pk-ku extract` refuses them
(`LegacyRunSupersededError`). Any future paid semantic pilot requires a
separate explicit user cost-approval checkpoint — the v2 activation approval
does NOT authorize paid LLM extraction.

## Native client-directory discovery (Phase 62 seam, 不经过 AgentsView 中转)

`pk-sync conversations` 的 v2 seam 支持**直接扫描本机 AI 客户端目录**：

```powershell
# 1) metadata-only 发现报告（零写入、零付费）
pk-sync conversations --v2-native-dry-run

# 2) 发现 -> 增量 stage（按 content hash 去重）-> NON-active shadow
pk-sync conversations --v2-native
#    stage 根: data/staging/v2/native/<family>/<relative-path>
#    shadow 报告: data/staging/v2/report.json (metadata-only)
#    大件（zcode 实时库快照 ~313MB）默认已覆盖（--v2-byte-limit 600MB）

# 3) 人工确认后显式激活（永不自动激活，D-18）
pk-sync conversations --v2-activate <generation-id> --write \
    --v2-db data/canonical/agent/structured/db/agent_conversations.sqlite
```

### 发现层（新增）

- `src/personal_knowledge/adapters/conversation_sources/discovery.py`：
  - `FAMILY_CLIENT_ROOTS`：家族 -> 本机候选根（可用 `PK_CLIENT_ROOT_<FAMILY>` 覆盖）。
  - `discover_client_sources()`：用**家族自己的 detector** 探测候选根下文件（不重造解析器）。
  - `stage_client_sources()`：按 content hash 增量复制到 stage 根（`<family>/<relative>`），幂等。
  - `SQLITE_ALLOWLISTS`：SQLite 家族（zcode/mimo/opencode/antigravity/chatgpt）的 LIVE allowlist，
    单一数据源引用各适配器模块常量。
- `v2_sync.py` 修复（62-07 记录的环境缺口）：探测文件头识别 SQLite 魔数（`source_kind="sqlite"`），
  SQLite 家族捕获走 `capture_sqlite`（WAL-safe online backup + allowlist），不再 `no_source`。
- `AdaptedSession` 会话上下文字段（additive）：`cwd` / `git_branch` / `model` / `title` / `stop_reason`，
  各家族适配器从原生工件提取（Codex `session_meta.cwd`、Claude `cwd`/`gitBranch`/`slug`/`stop_reason` 等）；
  `ce_sessions` 同名列持久化；compatibility projection 已把 `cwd/git_branch/model` 映射到
  legacy `canonical_sessions`（此前硬编码 None）。


### 定时同步

```powershell
# 注册每日 23:00 计划任务（仅 shadow，永不激活）
pwsh -File tools\register-native-sync.ps1
# 立即执行一次 / 删除任务
pwsh -File tools\register-native-sync.ps1 -RunNow
pwsh -File tools\register-native-sync.ps1 -Unregister
```

日志：`var/logs/native-sync.log`。任务只做 discover→stage→shadow；
激活始终是人工显式步骤，与 Phase 62 D-18/D-31 一致（零付费）。
## Live incremental sync (single live generation, 2026-09)

替代"每轮全量重写快照"的路径：**稳定槽位身份 + 原地增量更新**。
`artifact_id = sha256("art|<family>|<mirror_path>")`（槽位与文件内容脱钩，`CONTRACT_VERSION=2`），
单一 live 代原地增删改；变更检测走 mtime+size 快筛 + 槽位 `content_hash` 水位。
存储上不可能再出现"15 代 × 全量"式膨胀；审计走 `ce_live_sync_log`。

```powershell
# 跑一次增量（stage 客户端源 -> 镜像 -> 增量应用）
pk-sync conversations --live-sync

# 预演：报告将发生什么，零写入（连库文件都不创建）
pk-sync conversations --live-dry-run --live-db <db> --live-mirror <mirror>

# 常驻后台监听（30s 轮询 + 连续 2 次稳定扫描才触发；Ctrl-C 优雅退出）
pk-sync conversations --watch [--watch-interval 30]

# 只读运维视图：槽位计数 / 最近一次同步 / 待变更数（零写入）
pk-sync conversations --live-status
```

默认目标：`--live-db` = `data/staging/v2/agent_conversations_v2.sqlite`（暂存库），
`--live-mirror` = `data/staging/v2/native`。**live 模式默认永不指向 canonical 生产库**；
要用增量直接维护 canonical，必须显式传 `--live-db data/canonical/agent/structured/db/agent_conversations.sqlite`。
与 `--v2-*` 模式互斥（同时给出则 fail-closed 退出码 2）。

机制要点（改动排查时先看这些）：

| 关注点 | 位置 |
|---|---|
| 槽位身份 | `adapters/conversation_sources/snapshots.py::make_slot_artifact_id` |
| 增量引擎（单事务剪枝+幂等插入） | `application/conversation/live_sync.py::live_sync_once` |
| 监听循环（防抖/锁/日志/信号） | `application/conversation/watch.py::run_watch` |
| live 表与索引（additive） | `application/conversation/event_schema.py` |
| CLI 路由 | `application/sync.py::_cmd_conversations`（live 优先于 v2） |

运维提示：

- 单实例锁：`<mirror 同级>/live-sync.lock`（PID+心跳；死进程锁自动回收，活进程持锁时第二个实例 `status=locked` 退出码 1）
- 监听日志：`<mirror 同级>/live-sync.log`（1MB 滚动保留一代；每轮心跳，可用心跳时间判断进程死活）
- 单轮失败记 `status=partial` 并在下个稳定扫描重试，循环不死
- 幂等保证：源零变化时第二次运行 `status=no-op`、0 行写入、库文件字节不变

## Event-delta preparation status

`pk-ku view-status` (without `--run`) reads the current authority and consumer
backlog in one read-only snapshot. `--conversation-db <path>` selects an explicit
database; `view-status --run <run-id>` keeps the existing per-run audit behavior.

- `pending`: committed event pages remain; inspect `pending_delta_count` and
  `pending_event_count`, then use bounded `pk-ku view-consume` when appropriate.
- `withdrawal_required`: pages are prepared, but persisted withdrawal references
  have not been executed. Their count represents queued reference occurrences.
- `prepared`: no pending prepare pages or withdrawal references were observed.
  This is not extraction, review, publication or memory-quality completion.
- `uninitialized` / `degraded` (exit 2): source authority/history is unavailable,
  or the consumer checkpoint is inconsistent. Unknown counts stay null.

`prepare_caught_up` describes only this prepare cursor. `memory_ready=false` and
`extraction_status=not_automated` preserve the current delivery boundary. The
consumer rejects a missing/mismatched committed checkpoint and an active
generation without its own activation delta history; it never repairs these by
resetting the cursor, fabricating an empty delta or running a full inventory.

## One-time baseline for an existing active generation

For an active generation created before durable deltas existed, run
`pk-ku view-baseline` first. This is a read-only, streaming full-source digest
and metadata preview; its `event_count` is the entire one-time historical scope,
not today's new evidence. It does not prepare views or call a model.

After reviewing and approving that historical scope, use
`pk-ku view-baseline --write --approval <confirmation-from-preview>`.
The confirmation binds source rows and activation binding metadata. Changed
source/context/bindings require a fresh preview and approval. Initialization
atomically adds a baseline audit and all current event references as pending;
it does not activate, rewrite source/projection rows, advance a cursor or publish
knowledge. Existing history/consumer state blocks first initialization. Exact
replay verifies the baseline ledger without adding rows. This does not reverify
the derived projection or authorize a paid semantic pilot/full extraction.

Then `pk-ku view-status` should show the baseline backlog. Subsequent
`pk-ku view-consume` remains bounded per invocation and never calls a provider.

## Related docs

- Agent operating manual: [../AGENTS.md](../AGENTS.md)
- **KU incremental (delta only):** [ku-incremental.md](ku-incremental.md)
- Retrieval SSOT: [../architecture/retrieval-ssot.md](../architecture/retrieval-ssot.md)
- Zones: [../architecture/repository-zones.md](../architecture/repository-zones.md)
