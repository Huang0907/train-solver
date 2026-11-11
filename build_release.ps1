# 打包发布版（模拟人类输入已整合进 exe）
#
#   公开版 public：release\TrainSolveClient-<ver>-public      （目录 + zip）
#       exe + config.public.toml（账号/Key 全空）+ portable.txt + 使用说明
#
# 规矩（2026-11-06 改定：只产出 public，不再产出 full）：
#   1) 本脚本只产出公开版，写进 release\；**full 版本已取消**，不再有自用目录、
#      不再有 full 的 zip 开关，也不再写外发警示文件。
#   2) release\ 里一旦出现 full 包（历史遗留或误拷），脚本在打包**之前**报错退出，
#      防止旧的私有包被顺手转发。
#   3) 凭据只来自仓库根 config.toml，本脚本内不内联任何 key；exe 的密钥扫描是
#      **解包后再扫**（PyInstaller 的 PYZ 是 zlib 压缩的，扫原始字节会假阴性）。
#   4) 公开版 exe 里不得残留任何真实凭据（账号/密码/Key）；config.toml 三个
#      凭据字段必须为空，首次启动由用户自己填。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build_release.ps1
$ErrorActionPreference = 'Stop'
# 中文输出别乱码，也让 grab_version / 扫描器的输出可读
$env:PYTHONIOENCODING = 'utf-8'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

$version = (python -c "import trainsolve;print(trainsolve.__version__)").Trim()
Write-Host "== 版本 $version ==" -ForegroundColor Cyan

$releaseRoot = Join-Path $here 'release'
$pubDir = Join-Path $releaseRoot "TrainSolveClient-$version-public"
$versionFile = Join-Path $here 'build\version_info.txt'

# ---- 前置检查：release\ 里不许有 full（历史遗留也拦） ----
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
$stale = @(Get-ChildItem $releaseRoot -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '*full*' })
if ($stale.Count -gt 0) {
    $names = ($stale | ForEach-Object { $_.Name }) -join ', '
    throw "release\ 里出现 full 包：$names。本项目只产出 public，full 已取消，请先把它们移走再打包。"
}

# 真实凭据不写进仓库：从本机 config（被 .gitignore 挡住）读学号与 Key，取前缀做扫描名单
# 0.6.9 起本机配置可能已加密成 config.toml.enc：先用 CLI 解密到临时文件再读，读完立刻删。
$secretForbidden = @()
$localConfig = Join-Path $here 'config.toml'
$encConfig = Join-Path $here 'config.toml.enc'
$tempConfig = $null
if (-not (Test-Path $localConfig) -and (Test-Path $encConfig)) {
    $tempConfig = Join-Path $env:TEMP ("trainsolve-creds-" + [guid]::NewGuid().ToString('N') + ".toml")
    & python -B -m trainsolve decrypt-config --source $encConfig --to $tempConfig --force | Out-Null
    if ($LASTEXITCODE -eq 0 -and (Test-Path $tempConfig)) {
        $localConfig = $tempConfig
    } else {
        Write-Host "   ! config.toml.enc 解密失败（换用户/换机器会这样），本次只按通用规则扫描" -ForegroundColor Yellow
    }
}
try {
    if (Test-Path $localConfig) {
        $cfgText = [System.IO.File]::ReadAllText($localConfig)
        foreach ($pattern in @('(?m)^\s*username\s*=\s*"([^"]+)"', '(?m)^\s*api_key\s*=\s*"([^"]+)"')) {
            foreach ($match in [regex]::Matches($cfgText, $pattern)) {
                $value = $match.Groups[1].Value
                if ($value.Length -ge 6) {
                    $secretForbidden += $value.Substring(0, [Math]::Min(8, $value.Length))
                }
            }
        }
        $secretForbidden = @($secretForbidden | Select-Object -Unique)
    }
} finally {
    if ($tempConfig -and (Test-Path $tempConfig)) {
        Remove-Item $tempConfig -Force -ErrorAction SilentlyContinue
    }
}
if ($secretForbidden.Count -eq 0) {
    Write-Host "   ! 本机没有带凭据的 config.toml / config.toml.enc，本次只按通用规则扫描" -ForegroundColor Yellow
}
$emptyCredentialPatterns = @(
    'username\s*=\s*"[^"]+"', 'password\s*=\s*"[^"]+"', 'api_key\s*=\s*"[^"]+"'
)

Write-Host "== 1/7 运行测试 ==" -ForegroundColor Cyan
python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "测试未通过，终止打包" }

Write-Host "== 2/7 生成版本资源（版本号取自 trainsolve.__version__） ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $versionFile) | Out-Null
python (Join-Path $here 'scripts\gen_version_info.py') $versionFile
if ($LASTEXITCODE -ne 0) { throw "生成版本资源失败" }

