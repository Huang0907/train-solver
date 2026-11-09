# 打包独立客户端（单文件 exe，内置 Playwright：可直接勾选「模拟真人打字提交」）
# 用法：powershell -ExecutionPolicy Bypass -File build_client.ps1
#
# 这是日常开发用的单版本打包（只出 dist\TrainSolveClient.exe，不动 release\）。
# 要出交付包（public / full 两版）用 build_release.ps1。
$ErrorActionPreference = "Stop"
# 中文输出别乱码，也让扫描器的输出可读
$env:PYTHONIOENCODING = "utf-8"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

$versionFile = Join-Path $here "build\version_info.txt"

Write-Host "== 1/4 检查依赖 ==" -ForegroundColor Cyan
python -c "import tkinter, sys; print('tkinter', tkinter.TkVersion, '| python', sys.version.split()[0])"
python -m PyInstaller --version
python -c "import playwright; print('playwright', playwright.__file__)"

Write-Host "== 2/4 运行测试 ==" -ForegroundColor Cyan
python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "测试未通过，终止打包" }

Write-Host "== 3/4 生成版本资源并打包 ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $versionFile) | Out-Null
python (Join-Path $here "scripts\gen_version_info.py") $versionFile
if ($LASTEXITCODE -ne 0) { throw "生成版本资源失败" }

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name TrainSolveClient `
    --version-file $versionFile `
    --hidden-import trainsolve.gui `
    --hidden-import trainsolve.human `
    --hidden-import trainsolve.humantype `
    --hidden-import playwright.sync_api `
    --collect-data playwright `
    --distpath dist --workpath build/pyi --specpath build `
    client_main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

if (-not (Test-Path "dist\TrainSolveClient.exe")) {
    throw "打包失败，没有生成 exe"
}

Write-Host "== 4/4 解包后扫描凭据 ==" -ForegroundColor Cyan
& python (Join-Path $here "scripts\scan_exe_secrets.py") "dist\TrainSolveClient.exe"
$scanCode = $LASTEXITCODE
if ($scanCode -eq 1) { throw "exe 命中凭据（见上），不能交付" }
if ($scanCode -ne 0) { throw "密钥扫描器自身出错（exit $scanCode），不能当作通过" }

$size = [math]::Round((Get-Item "dist\TrainSolveClient.exe").Length / 1MB, 1)
Write-Host "完成：dist\TrainSolveClient.exe（$size MB，含 Playwright driver 与版本资源）" -ForegroundColor Green
