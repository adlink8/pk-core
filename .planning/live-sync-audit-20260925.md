# live_sync 链路全量缺陷清单（2026-09-25 三路审计）

> **最终状态（2026-09-25 深夜）**：48 项中 44 项已修复并提交（16 个 commit），2 项上游固有不可修（kimi 正文不落盘/codex id 不可靠）+ 1 项（antigravity summary-only）属客户端行为，1 项（AV 布局）已有防回归测试守着；另发现 1 个基线既有失败（test_conversation_event_generations 与 P1-3 择富规则冲突）留待裁决。最终回归 447 passed / 0 failed。**48 项清单至此关闭，无待修余项。** 后续工作进入迁移计划（P3 并轨 / P4 清理）。

## 总账

| 级别 | 数量 | 含义 |
|---|---|---|
| P0 | 6 | 会丢/错/折叠数据，阻断迁移 |
| P1 | 20 | 重复、漂移、门禁缺失，迁移后数据不可信 |
| P2 | 22 | 质量瑕疵与健壮性 |
| 上游固有 | 3 | 客户端不落盘，修不了，只能标注 |

数据层证据（试点库 148,242 消息 vs 权威库 226,555）：
- 重复消息：试点 36.7%（54,375 行，codex 一家占 46,096）；**权威库同口径也有 35.1%（79,538 行）——重复病是上游链路遗传的，不是 live_sync 新造的**。
- ordinal 冲突会话：试点 111 / 权威 453，最大重复度 6,036（同一会话）。
- 442/1,593 个共有会话消息行数不一致（试点最多缺 4,601 行），且 message_count 列只有 3 个不同——**message_count 元数据不可信**。
- grok/cursor 消息 timestamp 100% NULL；qoder 65.4%、claude 57.7% 消息 role=system（分类污染）；6.6% 消息无正文。
- 好消息：两库均零孤儿，外键层完好。

## P0（阻断迁移，必须修）

| # | 缺陷 | 位置 | 证据 |
|---|---|---|---|
| P0-1 | **stale 行无代际标记**：源截断/删行后旧行永久以现行数据身份留在 ce 与 canonical，参与投影、计数、检索 | live_sync.py:41-43,779-784,1103-1113 | 只增不删的 collection 语义没有"现行版"载体 |
| P0-2 | **cursor SQLite 多 thread 全折叠**：session_id 循环外算一次，多 thread 撞 duplicate id 抛异常；消息不过滤 thread 混流 | cursor.py:141,168-238 | 代码 |
| P0-3 | **grok 会话键取 summary.md 首行**：所有导出同标题头 → 全家族折叠成一个伪会话 | grok.py:292-301,752-757 | 代码 |
| P0-4 | **kimi agents/ 目录不认**：子代理判定只查 `subagents/`，kimi 真实布局是 `agents/` → 与主会话同批时整会话失败，单独时误判成多会话 | workbuddy_kimi.py:390-393,842-846 | 代码 |
| P0-5 | **kimi `_timestamp` 秒级/字符串毫秒透传**：occurred_at 变 "1758768000" 垃圾值 | workbuddy_kimi.py:168-175 | 代码 |
| P0-6 | **pathless 通道 blob 按 artifact_id 寻址**：必然路径不存在 → grok/chatgpt 的 AV 通道所有抓取直接抛异常 | agentsview_pathless.py:88（contracts.py:97-105 docstring 早已预告） | 代码 |

## P1（迁移后数据不可信，按批次修）

