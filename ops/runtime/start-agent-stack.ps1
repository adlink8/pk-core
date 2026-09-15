#requires -Version 7.0
<#!
.SYNOPSIS
  Production-safe foreground supervisor for the local REST, ChatGPT MCP and tunnel stack.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [ValidateSet('Run', 'Check', 'Probe', 'Stop', 'Status')][string]$Mode = 'Run',
  [string]$ProjectRoot = '',
  [string]$TunnelDirectory = '',
  [string]$TunnelProfile = 'personal-data-app',
  [string]$TunnelProxy = '',
  [ValidateRange(1, 65535)][int]$RestPort = 8000,
  [ValidateRange(1, 65535)][int]$McpPort = 8789,
  [ValidateRange(1, 65535)][int]$KernelPort = 8790,
  [ValidateRange(1, 65535)][int]$TunnelHealthPort = 8081,
  [ValidateRange(1, 10)][int]$MaxRestarts = 3,
  [ValidateRange(2, 300)][int]$StartTimeoutSeconds = 30,
  [ValidateRange(2, 300)][int]$TunnelStartTimeoutSeconds = 90,
  [ValidateRange(1, 60)][int]$HealthIntervalSeconds = 5,
  [ValidateRange(0, 86400)][int]$RunForSeconds = 0,
  [switch]$SkipTunnel,
  [switch]$DryRun,
  [switch]$PauseOnExit
)

$ErrorActionPreference = 'Stop'
$scriptPath = $MyInvocation.MyCommand.Path
$scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } elseif ($scriptPath) { Split-Path -Parent $scriptPath } else { (Get-Location).Path }
if (-not $ProjectRoot) { $ProjectRoot = Join-Path $scriptRoot '..\..' }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$appDir = Join-Path $ProjectRoot 'apps\personal_data_chatgpt'
$opsRoot = Join-Path $ProjectRoot 'ops'
$logDir = Join-Path $opsRoot 'logs'
$stateDir = Join-Path $opsRoot 'state'
$evidenceDir = Join-Path $opsRoot 'reports\evidence'
$logPath = Join-Path $logDir 'agent-stack.jsonl'
$statePath = Join-Path $stateDir 'agent-stack.json'
$MaxLogSizeBytes = 10MB
$loopbackHost = '127.0.0.1'
$loopbackScheme = 'http'
if (-not $TunnelDirectory) { $TunnelDirectory = Join-Path $env:USERPROFILE 'Desktop\tunnel-client' }
$TunnelDirectory = [IO.Path]::GetFullPath($TunnelDirectory)
$tunnelExe = Join-Path $TunnelDirectory 'tunnel-client.exe'
$profilePath = Join-Path $env:APPDATA "tunnel-client\$TunnelProfile.yaml"

function New-LocalUrl {
  param([int]$Port, [string]$Path)
  return ('{0}://{1}:{2}{3}' -f $loopbackScheme, $loopbackHost, $Port, $Path)
}

function Write-StructuredLog {
  param([string]$Level, [string]$Event, [string]$Target, [string]$Message)
  $entry = [ordered]@{
    timestamp = (Get-Date).ToUniversalTime().ToString('o')
    level = $Level; event = $Event; target = $Target; message = $Message
  }
  $line = $entry | ConvertTo-Json -Compress
  Write-Host $line
  if ((Test-Path -LiteralPath $logDir) -and -not $DryRun -and $Mode -eq 'Run') {
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -ge $MaxLogSizeBytes) {
      $archive = Join-Path $logDir ("agent-stack-{0}.jsonl" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
      Move-Item -LiteralPath $logPath -Destination $archive
    }
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
  }
}

function Get-Executable {
  param([string]$Name, [string]$Explicit = '')
  if ($Explicit -and (Test-Path -LiteralPath $Explicit)) { return (Resolve-Path -LiteralPath $Explicit).Path }
  $command = Get-Command $Name -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  return $null
}

function Test-Endpoint {
  param([string]$Url)
  try {
    $response = Invoke-WebRequest -Uri $Url -NoProxy -TimeoutSec 4 -ErrorAction Stop
    return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
  } catch { return $false }
}

