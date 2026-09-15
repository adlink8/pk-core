# Proactive intelligence runbook

Phase 27 的主动情报是非 serving 的 `a.proactive_intelligence` 分析层。它只预计算可读取的 inbox/digest 候选，不发送通知、不创建日历或任务、不执行命令，也不调用 connector、网络或付费服务。

## 语义与读接口

固定八域：`learning`、`career`、`project`、`health`、`finance`、`relationship`、`time`、`energy`。冲突必须绑定兼容的资源、单位和时间窗；否则以 reason-coded abstention 结束。

CLI、REST、MCP 共享同一个 checksum 校验服务，提供：inbox、digest、candidate get、explain、controls status 和 metrics。返回仅含 snapshot/run/policy/control-frontier/checksum、domain、reason、uncertainty 和 evidence-status 元数据。读取不会隐式记录 `presented`。

常见抑制原因包括 privacy/evidence veto、dedup、cooldown、quiet period、global/domain budget 和用户 trust veto。重要性不会覆盖敏感信息或用户抑制。

## 权限边界

REST 与 MCP 严格只读，不存在 control write、surface write、notify、send、schedule、execute 或 dispatch 路由/工具。只有本地 CLI 能显式追加控制或 `presented/acknowledged/dismissed` 事件，且同时要求：

1. `--write`
2. `--i-confirm <candidate-id>` 精确匹配
3. `--actor-class user` 与 64 位 identity hash
4. `--expected-sequence`
5. 非空 `--idempotency-key`

控制和 surface 历史都是 append-only。`restore` 是引用旧事件的补偿事件，不擦除历史；过期/并发序列和 checksum 漂移均 fail closed。`correct` 只创建 canonical correction request，真正 KU 修正必须走 Phase 24 人工审核 lifecycle 流程。

## Target D 验收

live 环境只允许：

```powershell
python -m personal_knowledge.intelligence.proactive.cli acceptance --dry-run --metadata-only --json
```

schema 只有三种合法解释：全部表不存在为 `unapplied`（metadata-only 可接受）、全部存在且校验通过为 `applied`、部分存在为 `partial`（技术失败）。验收不得迁移、持久化分析/控制数据、发布、激活、推进 watermark、完成评审、应用 lifecycle 或触发外部动作。

`technical_status=passed` 只证明 fixture/sandbox 与 live 零副作用合同。`release_ready` 还必须同时满足 Phase 24 真实 Gold/Judge/UAT、human review strict PASS、lifecycle strict PASS、final gate PASS 和显式 product UAT。当前这些条件未满足，因此产品状态必须保持 `release_blocked`；fixture 不能被解释为真实个人效用或产品采用。