**身份与重复（数据已证实）：**
| # | 缺陷 | 位置 |
|---|---|---|
| P1-1 | 消息地址路径依赖 → 同一消息多副本多 id，canonical 内重复（试点 36.7% 行重复） | contracts.py:125-140 + 各适配器 locator |
| P1-2 | canonical 会话级字段"最后排序者赢"：多副本 started/ended/message_count 随 slot 哈希序随机互相覆盖，无 min/max 合并 | compatibility_projection.py:282-314,522-553 |
| P1-3 | "保留最富副本"只在单次 compute 内有效：跨 apply 时富副本被贫副本原地覆盖，无版本历史 | compatibility_projection.py:193-198 + live_sync.py:1377-1396 |
| P1-4 | 跨 family 嵌套根重复：`~/.gemini/antigravity` 被 gemini 与 antigravity 双认领 → 同会话两套 canonical | discovery.py:163-169,593-597 |
| P1-5 | 跨 slot 同 relation_id 被 INSERT OR IGNORE 静默丢弃，关系端点锚在先到副本 | live_sync.py:604-649,840-848 |
| P1-6 | _richer 只比长度：源编辑删短后旧长文胜出，stale 旧版参与竞争放大 | compatibility_projection.py:251-264 |
| P1-7 | stem 兜底伪会话键（kimi 已修，claude:724-727 / copilot:423-430 / cursor:400 / kimi journal 同类仍在） | 各适配器 |
| P1-8 | codex native_event_id 作 dict key 时后者覆盖前者：turn 边界错锚、call 配对丢历史 | codex.py:1010,1023 |

**时间与分类（数据已证实）：**
| # | 缺陷 | 位置 |
|---|---|---|
| P1-9 | 时间戳零归一化家族：gemini/codex/copilot/cursor/grok 原样透传 → grok/cursor 消息 100% NULL timestamp | 各适配器 + time_utils.py:24-93（秒级盲区） |
| P1-10 | role 污染：qoder 65.4%/claude 57.7% 消息被标 system（tool_result/未知行归类错误） | claude_qoder.py 分类逻辑 |
| P1-11 | kimi 会话起止 naive/aware datetime 混排可能倒挂 | workbuddy_kimi.py:200-209 |

**引擎与契约：**
| # | 缺陷 | 位置 |
|---|---|---|
| P1-12 | **六项硬门 + 软门 quarantine 在 live 路径全缺**（secret/消息丢失/时间倒挂/重复源会话/空会话/quarantine 表） | authority_ingest.py:145-266 vs live_sync.py:1131-1175 |
| P1-13 | capture 与指纹写入竞态：捕获后文件再追加 → 新字节被旧指纹固化，尾部永久漏采 | live_sync.py:1307-1403,1533-1539 |
| P1-14 | FTS 契约两处破坏：UPDATE 改 content 不通知索引；clear 重投影后 rowid 回收跌破 watermark 静默缺索引 | compatibility_projection.py:548-553,600-632 vs conversation_fts.py:29-33,395-403 |
| P1-15 | 一次性 --live-sync 不加锁 + copy2 非原子：半截文件可被捕获并被指纹固化 | watch.py:539,882-884 + discovery.py:625 |
| P1-16 | legacy 迁移行被投影原地覆盖：file_hash/relationship_type/lifecycle 被默认值抹掉且无历史 | compatibility_projection.py:303,536-553 |
| P1-17 | grok tool_call 原生 id 重复 → event_id 撞车整会话失败；坏 JSONL 行静默丢 | grok.py:414-424,185-201 |
| P1-18 | pi thinking 正文截断 2048、toolCall 截断 512，fidelity 未诚实降级 | pi.py:333-334,393-401 |
| P1-19 | 无 native_session_id 的会话永不塌缩（ce:<hash> 回退含 slot 身份） | compatibility_projection.py:270 |
| P1-20 | `_sanitize` 把 `|` 换 `/`：不同 native key 可塌缩同 id，后到覆盖先到 | uniform_id_migration.py:152-166 |

## P2（质量瑕疵，迁移后排队）

适配器层：gemini title 不滤注入块 / gemini `_message_text` 走 str(dict) / copilot ended_at 取首个 shutdown / copilot 同 tool_id 静默覆盖 / copilot .json 丢非字符串块 / copilot 双读全文件 / claude title 吃子代理文本 / claude usage 行号兜底 / cursor JSONL tool_result 静默丢 / cursor cwd 不解析转义 / grok events.jsonl 整型时间丢 / grok detect 全文件载入 / pi detect 只看首行 / pi O(n²) 排序 / mimo 孤儿 part 丢弃 / mimo usage 裸词别名误报 / mimo 坏 JSON 静默 {} / zcode ended_at 字典序混格式 / zcode usage 裸词别名 / antigravity 未知 role 静默跳过 / antigravity 空正文报 COMPLETE / antigravity step_type=132 只取首个 blob / protobuf lenient 文本拼凑 / chatgpt pathless 空内容标 MAPPED / jsonl strict=True 一行坏全文件炸 / detect 标记依赖紧凑 JSON。

