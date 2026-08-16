# manus-gui 打包脚本
#
# 产出一个自包含目录：三个 exe + 依赖 + 只读资源 + 用户可编辑的 config/，
# 目标是拷到新电脑解压后能直接跑采集管线。
#
# 为什么构建输出默认落在 C:\manus-build 而不是项目目录：
# 本机装有会改写新建 exe 的安全软件——凡写进桌面项目树的可执行文件都会被重写成
# GUI 子系统、注入伪造版本资源，严重时直接剥掉追加在 PE 尾部的 PKG 载荷，
# 而整个过程 PyInstaller 一声不吭、退出码依然是 0，只在运行时表现为「进程秒退、
# 零输出」。实测 C:\ 下的独立目录不受影响，故把输出隔离过去；
# 构建完成后再机检一遍（verify_build.py），不通过就中止，绝不让坏产物流出去。

param(
    [string]$BuildRoot = "C:\manus-build",
    [switch]$Clean,
    [switch]$IncludeBrowsers,   # 额外内置 Playwright 浏览器（约 520MB，采集管线用不到）
    [switch]$SkipZip,
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$PackageDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $PackageDir
$DistDir = Join-Path $BuildRoot "dist\manus-gui"
$WorkDir = Join-Path $BuildRoot "build"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " manus-gui 打包  v$Version" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "项目根目录: $ProjectRoot"
Write-Host "构建输出  : $BuildRoot"
Write-Host ""

# ---------- 1. 环境检查 ----------
Write-Host "[1/6] 检查构建环境..." -ForegroundColor Yellow

try {
    $pyVersion = (python --version 2>&1 | Out-String).Trim()
    Write-Host "  $pyVersion" -ForegroundColor Green
} catch {
    Write-Host "  未找到 Python" -ForegroundColor Red
    exit 1
}

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  PyInstaller 未安装，正在安装..." -ForegroundColor Yellow
    python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { Write-Host "  安装失败" -ForegroundColor Red; exit 1 }
}
Write-Host "  PyInstaller 就绪" -ForegroundColor Green

# ---------- 2. 清理 ----------
if ($Clean) {
    Write-Host "`n[2/6] 清理旧构建..." -ForegroundColor Yellow
    foreach ($d in @($DistDir, $WorkDir)) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force; Write-Host "  已删除 $d" }
    }
} else {
    Write-Host "`n[2/6] 跳过清理（增量构建）" -ForegroundColor Yellow
}
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null

# ---------- 3. PyInstaller ----------
Write-Host "`n[3/6] 运行 PyInstaller（约需 5-8 分钟）..." -ForegroundColor Yellow

$logPath = Join-Path $BuildRoot "pyinstaller.log"
Push-Location $ProjectRoot
try {
    $specPath = Join-Path $PackageDir "manus.spec"
    $distPath = Join-Path $BuildRoot "dist"
    # 走 cmd 做重定向，而不是 PowerShell 的 `> log 2>&1`：
    # PyInstaller 把进度信息全打在 stderr 上，而 Windows PowerShell 5.1 会把原生命令的
    # stderr 每一行包装成 NativeCommandError，配合 $ErrorActionPreference='Stop'
    # 会在第一行日志出现时就抛异常中断构建（并非真的失败）。
    $cmd = "python -m PyInstaller ""$specPath"" --noconfirm --clean --distpath ""$distPath"" --workpath ""$WorkDir"" > ""$logPath"" 2>&1"
    cmd /c $cmd
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Host "  PyInstaller 失败（退出码 $exitCode）" -ForegroundColor Red
    Write-Host "  日志尾部：" -ForegroundColor Yellow
    Get-Content $logPath -Tail 25 | ForEach-Object { Write-Host "    $_" }
    Write-Host "  完整日志: $logPath" -ForegroundColor Yellow
    exit 1
}
Write-Host "  打包完成" -ForegroundColor Green

