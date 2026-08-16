# 以远程调试端口启动 Chrome —— 采集/订单/活动三条管线的前置条件
#
# 管线全部通过 CDP 接管「用户已登录的真实 Chrome」（见 app/collect/service.py 的
# connect_over_cdp），而不是自己 launch 一个干净浏览器，这样才能绕过平台风控、
# 复用已有登录态。因此运行管线前必须让 Chrome 带 --remote-debugging-port=9222 跑起来。
#
# 两个必须踩准的点：
# 1) 复用默认用户数据目录。若用独立的 --user-data-dir，开出来的是全新 profile，
#    没有任何登录态，管线会被平台踢回登录页。
# 2) 必须先完全退出已在运行的 Chrome。Chrome 同一 profile 只有首个进程能持有调试端口；
#    已有实例在跑时，再次带参数启动只会把请求转交给老进程、参数被静默丢弃，
#    端口根本不会打开——这种「看起来启动了其实没开端口」最容易误判。

param(
    [int]$Port = 9222,
    [switch]$Force   # 直接结束已运行的 Chrome，不再交互询问
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 启动调试模式 Chrome（端口 $Port）" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. 定位 Chrome
$candidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $chrome) {
    # 注册表兜底：非标准安装位置
    try {
        $reg = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" -ErrorAction Stop
        if ($reg -and (Test-Path $reg.'(default)')) { $chrome = $reg.'(default)' }
    } catch { }
}

if (-not $chrome) {
    Write-Host "未找到 Chrome。请先安装 Google Chrome 后重试。" -ForegroundColor Red
    Write-Host "下载地址: https://www.google.cn/chrome/" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}
Write-Host "Chrome 路径: $chrome" -ForegroundColor Green

# 2. 端口已经在监听？那说明调试 Chrome 已就绪，直接复用，避免重复开实例
$listening = $false
try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    if ($conn) { $listening = $true }
} catch {
    # 老系统没有 Get-NetTCPConnection，退回 netstat 文本匹配
    $listening = [bool]((netstat -ano -p TCP | Select-String ":$Port\s" | Select-String "LISTENING"))
}

if ($listening) {
    Write-Host "`n端口 $Port 已在监听，调试 Chrome 已就绪，无需重复启动。" -ForegroundColor Green
    Write-Host "可以直接运行采集管线了。" -ForegroundColor Green
    exit 0
}

# 3. 有 Chrome 在跑但端口没开 —— 必须先退干净，否则参数会被丢弃
$running = Get-Process chrome -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "`n检测到 Chrome 正在运行（$($running.Count) 个进程），但调试端口未开启。" -ForegroundColor Yellow
    Write-Host "同一 profile 下只有首个 Chrome 进程能持有调试端口，" -ForegroundColor Yellow
    Write-Host "必须先完全退出 Chrome，否则端口不会打开。" -ForegroundColor Yellow

    if (-not $Force) {
        Write-Host "`n请先手动保存正在编辑的网页内容。" -ForegroundColor Yellow
        $ans = Read-Host "现在关闭所有 Chrome 窗口并以调试模式重启？(y/n)"
        if ($ans -ne 'y' -and $ans -ne 'Y') {
            Write-Host "已取消。采集管线在调试端口开启前无法运行。" -ForegroundColor Yellow
            exit 1
        }
    }

    Write-Host "正在关闭 Chrome..." -ForegroundColor Yellow
    # 先温和关窗，给 Chrome 机会保存会话；不奏效再强制
    $running | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 3
    $still = Get-Process chrome -ErrorAction SilentlyContinue
    if ($still) {
        Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

# 4. 用默认 profile 启动（关键：不传 --user-data-dir，否则登录态全丢）
Write-Host "`n正在以调试模式启动 Chrome..." -ForegroundColor Yellow
Start-Process -FilePath $chrome -ArgumentList "--remote-debugging-port=$Port"

# 5. 轮询确认端口真的开了 —— 不确认就报成功是本项目踩过的坑
Write-Host "等待调试端口就绪..." -ForegroundColor Yellow
$ok = $false
foreach ($i in 1..20) {
    Start-Sleep -Milliseconds 500
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}

Write-Host ""
if ($ok) {
    Write-Host "========================================" -ForegroundColor Green
    Write-Host " 调试端口 $Port 已就绪" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "`n下一步：" -ForegroundColor Cyan
    Write-Host "  1. 在这个 Chrome 里登录 Temu 卖家后台 / 1688" -ForegroundColor White
    Write-Host "  2. 保持该窗口开着，然后运行采集管线" -ForegroundColor White
    Write-Host "`n注意：采集期间不要关闭这个 Chrome。" -ForegroundColor Yellow
} else {
    Write-Host "调试端口未能就绪。" -ForegroundColor Red
    Write-Host "常见原因：仍有残留 Chrome 进程占用同一 profile。" -ForegroundColor Yellow
    Write-Host "请在任务管理器中结束全部 chrome.exe 后重试，或加 -Force 参数重跑。" -ForegroundColor Yellow
    exit 1
}
