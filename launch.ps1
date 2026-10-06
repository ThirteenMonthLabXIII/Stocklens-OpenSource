$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot
$host.UI.RawUI.WindowTitle = 'A股选股仪 开发版启动器'

$legacyFrontendVariable = 'STOCK_TOOL_' + 'CLASSIC_UI'
$legacyFrontendArgument = '--' + 'classic'
$legacyFrontendOptions = @('-' + 'Frontend', '-' + 'Classic', $legacyFrontendArgument)
$legacyFrontendValue = [Environment]::GetEnvironmentVariable($legacyFrontendVariable)
if (($legacyFrontendValue -and $legacyFrontendValue.Trim() -eq '1') -or ($args | Where-Object { $legacyFrontendOptions -contains $_ })) {
    Write-Error ('经典' + '前端已移除，请使用 Qt 工作台。')
    exit 2
}

# A launcher run must never inherit probe or fault-injection state from a test shell.
foreach ($name in @(
    'STOCK_TOOL_STARTUP_PROBE',
    'STOCK_TOOL_STARTUP_PROBE_FILE',
    'STOCK_TOOL_STARTUP_PROBE_MODE',
    'STOCK_TOOL_TEST_MODE',
    'STOCK_TOOL_QT_FAULT_INJECTION',
    'STOCK_TOOL_TEST_FALLBACK_CHOICE',
    'STOCK_TOOL_' + 'QT_WORKBENCH'
)) {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}

Write-Host '========================================'
Write-Host 'A股选股仪 开发版启动器'
Write-Host '========================================'
Write-Host "当前目录：$PSScriptRoot"
Write-Host ''
Write-Host '正在检查 Python 环境...'
Write-Host ''

$py = $null; $args3 = @()
if (Get-Command python -ErrorAction SilentlyContinue) { $py = 'python'; $args3 = @('main.py') }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = 'py'; $args3 = @('-3','main.py') }

if ($py) {
    & $py --version
    Write-Host ''
    Write-Host '正在启动软件...'
    & $py @args3
} else {
    Write-Host '没有找到 Python。'
    Write-Host '请先安装 Python 3.10 或以上版本。'
    Write-Host '如果只是测试成品软件，请使用 商业出售版_不含源码 文件夹里的 A股选股仪.exe。'
}

Write-Host ''
Write-Host '如果窗口没有正常打开，常见原因是：'
Write-Host '1. 还没有完整解压源码包。'
Write-Host '2. 还没有安装 Python。'
Write-Host '3. 还没有先双击 安装依赖.bat。'
Write-Host ''
Read-Host '按回车键关闭本窗口'
