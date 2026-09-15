# Phase 35 运行时生产就绪验收报告

**验收时间：** 2026-07-19  
**目标：** `ops/runtime/start-agent-stack.ps1` 与本地 REST/MCP/tunnel Agent 链路  
**结论：** **PASS（外部 ChatGPT 设置页保留人工可见性复核）**

## 结论摘要

- 生产脚本审计：Critical 0、High 0、Medium 0、Low 0。
- 完整栈真实启动：REST `8000`、MCP `8789`、tunnel health `8081` 均 ready；doctor、OAuth protected-resource metadata 和 tunnel UI 均通过。
- MCP 契约：协议 `2025-06-18`，44 个工具，descriptor SHA-256 `42920a097e3073791634cf8af006e9eb35b07bbcdba53541c21b04c553b42706` 与评审快照完全一致。
- 真实 Agent 流：`analysis.list → analysis.explain → session.prepare → session.confirm → exact replay` 完成；replay 返回同一事件。
- 权威边界：Personal、External、Analysis、Pilot、Calibration 五库指纹不变；仅 orchestration 追加 1 session、1 event、1 confirmation；provider 调用、外部动作、自动 promotion 均为 0。

## 生产检查表

| 检查项 | 结果 | 证据 |
|---|---|---|
| PowerShell 7 参数与模式约束 | PASS | Run/Check/Probe/Stop/Status，参数有界 |
| Check/DryRun 零写入 | PASS | subprocess 测试 |
| 进程所有权与安全停止 | PASS | 仅登记 owned PID；端口冲突不杀 owner |
| 启动顺序和真实 readiness | PASS | REST → MCP → tunnel；三端口实测 |
| 有界恢复 | PASS | 指数退避、最大重试预算、独立 tunnel 90 秒窗口 |
| secret 处理 | PASS | HMAC 仅进程内生成；控制面 key 只检查存在性 |
| 日志和状态 | PASS | JSONL 结构化日志、轮转、owned PID 状态 |
| MCP/OAuth/tunnel | PASS | `/mcp`、metadata、`/healthz`、`/ui` |
| Descriptor 漂移 | PASS | 44-tool 快照 hash 相等 |
| 显式确认与幂等 replay | PASS | 同 event、`replayed=true` |
| 未授权副作用 | PASS | 五权威指纹不变，external_actions=0 |

## 执行与结果

```powershell
python C:\Users\li\.codex\skills\production-script-hardening\scripts\audit_script.py --script ops/runtime/start-agent-stack.ps1 --out-dir ops/reports/audits --format both --runtime-verified
# verdict=PASS; critical/high/medium/low = 0/0/0/0

python -m pytest tests/unit/test_privacy_guard.py tests/unit/test_orchestration_core.py tests/integration/test_orchestration_flow.py tests/integration/test_orchestration_replay.py tests/contract/test_orchestration_interfaces.py tests/e2e/test_orchestration_acceptance.py tests/e2e/test_agent_ux_evals.py tests/ops/test_agent_stack_script.py tests/security -q
# 67 passed

cd apps/personal_data_chatgpt; npm test
# 23 passed
```

运行时证据由以下命令生成：

```powershell
python ops/runtime/smoke-agent-stack.py --snapshot apps/personal_data_chatgpt/contracts/tool-descriptors.snapshot.json --out ops/reports/evidence/plan35-final-mcp-smoke.json
python ops/runtime/live-agent-acceptance.py --out ops/reports/evidence/plan35-live-agent-acceptance.json
```

## 已修复的运行时缺陷

1. tunnel 慢握手使用独立 90 秒有界 readiness，避免复用业务服务的过短窗口。
2. 子进程在启动前登记所有权，首次 readiness 超时也会在 `finally` 清理。
3. 子进程继承监督器输出句柄，消除未消费重定向管道造成的死锁风险。
4. Python/Node 隐私封存不再把结构化 ID/hash/checksum 中的数字串误判为电话并破坏签名。
5. Preview 对整数型浮点做 JSON 跨语言稳定规范化，`1.0 → 1` 不再导致 checksum 漂移。

## 剩余边界

- 已确认 Chrome 中 ChatGPT 登录态存在，connector URL 被当前 ChatGPT UI 重定向到 `#settings/Plugins`；该设置页在 Chrome 控制扩展下 DOM/可见树读取连续超时，因此没有取得新的 UI 内工具调用 transcript。
- 这不影响已验证的真实 HTTPS tunnel、HTTP MCP、descriptor、read/explain、confirmed replay 和权威指纹证据。发布到公共应用、账号权限变更和自动外部执行仍不在本里程碑范围。

