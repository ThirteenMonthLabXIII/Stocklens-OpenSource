[CmdletBinding()]
param(
    [string]$ExePath = "",
    [string]$SourceMainPath = "",
    [string]$TaskName = "StockSelectorTool Review Automation",
    [switch]$DryRun,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    if ($DryRun) {
        Write-Output "DRY-RUN: unregister scheduled task '$TaskName'"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "已删除任务：$TaskName"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($ExePath) -eq ([string]::IsNullOrWhiteSpace($SourceMainPath))) {
    throw "请二选一指定 -ExePath 或 -SourceMainPath。"
}

if ($ExePath) {
    $resolvedTarget = (Resolve-Path -LiteralPath $ExePath).Path
    $action = New-ScheduledTaskAction -Execute $resolvedTarget
    $targetText = $resolvedTarget
} else {
    $resolvedTarget = (Resolve-Path -LiteralPath $SourceMainPath).Path
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        throw "找不到 python.exe；请先激活运行环境或直接使用 -ExePath。"
    }
    $action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}"' -f $resolvedTarget)
    $targetText = "$python $resolvedTarget"
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -RandomDelay (New-TimeSpan -Seconds 30)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

if ($DryRun) {
    Write-Output "DRY-RUN: register scheduled task '$TaskName'"
    Write-Output "Target: $targetText"
    Write-Output "Trigger: AtLogOn + 30 seconds"
    Write-Output "MultipleInstances: IgnoreNew"
    Write-Output "Restart: 3 times, every 5 minutes"
    exit 0
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "登录后启动 StockSelectorTool，使本地 Qt 自动复盘能够在用户会话中补做遗漏。" `
    -Force | Out-Null
Write-Output "已注册任务：$TaskName"
Write-Output "启动目标：$targetText"