Write-Host "== 3/7 打包 exe（内置 Playwright + 版本资源） ==" -ForegroundColor Cyan
$pyiArgs = @(
    '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'TrainSolveClient',
    '--version-file', $versionFile,
    '--hidden-import', 'trainsolve.gui',
    '--hidden-import', 'trainsolve.human',
    '--hidden-import', 'trainsolve.humantype',
    '--hidden-import', 'playwright.sync_api',
    '--collect-data', 'playwright',
    '--distpath', 'dist', '--workpath', 'build/pyi', '--specpath', 'build',
    'client_main.py'
)
python -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
$exe = Join-Path $here 'dist\TrainSolveClient.exe'
if (-not (Test-Path $exe)) { throw "打包失败，没有生成 exe" }
$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "   ✓ dist\TrainSolveClient.exe（$sizeMB MB）" -ForegroundColor Green

Write-Host "== 4/7 检查 exe 内不含任何凭据（解包后逐模块扫描） ==" -ForegroundColor Cyan
$scanArgs = @((Join-Path $here 'scripts\scan_exe_secrets.py'), $exe)
if ($secretForbidden.Count -gt 0) { $scanArgs += @('--forbid') + $secretForbidden }
& python @scanArgs
$scanCode = $LASTEXITCODE
if ($scanCode -eq 1) { throw "exe 命中凭据（见上），终止打包" }
if ($scanCode -ne 0) { throw "密钥扫描器自身出错（exit $scanCode），不能当作通过" }

Write-Host "== 5/7 校验 exe 的版本资源与 Playwright driver ==" -ForegroundColor Cyan
$verTmp = Join-Path ([System.IO.Path]::GetTempPath()) "ts_version_$PID.txt"
if (Test-Path $verTmp) { Remove-Item $verTmp -Force }
# 两个坑叠在一起，都踩过：
#   1) grab_version 的第二个**位置参数**才是输出文件，只重定向 stdout 会拿到空内容；
#   2) 它会把 DeprecationWarning 写到 stderr，而 $ErrorActionPreference='Stop' 时
#      PowerShell 5.1 会把原生 stderr 当成致命错误直接中断脚本——所以本段临时放宽。
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & python -W ignore -m PyInstaller.utils.cliutils.grab_version $exe $verTmp *> $null
    $listing = (python -m PyInstaller.utils.cliutils.archive_viewer -l $exe | Out-String)
}
finally {
    $ErrorActionPreference = $prevEap
}
if (-not (Test-Path $verTmp)) { throw "exe 里没有版本资源（--version-file 没生效）" }
$verDump = Get-Content $verTmp -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
Remove-Item $verTmp -Force -ErrorAction SilentlyContinue
if (-not $verDump -or $verDump -notmatch 'ProductVersion') {
    throw "exe 里没有版本资源（--version-file 没生效）"
}
if ($verDump -notmatch [regex]::Escape("'$version'")) {
    throw "exe 版本资源里的 ProductVersion 不是 $version"
}
if ($listing -notmatch 'playwright') { throw "exe 里没找到 playwright（打包参数失效？）" }
if ($listing -notmatch 'driver') { throw "exe 里没找到 Playwright driver（模拟打字会失败）" }
Write-Host "   ✓ 版本资源 = $version；Playwright driver 已内置" -ForegroundColor Green

Write-Host "== 6/7 组装公开版（无账号无密钥） ==" -ForegroundColor Cyan
if (Test-Path $pubDir) { Remove-Item -Recurse -Force $pubDir }
New-Item -ItemType Directory -Force -Path $pubDir | Out-Null
Copy-Item $exe (Join-Path $pubDir 'TrainSolveClient.exe')
Copy-Item (Join-Path $here 'config.public.toml') (Join-Path $pubDir 'config.toml')
Set-Content -Path (Join-Path $pubDir 'portable.txt') -Value 'portable' -Encoding ascii
Copy-Item (Join-Path $here 'docs\使用说明-公开版.md') (Join-Path $pubDir '使用说明.md')

$pubText = ((Get-ChildItem $pubDir -File) |
        ForEach-Object { [System.IO.File]::ReadAllText($_.FullName) }) -join "`n"
foreach ($needle in $secretForbidden) {
    if ($pubText.Contains($needle)) { throw "公开版目录里发现敏感串：$needle" }
}
$pubCfg = Get-Content (Join-Path $pubDir 'config.toml') -Raw
foreach ($pattern in $emptyCredentialPatterns) {
    if ($pubCfg -match $pattern) { throw "公开版 config.toml 仍有非空凭据：$($Matches[0])" }
}
Write-Host "   ✓ 公开版：账号/密码/Key 全为空，无私有网关" -ForegroundColor Green

Write-Host "== 7/7 压缩 ==" -ForegroundColor Cyan
$pubZip = Join-Path $releaseRoot "TrainSolveClient-$version-public.zip"
if (Test-Path $pubZip) { Remove-Item -Force $pubZip }
Compress-Archive -Path (Join-Path $pubDir '*') -DestinationPath $pubZip -CompressionLevel Optimal
Write-Host "   ✓ $pubZip" -ForegroundColor Green

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "发布目录（可外发）：$releaseRoot" -ForegroundColor Green
Get-ChildItem $releaseRoot | Sort-Object Name |
    Format-Table Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime -AutoSize
exit 0
