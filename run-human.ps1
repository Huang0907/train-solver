# trainsolve · 模拟人类打字模式启动器
#
#   .\run-human.ps1 --human --count 1 --cpm 400 --yes       打字 + 真实提交（1 题）
#   .\run-human.ps1 --human-dry-run --count 1 --cpm 400     只打字并回读校验，不提交
#   .\run-human.ps1 resume --session <id> --human --yes     对已开会话续做
#   .\run-human.ps1 run --auto --human --cpm 300 --yes      连续刷（每题都真打字提交）
#
# 首次运行会自动 pip install playwright；浏览器会被自动拉起（独立 profile，不动日常 Chrome）。
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $here

Write-Host "== 检查 Python ==" -ForegroundColor Cyan
$pyVersion = python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
Write-Host "  Python $pyVersion"

Write-Host "== 检查 Playwright ==" -ForegroundColor Cyan
python -c "import playwright" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  未安装，开始安装依赖（只装一次）…" -ForegroundColor Yellow
    python -m pip install --upgrade pip
    python -m pip install -r requirements-human.txt
    if ($LASTEXITCODE -ne 0) { throw "Playwright 安装失败，请手动执行：python -m pip install -r requirements-human.txt" }
}
Write-Host "  ✓ 依赖就绪"

if (-not $CliArgs -or $CliArgs.Count -eq 0) {
    Write-Host ""
    Write-Host "用法示例："
    Write-Host "  .\run-human.ps1 --human-dry-run --count 1 --cpm 400      # 先试：只打字不提交"
    Write-Host "  .\run-human.ps1 --human --count 1 --cpm 400 --yes        # 真打字 + 真提交"
    Write-Host "  .\run-human.ps1 resume --session <id> --human --yes      # 续做已开会话"
    Write-Host ""
    Write-Host "配置：同目录 config.toml 填平台账号密码 + 模型 Key（平台地址已填好）"
    exit 0
}

Write-Host "== 启动 trainsolve $($CliArgs -join ' ') ==" -ForegroundColor Cyan
python -m trainsolve @CliArgs
exit $LASTEXITCODE