# ---------- 4. 产物完整性校验 ----------
Write-Host "`n[4/6] 校验产物完整性..." -ForegroundColor Yellow
# 子进程按 UTF-8 输出，否则中文在非 UTF-8 代码页的控制台/管道里会变乱码
$env:PYTHONIOENCODING = "utf-8"
python (Join-Path $PackageDir "verify_build.py") $DistDir
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n产物校验未通过，已中止。" -ForegroundColor Red
    Write-Host "若提示「被改写」，请把 $BuildRoot 加入安全软件白名单后重新构建。" -ForegroundColor Yellow
    exit 1
}

# ---------- 5. 组装分发目录 ----------
Write-Host "`n[5/6] 组装分发目录..." -ForegroundColor Yellow

# 5.1 用户可编辑的 config/，必须与 exe 平级。
# app/config.py 的 DATA_ROOT 在冻结态优先取 exe 所在目录，读的就是这里；
# _internal 里那份只读副本仅作兜底，用户看不到也不该去改。
$cfgDir = Join-Path $DistDir "config"
New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
Copy-Item (Join-Path $ProjectRoot "config\config.example.toml") $cfgDir -Force
$mcpExample = Join-Path $ProjectRoot "config\mcp.example.json"
if (Test-Path $mcpExample) { Copy-Item $mcpExample $cfgDir -Force }
Write-Host "  已放置配置模板" -ForegroundColor Green

# 5.2 运行期脚本（启动调试 Chrome、首次运行向导、使用说明）
$runtimeSrc = Join-Path $PackageDir "runtime"
if (Test-Path $runtimeSrc) {
    Copy-Item "$runtimeSrc\*" $DistDir -Recurse -Force
    Write-Host "  已放置运行期脚本" -ForegroundColor Green
}

# 5.3 Playwright 浏览器（可选）。
# 采集/订单/活动三条管线全部走 connect_over_cdp 接管用户已登录的真实 Chrome，
# 不需要 playwright 自带的浏览器二进制；只有 agent 模式自行启动浏览器才用得上。
# 故默认不含，省下约 520MB。
if ($IncludeBrowsers) {
    $pwSrc = "$env:LOCALAPPDATA\ms-playwright"
    if (Test-Path $pwSrc) {
        Write-Host "  复制 Playwright 浏览器（约 520MB，需数分钟）..." -ForegroundColor Yellow
        $pwDst = Join-Path $DistDir "playwright_browsers"
        New-Item -ItemType Directory -Path $pwDst -Force | Out-Null
        Copy-Item "$pwSrc\*" $pwDst -Recurse -Force
        Write-Host "  浏览器已内置" -ForegroundColor Green
    } else {
        Write-Host "  未找到 Playwright 浏览器，跳过（先运行 playwright install chromium）" -ForegroundColor Yellow
    }
}

# 5.4 版本信息
[ordered]@{
    version      = $Version
    build_date   = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    has_browsers = [bool]$IncludeBrowsers
} | ConvertTo-Json | Out-File (Join-Path $DistDir "version.json") -Encoding UTF8

$distSize = (Get-ChildItem $DistDir -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "  分发目录大小: $([math]::Round($distSize/1MB,1)) MB" -ForegroundColor Green

# ---------- 6. 压缩 ----------
# 刻意产出 ZIP 而非安装程序 exe：一来分发途中 ZIP 里的 exe 不会被安全软件改写，
# 二来解压即用、装到哪都行，避开了 Program Files 只读带来的一连串权限问题。
if (-not $SkipZip) {
    Write-Host "`n[6/6] 压缩为分发包..." -ForegroundColor Yellow
    $zipPath = Join-Path $BuildRoot "manus-gui-$Version.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path "$DistDir\*" -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "  已生成: $zipPath ($([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB)" -ForegroundColor Green
} else {
    Write-Host "`n[6/6] 跳过压缩" -ForegroundColor Yellow
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " 打包完成" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "分发目录: $DistDir" -ForegroundColor White
if (-not $SkipZip) {
    Write-Host "分发压缩包: $(Join-Path $BuildRoot ('manus-gui-' + $Version + '.zip'))" -ForegroundColor White
}
