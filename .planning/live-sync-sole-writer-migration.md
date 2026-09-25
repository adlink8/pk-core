# live_sync 唯一写者迁移计划（2026-09-25 拍板）

## 决策记录

- **staging 路线废弃**：不再做"全量重建投影 + staging + 整库原子替换"发布。
- **实时入库**：live_sync 增量链成为权威库的唯一写者。
- **缺口家族**：为 5 家新写原生适配器（不接 AgentsView、不留 feeder 通道）。
- **旧世代清理**：切换时做集合差分，确认投影无缺口后删除 ce_* 中冻结/重复旧世代。
- authority_ingest 降级为一次性手动回填工具，退出自动调度。

## 现状事实（2026-09-25 实测）

- 权威库 `data/canonical/agent/structured/db/agent_conversations.sqlite` 7,317 MB。
- ce_* 事件模型：5,036 会话 / 2,450,967 事件；canonical 投影：2,382 会话 / 226,555 消息 / 15 家族全覆盖。
- **id 未对齐**：canonical 已是统一派生式 `cs|<family>|<native-session-id>`；ce_sessions 仍是路径派生 sha256 哈希。live_sync 直接写会产生整套重复副本。
- live_sync 集成测试 22/22 绿；其兼容投影 `upsert_compatibility_projection` 本身就是只增不删语义，现有 canonical 可原样作底。
- residual bug：投影 ordinal 按 ce session 发不按归并会话发（1,435 组冲突）。

## 缺口家族源根探测（2026-09-25 更新：适配器已全部存在，无需新写）

**P0 结论反转**：`src/personal_knowledge/adapters/conversation_sources/` 下 6 家适配器早已存在并注册
（qoder→claude_qoder.py、kimi/kimi-work→workbuddy_kimi.py、copilot.py、gemini.py、mimo_opencode.py），
discovery.py 的 `FAMILY_CLIENT_ROOTS` 也已配置好本机根路径。所谓"缺口家族"是 09-23 的过时判定——
实测 6 个根全部存在且有候选文件（qoder 2,676 / gemini 5,001 / kimi 342 / kimi-work 2 / copilot 86 / mimo 8）。

| 家族 | 源根（discovery 已配） | 实测数据形态 |
|---|---|---|
| qoder | `~/.qoder` | `projects/<项目>/<uuid>.jsonl`，Claude Code 格式兼容变体（6 种 type 行，多 workspace-directories/runtime-config/active-leaf），4 文件 1.15MB |
| kimi | `~/.kimi-code` | `server/events/session_<uuid>.jsonl` 一会话一文件 + `session_index.jsonl` 索引；**盲区：assistant 正文不落盘**，只有 user 输入+工具活动（8 文件 59MB） |
| gemini | `~/.gemini` | `tmp/<项目>/chats/session-*.json`，content 异构（user=parts 数组，gemini/info/error=字符串），工具输出在 session JSON 外的 tool-outputs/，5 文件 178KB |
| kimi-work | `~/.kimi-work` 等 | 2 个候选文件（基本无数据） |
| copilot | `~/.copilot` | 86 个候选文件 |
| mimo | `~/.local/share/mimocode` | **客户端已删但数据目录残留 8 个文件**，仍可采集 |

验证方式：`python -m personal_knowledge.application.sync conversations --live-sync --live-db <临时库>`
全流程试点（不碰权威库），进行中。

## 实施步骤（每步带验证闸）

1. **P0 家族覆盖验证 ✅（2026-09-25 完成）**：临时库 `var/db/live-pilot-scratch.sqlite` 全流程试点通过
   （1,107 秒，stage 126 新 / 1,601 跳过）。**15 家族全部采到**，6 个"缺口家族"零缺口：
   qoder 106 / kimi 105 / copilot 12 / mimo 15 / gemini 5 / chatgpt 170（ce 会话数）。
   投影 2,825 会话 / 148,242 消息 / 431,992 工具事件。
   待查：部分家族 ce→canonical 投影数下降明显（codex 1042→238、qoder 106→15、kimi 105→9），
   疑似同源 slot 按 canonical id 归并去重，P3 并轨时核实。
   kimi 的 assistant 正文盲区在门禁/报告层显式标注（上游不落盘，非丢数据）。
2. **P1 id 对齐迁移**（2026-09-25 深挖后范围重定义）：
   - **实测**：投影 session 级 id 已经是统一规则（`_session_key` = (family, native_session_id)，
     与 uniform_id_migration 同源）——codex 家族权威库与 live 投影的 cm id 形态已一致。
   - **真正的差距**：①权威库中 qoder/kimi/claude/gemini 的消息地址是 AgentsView 时代的 `av<序号>`，
     live 投影发的是 `文件#L行号`——同一条消息两套 id，P3 并轨会在同一 canonical 会话内出双份消息。
     处置方向：这些家族现在有原生根，原生采集应为准，AV 时代行在 P4 清理中让位（chatgpt 无原生根除外）。
   - **ordinal 冲突修复**：`_project_messages` 的 ordinal 按 ce session 发（1,435 组冲突），
     应改为投影后按 canonical session 统一重编号。
   - **kimi wire 修复 ✅（2026-09-25）**：`_path_session_key` 从路径提取 `session_<uuid>`，
     取代文件名词干兜底（曾把 97 份 wire.jsonl 快照折叠成伪会话 `wire`）；回归测试
     `test_kimi_wire_session_key_from_path_not_stem`。注意：镜像里的 97 个旧 slot 是"裸文件名"扁平路径，
     修复对它们不生效，需在重新采集时让位（P4/重灌范围）。
3. **P2 门禁移植**：authority_ingest 硬门清单（secret 残留、消息丢失、时间倒挂、重复源会话、孤儿消息、空会话）+ 软门 quarantine 进 live_sync capture 路径；grok 计数基准改比源侧实际行数。验证：grok 样本过门；故意投毒样本被拦。
4. **P3 投影并轨**：live_sync 投影以现有 canonical 2,382 会话为底 upsert；校验同 id 会话投影值一致（file_hash/message_count 抽样比对）。验证：并轨后 canonical 行数不增重复、不丢行。
5. **P4 旧世代清理**：ce_* 全量 vs 投影集合差分，确认无缺口后删除冻结/重复世代（用户已授权；差分报告存档）。验证：canonical 与 FTS 检索不受影响。
6. **P5 调度切换**：停 pk-authority-ingest，live-sync 高频调度或 watch 常驻；authority_ingest 保留为手动回填入口；FTS 改真增量（rowid 契约从此成立）。验证：一周观察期日志无 HARD 拦截。

## 收益清单（切换完成后）

- 实时入库：新会话/新消息分钟级可见（此前周级）。
- 库文件永不被整体替换 → FTS rowid 游标契约成立，检索层增量索引。
- 集合差分守卫不再需要（live_sync 天生只增不删，collection 语义）。
- 单事务写入，崩溃自动回滚，无半迁移状态。
