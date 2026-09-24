# 本机会话原生结构与解析验收

调查日期：2026-09-24。只读核对磁盘文件和当前适配器，没有改解析代码。数字来自当时的文件和权威库生效代 `shadow-cohort-0716eb642516`。

导入进 `ce_events` 之后，各软件已经是同一套字段（`kind`、`content`、`summary`、`session_id`）。缺口出在读原文件的时候：正文还在另一个字段、另一个文件，或文件里根本没有明文。

## 验收

公共接口是各家族的 `adapt()`（注册表 `adapt_for`）。一条会话解析成功的含义：

- 原文件里的每一条记录都变成一个事件，不允许静默丢掉。
- 能读到的用户、助手、工具、推理正文放进 `content`。
- 因为加密或源数据不在而读不出正文时，`content` 可以空，但事件上必须有一句原因，写明缺的是什么。不允许只标 `unknown_native`，也不允许空着不解释。
- 原因写在该事件的 field disposition `reason` 里，并带上原生类型名或字段名。

测试用最小夹具，不把真实对话正文写进仓库。

## Codex

一条会话是一个 `rollout-<时间>-<uuid>.jsonl`。活跃树 `D:\C_Links\.codex\sessions` 163 个，归档树 613 个。一行是 `timestamp`、`type`、`payload`。

会变成用户或助手消息的：`response_item` 且 `role` 为 user（6744）或 assistant（23498），`payload.type=agent_message`（1524），`event_msg` 的 `user_message`（47）和 `agent_message`（200）。

没有解析成正文的：

| 原生类型 | 条数 | 原因 |
|---|---:|---|
| `response_item` / `reasoning` | 63472 | `content` 全空，只有 `encrypted_content`。同级键没有密钥。13103 条有 `summary` 短文本 |
| `event_msg` / `agent_reasoning` | 175 | 明文在 `text`，目前只进了 reasoning |
| `compacted` | 524 | 适配器只认 `context_compacted`。明文在 `replacement_history` |
| `image_generation_call` | 30 | 没有分支。明文在 `revised_prompt` |
| `thread_goal_updated` | 277 | 没有分支。短文本在 `goal.objective` |
| `token_count` 且没有 `info` | 205 | 用量函数返回空后标成 unknown |
| `token_usage_record` | 2048 | 顶层类型没有分支。里面是 token 数字 |
| `inter_agent_communication_metadata` | 1524 | 只有布尔字段，没有正文 |

## Claude / Qoder

主会话是 `<项目>\<uuid>.jsonl`。子代理是同目录 `subagents\agent-*.jsonl`。正文在 `message.content[]` 里 `type=text` 的块。`thinking`、`tool_use`、`tool_result` 不是消息。

Claude 有 54 个子代理文件，每行都有 `agentId`。适配器把消息记在文件级会话上，再为每个 `agentId` 造一个只有 `subagent_boundary` 的会话。正文在同文件的另一个会话里。

Qoder 检测要求文件里出现 `isCompactSummary`，53 个文件里 6 个过检。`agent-ageneral-purpose-a92a969f4d4c5363.jsonl` 有 306 行，但 `text` 块只有 3 个。唯一没有 `agentId` 的是 `last-prompt`，于是多出来的会话只有一条 lifecycle。未映射类型：`active-leaf`、`runtime-config`、`workspace-directories`、`worktree-state`、`image` 块。

## Grok

一条会话是一个目录，里面同时有 `summary.json` 和 `chat_history.jsonl`。用户正文在 `type=user` 的 `content[].text`，助手在 `type=assistant` 的字符串 `content`。工具调用在 `assistant.tool_calls`，结果是 `tool_result`。推理的 `encrypted_content` 同级只有 `id`、`status`、`summary`、`type`。

导入一次只把一个文件交给适配器。`summary.json` 变成 91 个没有消息的会话，`chat_history.jsonl` 变成 89 个会话、2858 条消息。三份字节相同的 `chat_history.jsonl` 按内容哈希只留了一份，所以少 2 个。`events.jsonl`、`updates.jsonl`、`prompt_context.json` 不读。

