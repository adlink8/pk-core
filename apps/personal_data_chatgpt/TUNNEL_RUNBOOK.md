# Personal Data ChatGPT Tunnel Runbook

This runbook connects the personal decision-intelligence MCP Apps server to ChatGPT through OpenAI Secure MCP Tunnel. It exposes checksum-verified reads plus explicitly confirmed, local, append-only low-risk orchestration tools.

## Prerequisites

- REST API running at `http://127.0.0.1:8000`.
- Apps MCP server running at `http://127.0.0.1:8789/mcp`.
- `CONTROL_PLANE_API_KEY` available in the local environment.
- A tunnel id created in the OpenAI Platform tunnel settings.
- ChatGPT account/workspace with connector or Apps SDK developer access.

Do not write API keys or tunnel secrets into repo files. Keep credentials in the environment or a secure credential store.

## Start And Check The Full Stack

From the project root:

```powershell
pwsh -NoProfile -File ops\runtime\start-agent-stack.ps1 -Mode Check
pwsh -NoProfile -File ops\runtime\start-agent-stack.ps1 -Mode Run -TunnelProxy http://127.0.0.1:7897
```

The supervisor starts REST → MCP → tunnel in order, waits for real health, restarts with a bounded budget, records owned PIDs, and only stops processes it can prove it owns. Use a proxy argument only when the local environment requires it.

Status, probe and safe stop:

```powershell
pwsh -NoProfile -File ops\runtime\start-agent-stack.ps1 -Mode Status
pwsh -NoProfile -File ops\runtime\start-agent-stack.ps1 -Mode Probe
pwsh -NoProfile -File ops\runtime\start-agent-stack.ps1 -Mode Stop
```

Verify:

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8000/health
curl.exe --noproxy "*" http://127.0.0.1:8789/health
```

## Create A Separate Tunnel Profile

Do not modify the existing `codexpro` profile. Create a dedicated profile:

```powershell
cd C:\Users\li\Desktop\tunnel-client

.\tunnel-client.exe init `
  --profile personal-data-app `
  --tunnel-id <tunnel_id> `
  --mcp-server-url http://127.0.0.1:8789/mcp `
  --health-listen-addr 127.0.0.1:8081
```

Check the generated file before running:

```powershell
Get-Content "$env:APPDATA\tunnel-client\personal-data-app.yaml"
```

It should reference:

- `api_key: "env:CONTROL_PLANE_API_KEY"`
- `listen_addr: "127.0.0.1:8081"`
- `url: "http://127.0.0.1:8789/mcp"`

## Validate And Run The Tunnel

```powershell
cd C:\Users\li\Desktop\tunnel-client

.\tunnel-client.exe doctor --profile personal-data-app --explain
.\tunnel-client.exe run --profile personal-data-app
```

Expected local checks:

```powershell
curl.exe --noproxy "*" http://127.0.0.1:8081/healthz
curl.exe --noproxy "*" http://127.0.0.1:8081/readyz
curl.exe --noproxy "*" http://127.0.0.1:8081/ui
```

## ChatGPT Connector Setup

In ChatGPT:

1. Open connector / app developer settings.
2. Create or edit a local MCP connector.
3. Choose the tunnel connection option.
4. Select or paste the tunnel id used in `personal-data-app`.
5. Refresh the connector after tool descriptor or widget resource changes.

Manual UAT prompts:

- `打开我的长期记忆图谱，显示 LLM 判断边。`
- `查看 Codex 相关的记忆和 2 跳邻居。`
- `列出需要人工审查的长期记忆关系。`
- `用我的历史数据搜索最近关于 MCP 或 Apps SDK 的记录。`

## Local MCP Probe

Without ChatGPT, verify the app server directly:

```powershell
$body = @{jsonrpc='2.0'; id=1; method='tools/list'; params=@{}} | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri 'http://127.0.0.1:8789/mcp' -Method Post -ContentType 'application/json' -Body $body -NoProxy
```

Reviewed descriptor and live Agent acceptance:

```powershell
node apps\personal_data_chatgpt\scripts\descriptor-snapshot.mjs --check
python ops\runtime\smoke-agent-stack.py --snapshot apps\personal_data_chatgpt\contracts\tool-descriptors.snapshot.json --out ops\reports\evidence\agent-stack-smoke.json
python ops\runtime\live-agent-acceptance.py --out ops\reports\evidence\live-agent-acceptance.json
```

Graph tool call:

```powershell
$body = @{
  jsonrpc='2.0'
  id=2
  method='tools/call'
  params=@{
    name='show_memory_graph'
    arguments=@{include_llm=$true; limit=200}
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri 'http://127.0.0.1:8789/mcp' -Method Post -ContentType 'application/json' -Body $body -NoProxy
```

## Safety Boundary

- Personal, External, Analysis, Pilot and Calibration reads are checksum-verifying and read-only.
- Orchestration mutations require `confirmed=true`, an exact preview checksum and an idempotency key; the HMAC capability is minted and consumed only inside the REST process.
- Confirmed writes are limited to the local append-only orchestration authority. They do not write Personal/External authorities, perform external actions or auto-promote calibration proposals.
- It does not write `memory_items`, `memory_links`, or `memory_relations`.
- It does not expose the REST API publicly.
- It should not be run with raw unsafe HTTP logging enabled.
