# Register-NativeSync.ps1 - 定时同步 AI 客户端会话 (Phase 62 native discovery seam)
# 用法:
#   pwsh -File tools\register-native-sync.ps1            # 注册每日 23:00 定时任务
#   pwsh -File tools\register-native-sync.ps1 -Unregister # 删除任务
#   pwsh -File tools\register-native-sync.ps1 -RunNow     # 立即执行一次
#
# 任务执行: pk-sync conversations --v2-native
#   = discover 客户端目录 -> stage 新/变更文件 -> NON-active shadow (metadata-only)
#   人工激活入口已退役（--v2-activate/--v2-approval/--v2-families）；门禁通过后由自动合入发布。
param(
    [switch]$Unregister,
    [switch]$RunNow,
    [string]$TaskName = "pk-native-conversation-sync",
    [string]$At = "23:00"
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[ok] removed scheduled task: $TaskName"
    exit 0
}

$inner = "python -m personal_knowledge.application.sync conversations --v2-native"
$log = Join-Path $repoRoot "var\logs\native-sync.log"
$cmd = "cd /d `"$repoRoot`" && set PYTHONPATH=$repoRoot\src && $inner"

if ($RunNow) {
    Write-Host "[run] $cmd"
    cmd /c $cmd 2>&1
    exit $LASTEXITCODE
}

$trigger = New-ScheduledTaskTrigger -Daily -At $At
# 任务动作用 pwsh 7：powershell.exe 5.1 不认 cmd 的 cd /d、set、&&，且 UTF-8 无 BOM 中文注释会按 GBK 解析炸掉；
# $cmd 里的 cmd 语法仅保留给 -RunNow 用。
$pwshPath = (Get-Command pwsh -ErrorAction Stop).Source
$psCmd = "Set-Location -LiteralPath '$repoRoot'; " + `
    "`$env:PYTHONPATH='$repoRoot\src'; $inner 2>&1 | Out-File -FilePath '$log' -Append -Encoding utf8; exit `$LASTEXITCODE"
$psAction = New-ScheduledTaskAction -Execute $pwshPath -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$psCmd`"" -WorkingDirectory $repoRoot
Register-ScheduledTask -TaskName $TaskName -Action $psAction -Trigger $trigger -Description "Phase 62 native client-directory conversation sync (discover->stage->shadow, never activates)" -Force | Out-Null
Write-Host "[ok] registered scheduled task: $TaskName @ daily $At"
Write-Host "     $inner"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
