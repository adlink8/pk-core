#requires -Version 7.0
# FROZEN 2026-08-11: 本脚本为薄包装,本身不含 cockpit 托管逻辑(无 serve dist / /app / npm
# 构建);cockpit(web)的唯一入口是 REST 进程内的 /app 路由与 /ui/* 投影接口,已在
# src/personal_knowledge/services/api_server.py 的 do_GET 中停用。详见
# apps/personal_decision_cockpit/FROZEN.md。本脚本保留 REST + MCP + Tunnel + pi-kernel 核心服务逻辑。
<#! Thin compatibility wrapper. Canonical implementation: ops/runtime/start-agent-stack.ps1 #>
[CmdletBinding()]
param(
  [ValidateSet('Run','Check','Probe','Stop','Status')][string]$Mode = 'Run',
  [switch]$CheckOnly,
  [int]$HealthIntervalSeconds = 5,
  [int]$MaxRestarts = 3,
  [int]$StartTimeoutSeconds = 30,
  [string]$TunnelProxy = '',
  [string]$TunnelDirectory = '',
  [string]$TunnelProfile = 'personal-data-app',
  [switch]$SkipTunnel,
  [switch]$DryRun,
  [int]$RunForSeconds = 0,
  [switch]$PauseOnExit
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$canonical = Join-Path $projectRoot 'ops\runtime\start-agent-stack.ps1'
$arguments = @(
  '-NoProfile', '-File', $canonical,
  '-Mode', $(if ($CheckOnly) {'Check'} else {$Mode}),
  '-ProjectRoot', $projectRoot,
  '-HealthIntervalSeconds', [string]$HealthIntervalSeconds,
  '-MaxRestarts', [string]$MaxRestarts,
  '-StartTimeoutSeconds', [string]$StartTimeoutSeconds,
  '-RunForSeconds', [string]$RunForSeconds,
  '-TunnelProxy', $TunnelProxy,
  '-TunnelProfile', $TunnelProfile
)
if ($TunnelDirectory) { $arguments += @('-TunnelDirectory', $TunnelDirectory) }
if ($SkipTunnel) { $arguments += '-SkipTunnel' }
if ($DryRun) { $arguments += '-DryRun' }
if ($PauseOnExit) { $arguments += '-PauseOnExit' }
& pwsh @arguments
exit $LASTEXITCODE