function Test-PortListening {
  param([int]$Port)
  return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function New-ManagedProcess {
  param($Spec)
  $start = [Diagnostics.ProcessStartInfo]::new()
  $start.FileName = $Spec.FilePath
  foreach ($argument in $Spec.Arguments) { $null = $start.ArgumentList.Add([string]$argument) }
  $start.WorkingDirectory = $Spec.WorkDir
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  # Inherit the supervisor's output handles. Redirecting without continuously
  # draining both streams can deadlock a verbose child on a full pipe.
  $start.RedirectStandardOutput = $false
  $start.RedirectStandardError = $false
  foreach ($key in $Spec.Environment.Keys) { $start.Environment[$key] = [string]$Spec.Environment[$key] }
  $process = [Diagnostics.Process]::new()
  $process.StartInfo = $start
  if (-not $process.Start()) { throw "process_start_failed:$($Spec.Key)" }
  return $process
}

function Stop-OwnedProcess {
  param($Entry)
  if (-not $Entry.Process) { return }
  try {
    if (-not $Entry.Process.HasExited) {
      $Entry.Process.Kill($true)
      $null = $Entry.Process.WaitForExit(5000)
    }
  } catch { Write-StructuredLog 'WARN' 'cleanup_failed' $Entry.Key $_.Exception.Message }
  $Entry.Process = $null
}

function Wait-Ready {
  param($Entry, [int]$TimeoutSeconds = $StartTimeoutSeconds)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if ($Entry.Process -and $Entry.Process.HasExited) { return $false }
    if (Test-Endpoint $Entry.HealthUrl) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Save-State {
  param($Entries)
  if ($DryRun -or $Mode -ne 'Run') { return }
  $items = foreach ($entry in $Entries) {
    [ordered]@{
      service = $entry.Key
      pid = if ($entry.Process -and -not $entry.Process.HasExited) { $entry.Process.Id } else { $null }
      adopted = $entry.Adopted
      healthy = (Test-Endpoint $entry.HealthUrl)
      restarts = $entry.Restarts
      health_url = $entry.HealthUrl
    }
  }
  $temporary = "$statePath.tmp"
  @{ updated_at = (Get-Date).ToUniversalTime().ToString('o'); supervisor_pid = $PID; services = @($items) } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding utf8
  Move-Item -LiteralPath $temporary -Destination $statePath -Force
}

function StopManagedProcesses {
  if (-not (Test-Path -LiteralPath $statePath)) { throw 'managed_state_missing' }
  $saved = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
  $supervisor = Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.supervisor_pid)" -ErrorAction SilentlyContinue
  if ($supervisor) {
    if ($supervisor.CommandLine -notlike '*start-agent-stack.ps1*') { throw 'supervisor_ownership_mismatch' }
    if (-not $DryRun -and $PSCmdlet.ShouldProcess($saved.supervisor_pid, 'Stop owned Agent stack supervisor')) {
      Stop-Process -Id $saved.supervisor_pid -Force
      Start-Sleep -Milliseconds 500
    }
  }
  $patterns = @{ rest='personal_knowledge.services.api_server'; mcp='server.mjs'; 'pi-kernel'='personal_intelligence_kernel.*server.mjs'; tunnel='tunnel-client' }
  foreach ($service in $saved.services) {
    if ($service.adopted -or -not $service.pid) { continue }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($service.pid)" -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    if ($process.CommandLine -notlike "*$($patterns[$service.service])*" ) { throw "child_ownership_mismatch:$($service.service)" }
    if (-not $DryRun -and $PSCmdlet.ShouldProcess($service.pid, "Stop owned $($service.service) process")) {
      Stop-Process -Id $service.pid -Force
    }
  }
  Write-StructuredLog 'OK' 'managed_stop_complete' 'agent-stack' $(if ($DryRun) {'dry_run'} else {'stopped_owned_processes'})
}

function Show-ManagedStatus {
  $saved = if (Test-Path -LiteralPath $statePath) { Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json } else { $null }
  $status = [ordered]@{ state_path=$statePath; state_exists=[bool]$saved; supervisor_running=$false; services=@() }
  if ($saved) {
    $status.supervisor_running = [bool]($saved.supervisor_pid -and (Get-Process -Id $saved.supervisor_pid -ErrorAction SilentlyContinue))
    $status.services = @($saved.services | ForEach-Object {
      [ordered]@{ service=$_.service; pid=$_.pid; adopted=$_.adopted; process_running=[bool]($_.pid -and (Get-Process -Id $_.pid -ErrorAction SilentlyContinue)); healthy=(Test-Endpoint $_.health_url); health_url=$_.health_url }
    })
  }
  Write-Host ($status | ConvertTo-Json -Depth 5)
}

function Get-MissingRequiredSecrets {
  param([bool]$TunnelEnabled)
  $missing = @()
  if ($TunnelEnabled -and -not $env:CONTROL_PLANE_API_KEY) { $missing += 'CONTROL_PLANE_API_KEY_missing' }
  return $missing
}

function Invoke-Preflight {
  param([bool]$RequireSecret)
  $failures = [Collections.Generic.List[string]]::new()
  if (-not (Test-Path -LiteralPath $ProjectRoot)) { $failures.Add('project_root_missing') }
  if (-not (Test-Path -LiteralPath $appDir)) { $failures.Add('app_directory_missing') }
  if (@(@($RestPort, $McpPort, $KernelPort, $TunnelHealthPort) | Select-Object -Unique).Count -ne 4) { $failures.Add('ports_must_be_unique') }
  $python = Get-Executable 'python'
  $node = Get-Executable 'node'
  if (-not $python) { $failures.Add('python_missing') }
  if (-not $node) { $failures.Add('node_missing') }
  if (-not $SkipTunnel) {
    if (-not (Test-Path -LiteralPath $tunnelExe)) { $failures.Add('tunnel_executable_missing') }
    if (-not (Test-Path -LiteralPath $profilePath)) { $failures.Add('tunnel_profile_missing') }
    foreach ($missingSecret in (Get-MissingRequiredSecrets -TunnelEnabled:$RequireSecret)) { $failures.Add($missingSecret) }
    if ((Test-Path -LiteralPath $tunnelExe) -and (Test-Path -LiteralPath $profilePath)) {
      $profileList = & $tunnelExe profiles list 2>$null
      if ($LASTEXITCODE -ne 0 -or (($profileList -join "`n") -notmatch [regex]::Escape($TunnelProfile))) {
        $failures.Add('tunnel_profile_cli_validation_failed')
      }
    }
  }
  $restHealth = New-LocalUrl $RestPort '/health'
  $mcpHealth = New-LocalUrl $McpPort '/health'
  $kernelHealth = New-LocalUrl $KernelPort '/ready'
  # /healthz proves only that the daemon is alive. /readyz proves the control
  # plane and configured MCP channel are both ready to serve requests.
  $tunnelHealth = New-LocalUrl $TunnelHealthPort '/readyz'
  Write-StructuredLog 'INFO' 'config_loaded' 'agent-stack' (([ordered]@{
    project_root = $ProjectRoot; app_dir = $appDir; tunnel_profile = $TunnelProfile
    services = if ($SkipTunnel) { @('rest','pi-kernel','mcp') } else { @('rest','pi-kernel','mcp','tunnel') }
    health_urls = @($restHealth, $kernelHealth, $mcpHealth, $tunnelHealth)
    max_restarts = $MaxRestarts; required_environment = @{ CONTROL_PLANE_API_KEY_set = [bool]$env:CONTROL_PLANE_API_KEY; orchestration_hmac = 'generated_in_memory' }
  } | ConvertTo-Json -Compress))
  foreach ($failure in $failures) { Write-StructuredLog 'ERROR' 'preflight_failed' 'agent-stack' $failure }
  return @{ Ok = $failures.Count -eq 0; Python = $python; Node = $node; Failures = @($failures) }
}

$exitCode = 0
$entries = @()
if ($Mode -eq 'Status') { Show-ManagedStatus; exit 0 }
if ($Mode -eq 'Stop') {
  try { StopManagedProcesses; exit 0 } catch { Write-StructuredLog 'ERROR' 'managed_stop_failed' 'agent-stack' $_.Exception.Message; exit 2 }
}
try {
  $preflight = Invoke-Preflight -RequireSecret:(-not $SkipTunnel)
  if (-not $preflight.Ok) { throw "preflight_failed:$($preflight.Failures -join ',')" }

  $restHealth = New-LocalUrl $RestPort '/health'
  $mcpHealth = New-LocalUrl $McpPort '/health'
  $kernelHealth = New-LocalUrl $KernelPort '/ready'
  $tunnelHealth = New-LocalUrl $TunnelHealthPort '/readyz'
  $healthUrls = @($restHealth, $kernelHealth, $mcpHealth)
  if (-not $SkipTunnel) { $healthUrls += $tunnelHealth }
  if ($Mode -eq 'Probe') {
    $unhealthy = @($healthUrls | Where-Object { -not (Test-Endpoint $_) })
    if ($unhealthy.Count) { throw "readiness_failed:$($unhealthy -join ',')" }
    Write-StructuredLog 'OK' 'readiness_passed' 'agent-stack' 'all_enabled_services_healthy'
    return
  }
  if ($Mode -eq 'Check' -or $DryRun) {
    Write-StructuredLog 'OK' 'preflight_passed' 'agent-stack' 'zero_write_check_complete'
    return
  }

  foreach ($directory in @($logDir, $stateDir, $evidenceDir)) { $null = New-Item -ItemType Directory -Force -Path $directory }
  $secretBytes = [byte[]]::new(32)
  [Security.Cryptography.RandomNumberGenerator]::Fill($secretBytes)
  $orchestrationSecret = [Convert]::ToBase64String($secretBytes)
  $piKernelCapabilityBytes = [byte[]]::new(32)
  [Security.Cryptography.RandomNumberGenerator]::Fill($piKernelCapabilityBytes)
  $piKernelCapability = [Convert]::ToBase64String($piKernelCapabilityBytes)
  & $preflight.Python -c "from personal_knowledge.intelligence.orchestration import apply_schema; apply_schema(r'$ProjectRoot\var\db\decision_orchestration.sqlite')" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'orchestration_schema_provision_failed' }

  $specs = @(
    @{ Key='rest'; FilePath=$preflight.Python; Arguments=@('-m','personal_knowledge.services.api_server','--host',$loopbackHost,'--port',[string]$RestPort); WorkDir=$ProjectRoot; HealthUrl=$restHealth; Port=$RestPort; Environment=@{ PERSONAL_DATA_ORCHESTRATION_SECRET=$orchestrationSecret; PI_KERNEL_INTERNAL_CAPABILITY=$piKernelCapability; PI_KERNEL_URL=(New-LocalUrl $KernelPort ''); PI_KERNEL_AI_WORKFLOW='1' } },
    @{ Key='pi-kernel'; FilePath=$preflight.Node; Arguments=@('apps\personal_intelligence_kernel\src\server.mjs','--port',[string]$KernelPort); WorkDir=$ProjectRoot; HealthUrl=$kernelHealth; Port=$KernelPort; Environment=@{ PI_KERNEL_PORT=[string]$KernelPort; PI_KERNEL_INTERNAL_CAPABILITY=$piKernelCapability } },
    @{ Key='mcp'; FilePath=$preflight.Node; Arguments=@('server.mjs'); WorkDir=$appDir; HealthUrl=$mcpHealth; Port=$McpPort; Environment=@{ PORT=[string]$McpPort; PERSONAL_DATA_REST_URL=(New-LocalUrl $RestPort '') } }
  )
  if (-not $SkipTunnel) {
    $tunnelEnvironment = @{ NO_PROXY=($loopbackHost + ',localhost') }
    if ($TunnelProxy) { $tunnelEnvironment.HTTPS_PROXY=$TunnelProxy; $tunnelEnvironment.HTTP_PROXY=$TunnelProxy }
    $specs += @{ Key='tunnel'; FilePath=$tunnelExe; Arguments=@('run','--profile',$TunnelProfile); WorkDir=$TunnelDirectory; HealthUrl=$tunnelHealth; Port=$TunnelHealthPort; Environment=$tunnelEnvironment; StartTimeoutSeconds=$TunnelStartTimeoutSeconds }
  }

  foreach ($spec in $specs) {
    $entry = [pscustomobject]@{ Key=$spec.Key; Spec=$spec; HealthUrl=$spec.HealthUrl; Process=$null; Adopted=$false; Restarts=0; Failures=0 }
    # Register before starting so finally always owns and cleans up a process
    # that times out during its first readiness window.
    $entries += $entry
    if (Test-Endpoint $spec.HealthUrl) {
      $entry.Adopted = $true
      Write-StructuredLog 'OK' 'service_reused' $spec.Key $spec.HealthUrl
    } elseif (Test-PortListening $spec.Port) {
      throw "unhealthy_port_conflict:$($spec.Key):$($spec.Port)"
    } else {
      if ($spec.Key -eq 'tunnel') {
        & $tunnelExe doctor --profile $TunnelProfile --explain | ForEach-Object { Write-StructuredLog 'INFO' 'tunnel_doctor' 'tunnel' ([string]$_) }
        if ($LASTEXITCODE -ne 0) { throw 'tunnel_doctor_failed' }
      }
      if (-not $PSCmdlet.ShouldProcess($spec.Key, 'Start managed local service')) { throw "start_declined:$($spec.Key)" }
      $entry.Process = New-ManagedProcess $spec
      $readinessTimeout = if ($spec.ContainsKey('StartTimeoutSeconds')) { [int]$spec.StartTimeoutSeconds } else { $StartTimeoutSeconds }
      if (-not (Wait-Ready $entry $readinessTimeout)) { throw "startup_readiness_failed:$($spec.Key)" }
      Write-StructuredLog 'OK' 'service_ready' $spec.Key $spec.HealthUrl
    }
    Save-State $entries
  }

  Write-StructuredLog 'OK' 'stack_ready' 'agent-stack' 'REST_MCP_tunnel_ready'
  $startedAt = Get-Date
  while ($RunForSeconds -eq 0 -or ((Get-Date) - $startedAt).TotalSeconds -lt $RunForSeconds) {
    Start-Sleep -Seconds $HealthIntervalSeconds
    foreach ($entry in $entries) {
      if (Test-Endpoint $entry.HealthUrl) { $entry.Failures = 0; continue }
      if ($entry.Adopted) { throw "adopted_service_unhealthy:$($entry.Key)" }
      $entry.Failures++
      if ($entry.Failures -lt 2) { continue }
      if ($entry.Restarts -ge $MaxRestarts) { throw "restart_budget_exhausted:$($entry.Key)" }
      Stop-OwnedProcess $entry
      $entry.Restarts++
      $delay = [Math]::Min([Math]::Pow(2, $entry.Restarts - 1), 8)
      Write-StructuredLog 'WARN' 'service_restart' $entry.Key "attempt=$($entry.Restarts);delay_seconds=$delay"
      Start-Sleep -Seconds $delay
      $entry.Process = New-ManagedProcess $entry.Spec
      $entry.Failures = 0
      if (-not (Wait-Ready $entry)) { Write-StructuredLog 'WARN' 'restart_not_ready' $entry.Key $entry.HealthUrl }
      Save-State $entries
    }
  }
} catch {
  $exitCode = 2
  Write-StructuredLog 'ERROR' 'stack_failed' 'agent-stack' $_.Exception.Message
} finally {
  foreach ($entry in $entries) {
    if (-not $entry.Adopted) { Stop-OwnedProcess $entry }
  }
  if ($entries.Count) { Save-State $entries }
  Write-StructuredLog $(if ($exitCode) {'ERROR'} else {'OK'}) 'stack_stopped' 'agent-stack' "exit_code=$exitCode"
  if ($PauseOnExit -and [Environment]::UserInteractive) { $null = Read-Host 'Press Enter to close' }
}
exit $exitCode