引擎层：Windows PID 复用锁死 / mirror 清空无防护门 / registry 外 family 静默停用 / count_limit 2001 后静默欠采 / stage rel 两套实现裸名互相覆盖 / tool 投影 source_ref 列不存在被丢 / content=""与 summary or-回退不对称。

## 上游固有（修不了，报告层标注）

1. kimi 事件流不含 assistant 正文（wire.jsonl 同会话有但 journals 没有）。
2. codex 原生消息 id 不可靠（446 条共用 'agent_message'）。
3. 部分家族 summary-only 消息只有标题（antigravity protobuf）。

## 修复批次建议

- **批次 A（迁移阻断）**：P0-1～6 + P1-1～5。这是"live_sync 唯一写者"成立的最小集合——身份稳定、不折叠、不翻覆。预计是主干工作。
- **批次 B（数据可信）**：P1-6～20。门禁移植、时间戳、role 分类、FTS 契约。
- **批次 C**：P2 长尾 + 重新采集 + P4 清理。
- **独立决策项**：权威库自身 35% 重复行怎么办（遗传病，重灌 or 保留，影响 P3 并轨基线选择）。

## 批次 A 执行进度（2026-09-25 五代理并行修复，287 测试全绿，未提交）

| 项 | 状态 | 说明 |
|---|---|---|
| P0-2 cursor 多线程折叠 | ✅ | 每 thread 独立会话；单 thread id 零漂移，多 thread id 一次性变更 |
| P0-3 grok 会话键折叠 | ✅ | 兜底链改 info.id→同集合 summary.json→全路径键；删正文派生兜底；ADAPTER_VERSION 1.4.0 |
| P0-4 kimi agents/ 判定 | ✅ | family-aware：agents/ 段且非 main 判子代理；workbuddy 零变化 |
| P0-5 kimi 时间戳 | ✅ | 秒/毫秒/数字串/ISO 统一规范化，Z 后缀 |
| P0-6 pathless blob 寻址 | ✅ | content_hash 寻址 + 空内容 UNAVAILABLE；红绿验证过 |
| P1-2 会话字段互踩 | ✅ | _merge_session_copies：min/max/最大计数/确定性 cwd·model |
| P1-3 compute 内空正文覆盖 | ✅ | _richer 键改 (content is not None, 长度) |
| **P0-1 stale 标记** | ✅ | ce_events/ce_sessions 加 stale_at 列；slot 级截断→标记、恢复→复活（dirty/unchanged 双路）、removed slot 全标；投影读取过滤 stale_at IS NULL；canonical 已投影行归 P4 |
| **P1-1 消息地址去路径化** | ✅ | per-family：12 家族 native uuid 优先（claude/qoder/gemini/copilot/workbuddy/kimi/kimi-work/zcode/mimo/opencode/antigravity/pi），codex/grok/chatgpt/cursor 保持 locator 优先；同 artifact 内 native id 重复判占位回退 locator。**连带影响：权威库存量 locator 形态 cm id 需重跑迁移 plan/apply 收敛（P3 前置）** |
| **P1-3 跨 apply 富副本覆盖** | ✅ | _upsert_rows 升级为与库内现存行对账合并（messages/tools 择富、sessions min/max 合并、message_count 可降以配合 stale 语义）；MERGE_WITH_STORED 逃生门；无跨轮版本历史（后续工作） |
| **P1-4 跨 family 嵌套根重复** | ✅ | discovery 通用路径包含剪枝：子树归更具体 family，别名/同根/同家族多根不误伤；env override 平权参与 |
| **P1-5 跨 slot relation 丢弃** | ✅ | 被吞关系计数可见（relations_ignored_duplicates）+ 端点择优（来者端点全部现行且在库端点全 stale 时刷新，旧值进 ce_relation_versions） |

批次 A 十一项全部完成（2026-09-25，8 个代理接力，最终回归 333 passed / 0 failed，未提交）。