## Kimi / Kimi-work / WorkBuddy

Kimi 旧日志是 `~\.kimi-code\sessions\**\wire.jsonl`。用户正文在 `turn.prompt` 的 `input[]` 和 `context.append_message`。助手正文在 `content.part` 且 `part.type=text`。`part.type` 只有 `text` 和 `think`。没有分支的顶层类型约 8271 条，主体是 `llm.request`。

旁边 8 个 `server/events/session_*.jsonl` 是信封（`kind=event`）。类型表里没有用户消息和助手消息。`turn.started` 的 `payload.prompt` 有 189 条非空，被定成轮次边界。助手正文在同一批会话的 `wire.jsonl`。不在类型表里的信封类型有 22 条（审批和 `cron.fired`），`error` 13 条被直接定成 unknown。

Kimi-work 的发现根是空的。4 个 `wire.jsonl` 在 `%APPDATA%\kimi-desktop\daimon-share\...\sessions\`。多一个未映射类型 `tools.register_user_tool`。

WorkBuddy 是 `projects\**\*.jsonl`。用户是 `type=message` 且 `role=user` 的 `content[].input_text`。助手是 `output_text`。推理在 `reasoning.rawContent[].text`，不在 `content`。未映射：`ai-title`、`custom-title`、`session-meta`。

## Antigravity / Gemini

Antigravity 一条会话是 `~\.gemini\antigravity\conversations\<uuid>.db`。正文在 `steps.step_payload` 的 protobuf，不在文本列。用户 `step_type=14`，助手 `step_type=15`。`unknown_native` 455 条全是 `step_type=17` 的执行错误，错误说明在 summary。空 part 被丢掉。

Gemini 的 5 个会话在 `~\.gemini\tmp\<项目>\chats\session-*.json`。没有 `role`。用户是 `type=user` 且 `content[].text`，助手是 `type=gemini` 的字符串 `content`。目录名 `tmp` 被扫描跳过，库里 0 条。

## Zcode / Pi

Zcode 是整库 `~\.zcode\cli\db\db.sqlite` 的一行 `session`。正文在 `part.data`，`type=text` 再按 `message.data.role` 区分。`timeline` 439 行变成 unknown（换模型、压缩、分叉）。`model_usage`、`tool_usage`、`todo` 等表不读。

Pi 一条会话是一个 JSONL。抽到的文件没有未映射的顶层 `type`。`toolResult` 上的 `image` 块没有 `text` 时不会变成事件。

## Copilot / Cursor / ChatGPT

Copilot 一条会话是 `~\.copilot\session-state\<uuid>\events.jsonl`。用户是 `user.message` 的 `data.content`，助手是 `assistant.message` 的 `data.content`。22 条 unknown 是 `abort`、`system.notification`、`system.message`、`session.error`、`session.truncation`。压缩摘要在 `data.summaryContent`，适配器只读 `data.summary`。`reasoningText` 不单列。

Cursor 是 `projects\<项目>\agent-transcripts\<uuid>\<uuid>.jsonl`。没有 `type`，正文在 `message.content[].text`。适配器把整个 `content` 列表转成字符串，不抽 `text`。`tool_use` 不是独立事件。

ChatGPT 本机没有 `conversations.json` 或 `chat.html`。适配器只读 AgentsView 形态的 sqlite。

## Mimo / OpenCode

两边都是 sqlite 的 `session` / `message` / `part`。`message.data` 没有 `content`。正文在 `part.data`，`type=text` 的 `text`，继承父消息的 role。

`opencode.db.backup` 还在。5 个只有会话头的会话是库里 message=0、part=0 的空行。`patch` 73 条和 `subtask` 1 条是 74 条 unknown；`subtask.prompt` 有正文但没读。推理密文在 `metadata.openai.reasoningEncryptedContent`，适配器只读 `text`。

Mimo 的 `mimocode.db` 按现有类型表 unknown 为 0。`history_fts` 不读。
