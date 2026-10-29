<#
.SYNOPSIS
    在 Windows 上一条命令构建 EVE Update Accelerator 的 exe 与 Win10 安装包。

.DESCRIPTION
    产物：
      dist\app\                                             PyInstaller onedir 运行时
      dist\EveUpdateAccelerator-<ver>-win10-x64-setup.exe   安装包

    为什么先打 onedir 再打包，而不是直接 --onefile：
      单文件版每次启动都要把 30 MB 解压到临时目录，启动慢、易被杀软拦；
      安装包本来就会把文件铺开，没必要再让程序自己解压一次。
      两个 exe（图形界面 + 命令行）共用一个 _internal 运行时，这里靠「打两次、
      只取对方的 exe」来合并——两份 COLLECT 产物除入口 exe 外完全一致。

.PARAMETER Python
    指定 Python 解释器。默认用 .venv\Scripts\python.exe，其次 py -3 / python。

.PARAMETER NsisPath
    makensis.exe 的路径或所在目录。默认依次找 PATH、Program Files\NSIS、
    以及 .tools\nsis-*\makensis.exe（本地缓存的便携版）。

.PARAMETER SkipInstaller
    只构建 exe，不构建安装包。

.EXAMPLE
    .\build_win10.ps1

.EXAMPLE
    .\build_win10.ps1 -NsisPath "C:\Program Files (x86)\NSIS"
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$NsisPath,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Fail([string]$text) {
    Write-Host "错误：$text" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 版本号
$initPy = Join-Path $root 'src\eveupdate\__init__.py'
if (-not (Test-Path $initPy)) { Fail "找不到 $initPy，请在仓库根目录运行。" }
$version = (Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) { Fail "无法从 __init__.py 读取 __version__。" }
Write-Host "EVE Update Accelerator $version" -ForegroundColor Green

# ---------------------------------------------------------------- Python
if (-not $Python) {
    $candidates = @(
        (Join-Path $root '.venv\Scripts\python.exe'),
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { $Python = $c; break }
    }
}
if (-not $Python) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $Python = $py.Source }
}
if (-not $Python) { Fail "找不到 Python。请安装 Python 3.9+ 或先创建 .venv。" }

Write-Step "使用解释器：$Python"
$pyOk = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "无法运行 $Python。" }
Write-Host "Python $pyOk"

# ---------------------------------------------------------------- PyInstaller
Write-Step "检查 PyInstaller"
& $Python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "未安装，正在安装 pyinstaller>=6.0 ..."
    & $Python -m pip install "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 安装失败。" }
}
$pyiVer = (& $Python -m PyInstaller --version) 2>&1 | Select-Object -Last 1
Write-Host "PyInstaller $pyiVer"

# ---------------------------------------------------------------- 构建 exe
$dist   = Join-Path $root 'dist'
$work   = Join-Path $root 'build\pyinstaller'
$icon   = Join-Path $root 'assets\icon.ico'
$vinfo  = Join-Path $root 'packaging\version_info.txt'
$srcDir = Join-Path $root 'src'

$common = @(
    '--onedir', '--noconfirm', '--clean',
    '--paths', $srcDir,
    '--collect-submodules', 'eveupdate',
    '--hidden-import', 'tkinter',
    '--icon', $icon,
    '--version-file', $vinfo,
    '--distpath', $dist,
    '--workpath', $work,
    '--specpath', (Join-Path $root 'build')
)

Write-Step "构建命令行版（控制台）"
& $Python -m PyInstaller @common --console --name 'eve-update-accelerator' `
    (Join-Path $root 'packaging\build_entry.py')
if ($LASTEXITCODE -ne 0) { Fail "命令行版构建失败。" }

Write-Step "构建图形界面版（无控制台）"
& $Python -m PyInstaller @common --windowed --name 'eve-update-accelerator-gui' `
    (Join-Path $root 'packaging\build_entry_gui.py')
if ($LASTEXITCODE -ne 0) { Fail "图形界面版构建失败。" }

# ---------------------------------------------------------------- 合并运行时
Write-Step "合并两份运行时（只保留一份 _internal）"
$app = Join-Path $dist 'app'
if (Test-Path $app) { Remove-Item $app -Recurse -Force }
New-Item -ItemType Directory -Path $app | Out-Null
Copy-Item (Join-Path $dist 'eve-update-accelerator\*') $app -Recurse -Force
Copy-Item (Join-Path $dist 'eve-update-accelerator-gui\eve-update-accelerator-gui.exe') $app -Force

$appFiles = (Get-ChildItem $app -Recurse -File).Count
$appSize  = [math]::Round(((Get-ChildItem $app -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "dist\app：$appFiles 个文件，$appSize MB"

if ($SkipInstaller) {
    Write-Host ""
    Write-Host "已跳过安装包（-SkipInstaller）。" -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------- NSIS
Write-Step "定位 makensis.exe"
$makensis = $null
if ($NsisPath) {
    $candidate = if (Test-Path $NsisPath -PathType Container) { Join-Path $NsisPath 'makensis.exe' } else { $NsisPath }
    if (Test-Path $candidate) { $makensis = $candidate }
    else { Fail "-NsisPath 下找不到 makensis.exe：$NsisPath" }
}
if (-not $makensis) {
    $cmd = Get-Command makensis -ErrorAction SilentlyContinue
    if ($cmd) { $makensis = $cmd.Source }
}
if (-not $makensis) {
    $globs = @(
        (Join-Path $root '.tools\nsis-*\makensis.exe'),
        'C:\Program Files (x86)\NSIS\makensis.exe',
        'C:\Program Files\NSIS\makensis.exe'
    )
    foreach ($g in $globs) {
        $hit = Get-ChildItem $g -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $makensis = $hit.FullName; break }
    }
}
if (-not $makensis) {
    Fail @"
找不到 NSIS（makensis.exe）。安装方式任选其一：
  1) winget install NSIS.NSIS
  2) choco install nsis
  3) 下载便携版 https://nsis.sourceforge.io/Download 解压到 .tools\nsis-3.x\
然后重跑本脚本，或用 -NsisPath 指定位置。
"@
}
Write-Host "makensis：$makensis"

Write-Step "编译安装包"
$setup = Join-Path $dist "EveUpdateAccelerator-$version-win10-x64-setup.exe"
Push-Location (Join-Path $root 'packaging')
try {
    & $makensis /V2 "/DAPP_VERSION=$version" "/DOUTFILE=$setup" 'installer.nsi'
    if ($LASTEXITCODE -ne 0) { Fail "安装包编译失败。" }
} finally {
    Pop-Location
}

$setupSize = [math]::Round(((Get-Item $setup).Length / 1MB), 2)
Write-Host ""
Write-Host "构建完成" -ForegroundColor Green
Write-Host "  安装包：dist\$(Split-Path $setup -Leaf)  ($setupSize MB)"
Write-Host "  免安装：dist\app\（可直接运行 eve-update-accelerator-gui.exe）"
