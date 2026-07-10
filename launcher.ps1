# manus-gui 启动器（由 启动.bat 调用，普通用户无需直接打开本文件）
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# 切换到脚本所在目录（双击时工作目录可能不对）
Set-Location -Path $PSScriptRoot

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "           manus-gui  正在启动" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

function Pause-Exit([int]$code) {
    Write-Host ""
    Write-Host "按回车键关闭窗口..." -ForegroundColor DarkGray
    [void][System.Console]::ReadLine()
    exit $code
}

# ---- 第 1 步：找到 Python ----
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $py = @('py', '-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = @('python')
}
if (-not $py) {
    Write-Host "[错误] 没有找到 Python，请先安装 Python 3.11 或更高版本。" -ForegroundColor Red
    Write-Host "       下载地址：https://www.python.org/downloads/"
    Write-Host '       安装时请勾选 "Add Python to PATH"。'
    Pause-Exit 1
}

# ---- 第 2 步：检查依赖，缺了就自动安装 ----
& $py[0] $py[1..($py.Length-1)] -c "import fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[提示] 检测到依赖未安装，正在自动安装，第一次会比较久，请耐心等待..." -ForegroundColor Yellow
    Write-Host ""
    & $py[0] $py[1..($py.Length-1)] -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[错误] 依赖安装失败，请把上面的红字截图发给技术人员。" -ForegroundColor Red
        Pause-Exit 1
    }
}

# ---- 第 3 步：打开采集用的调试 Chrome（只在没开的时候开）----
# 采集功能需要接管一个带调试端口、且已登录 Temu/1688 的 Chrome。
# 用独立用户目录，登录状态会一直保存，普通用户只需首次手动登录一次。
$cdpPort = 9222
$chromeProfile = "C:\chrome-debug-manus"
$chromeExe = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

# 检查 9222 是否已经在监听（已开过就不再重复开）
$cdpAlive = $false
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $iar = $tcp.BeginConnect('127.0.0.1', $cdpPort, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(800)) { $tcp.EndConnect($iar); $cdpAlive = $true }
    $tcp.Close()
} catch {}

if ($cdpAlive) {
    Write-Host "[采集浏览器] 调试 Chrome 已在运行（端口 $cdpPort），直接使用。" -ForegroundColor Green
} elseif ($chromeExe) {
    Write-Host "[采集浏览器] 正在打开调试 Chrome ..." -ForegroundColor Green
    Start-Process -FilePath $chromeExe -ArgumentList `
        "--remote-debugging-port=$cdpPort", "--user-data-dir=$chromeProfile"
    Write-Host "  如果是第一次用，请在弹出的这个 Chrome 里手动登录 Temu / 1688 一次。" -ForegroundColor Yellow
    Write-Host "  （登录后不用每次都登，下次会记住）"
} else {
    Write-Host "[提示] 没找到 Chrome，采集功能需要 Chrome。普通网页操作不受影响。" -ForegroundColor Yellow
    Write-Host "       Chrome 下载地址：https://www.google.cn/chrome/"
}
Write-Host ""

# ---- 第 4 步：启动服务 ----
Write-Host ""
Write-Host "启动中... 稍等几秒会自动打开浏览器。" -ForegroundColor Green
Write-Host "如果没自动打开，请手动访问：http://localhost:5172"
Write-Host ""
Write-Host "！！！这个窗口不要关，关了程序就停了！！！" -ForegroundColor Yellow
Write-Host "（用完后关闭这个窗口即可退出程序）"
Write-Host ""

& $py[0] $py[1..($py.Length-1)] app.py

# ---- 程序退出后（正常或报错）都停在这里 ----
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "程序已停止。若是意外退出，请把上面的信息截图。" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Pause-Exit 0
