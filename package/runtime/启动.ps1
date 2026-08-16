# manus-gui 启动器 —— 首次运行会引导补齐配置，之后直接拉起 Web 界面
#
# 放在与 exe 平级的位置。程序侧（app/config.py 的 DATA_ROOT）在冻结态优先把
# exe 所在目录当可写数据根，所以 config/ 就在本脚本旁边，用户能直接看到和编辑。

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Here = $PSScriptRoot
Set-Location $Here

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " manus-gui" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$configPath = Join-Path $Here "config\config.toml"
$examplePath = Join-Path $Here "config\config.example.toml"

# ---------- 首次运行：生成可编辑的 config.toml ----------
if (-not (Test-Path $configPath)) {
    Write-Host "`n首次运行，正在创建配置文件..." -ForegroundColor Yellow

    if (-not (Test-Path $examplePath)) {
        Write-Host "缺少配置模板 config\config.example.toml，安装包可能不完整。" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }

    Copy-Item $examplePath $configPath
    Write-Host "已创建: $configPath" -ForegroundColor Green

    Write-Host "`n请在打开的记事本里至少填好这几项，保存后回到本窗口：" -ForegroundColor Yellow
    Write-Host "  [llm] 段" -ForegroundColor White
    Write-Host "    api_key   —— 模型 API 密钥（必填，不填无法调用模型）" -ForegroundColor Gray
    Write-Host "    base_url  —— API 地址" -ForegroundColor Gray
    Write-Host "    model     —— 模型名" -ForegroundColor Gray
    Write-Host "  [collect] 段 —— 采集目标（云端文档 ID，或留空走本地 Excel）" -ForegroundColor White

    Start-Process notepad.exe $configPath
    Write-Host ""
    Read-Host "配置保存好后按回车继续"
}

# ---------- 采集管线前置：调试端口 Chrome ----------
# 采集/订单/活动三条管线都靠 CDP 接管用户已登录的真实 Chrome（localhost:9222），
# 端口没开就一定连不上。这里只做提示不强制，因为纯 agent 对话用不到它。
$port = 9222
$portOpen = $false
try {
    $null = Invoke-WebRequest -Uri "http://localhost:$port/json/version" -UseBasicParsing -TimeoutSec 2
    $portOpen = $true
} catch { }

if ($portOpen) {
    Write-Host "`n调试 Chrome 已就绪（端口 $port），采集管线可用。" -ForegroundColor Green
} else {
    Write-Host "`n提示：调试端口 $port 未开启，采集/订单/活动管线暂时无法连接浏览器。" -ForegroundColor Yellow
    $ans = Read-Host "现在启动调试 Chrome？(y/n)"
    if ($ans -eq 'y' -or $ans -eq 'Y') {
        & (Join-Path $Here "启动调试Chrome.ps1")
        Write-Host ""
    }
}

# ---------- 内置浏览器（可选） ----------
# 只有勾了 -IncludeBrowsers 打的包才有这个目录；采集管线不依赖它。
$pwDir = Join-Path $Here "playwright_browsers"
if (Test-Path $pwDir) {
    $env:PLAYWRIGHT_BROWSERS_PATH = $pwDir
}

# ---------- 拉起 Web 界面 ----------
Write-Host "`n正在启动 Web 界面..." -ForegroundColor Green
Write-Host "浏览器会自动打开；按 Ctrl+C 停止服务。`n" -ForegroundColor Yellow

& (Join-Path $Here "manus-web.exe")
