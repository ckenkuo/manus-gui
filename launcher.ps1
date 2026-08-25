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

# ---- 第 1 步：找到 Python（优先项目自带 .venv，依赖都在里面）----
# 2026-08-22 实测教训：系统里新装了 Python 3.14 后，`py -3` 会优先解析到它，
# 而 fastapi 只装在 .venv 里——依赖检查直接失败。故 .venv 存在时一律用它。
$py = $null
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $py = @($venvPy)
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
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
# 可执行文件与参数分开存：$py[1..($py.Length-1)] 在单元素时会变成 1..0
# （PowerShell 逆序区间），把 python.exe 路径当成脚本又传一遍（2026-08-22 实测踩中）
$pyExe = $py[0]
$pyArgs = @()
if ($py.Length -gt 1) { $pyArgs = $py[1..($py.Length-1)] }

# ---- 第 2 步：检查依赖，缺了就自动安装 ----
# 注意：PS5.1 里 $ErrorActionPreference='Stop' + 原生命令重定向 stderr（2>$null）
# 会把 stderr 当成 terminating error 直接炸掉脚本（双击表现为「闪退」），
# 所以这里局部放宽到 Continue 再判退出码（2026-08-22 实测）。
$depsOk = $false
$prevEAP = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $pyExe @pyArgs -c "import fastapi, uvicorn" 2>$null
    $depsOk = ($LASTEXITCODE -eq 0)
} catch { $depsOk = $false }
$ErrorActionPreference = $prevEAP
if (-not $depsOk) {
    Write-Host "[提示] 检测到依赖未安装，正在自动安装，第一次会比较久，请耐心等待..." -ForegroundColor Yellow
    Write-Host ""
    try {
        $ErrorActionPreference = 'Continue'
        & $pyExe @pyArgs -m pip install -r requirements.txt
        $installOk = ($LASTEXITCODE -eq 0)
    } catch { $installOk = $false }
    $ErrorActionPreference = $prevEAP
    if (-not $installOk) {
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
    # 端口活着还不够：得确认占端口的是「本项目」的 Chrome（用户目录对得上）。
    # 2026-08-21 实测：另一项目留下的 Chrome（--user-data-dir=C:\chrome-debug-ctrip）
    # 占了 9222，这里误以为可用直接复用——那个 profile 没登录店小秘，
    # 用户表现为「每次都要重新登录」（本项目的 C:\chrome-debug-manus 里登录态一直在）。
    $wrongProfile = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
        Where-Object { $_.CommandLine -match 'remote-debugging-port' -and
                       $_.CommandLine -notmatch [regex]::Escape($chromeProfile) } |
        Select-Object -First 1
    if ($wrongProfile) {
        Write-Host "[警告] 9222 端口上的调试 Chrome 用的不是本项目的用户目录：" -ForegroundColor Red
        Write-Host "       $($wrongProfile.CommandLine)" -ForegroundColor DarkGray
        Write-Host "       这个浏览器里没有店小秘登录态，采集/发布会一直要求重新登录。" -ForegroundColor Yellow
        Write-Host "       请把它完全关闭（所有窗口），再重新运行 启动.bat，" -ForegroundColor Yellow
        Write-Host "       启动器会用正确的用户目录 $chromeProfile 打开调试 Chrome。" -ForegroundColor Yellow
    } else {
        Write-Host "[采集浏览器] 调试 Chrome 已在运行（端口 $cdpPort），直接使用。" -ForegroundColor Green
    }
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

& $pyExe @pyArgs app.py

# ---- 程序退出后（正常或报错）都停在这里 ----
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "程序已停止。若是意外退出，请把上面的信息截图。" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Pause-Exit 0
