# LihuQuantify → NAS 同步（2026-09-26 建）
# 前提：NAS 在线（192.168.123.204）且 Windows 已存凭据（2026-09-26 已配置 tld）
# 铁律：绝不推送 data/（NAS 侧 paper_state/duckdb 是生产状态）与 outputs/（NAS 自产报告）
# 用法：pwsh -File scripts\sync_to_nas.ps1
param([string]$NasRoot = "\\192.168.123.204\Lihu_Quantify")
if (-not (Test-Path $NasRoot)) { Write-Host "NAS 目录不可达: $NasRoot —— 请先开机/映射共享文件夹"; exit 1 }
$src = $PSScriptRoot\..
foreach ($dir in @("src", "config", "scripts", "web")) {
    robocopy (Join-Path $src $dir) (Join-Path $NasRoot $dir) /MIR /XD __pycache__ .pytest_cache node_modules /XF *.pyc | Out-Null
    Write-Host "已同步: $dir"
}
foreach ($f in @("run_scheduler.py","run_backtest.py","run_full_backtest.py","run_live.py","pyproject.toml","README.md","Dockerfile","docker-compose.yml",".dockerignore","docs\决策日志.md")) {
    $from = Join-Path $src $f; $to = Join-Path $NasRoot $f
    if (Test-Path $from) { New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null; Copy-Item $from $to -Force }
}
# 修正 NAS 侧 token_file 为容器路径（本地为 Windows 路径，直接覆盖会导致容器内 token 读不到）
$nasYaml = Join-Path $NasRoot "config\settings.yaml"
if (Test-Path $nasYaml) {
    (Get-Content $nasYaml -Raw) -replace 'token_file: .*', 'token_file: /app/tushareMcp.json' | Set-Content $nasYaml -NoNewline -Encoding UTF8
    Write-Host "已修正 NAS token_file → /app/tushareMcp.json"
}
Write-Host "同步完成——注意：src 构建进镜像，需在 NAS 执行 docker compose up -d --build（或 DSM Container Manager 点构建）才生效；仅改 settings.yaml 时重启容器即可"
Write-Host "注意：config/settings.yaml 已推送（含 vol_target/ir20 影子账本），NAS 侧 .env（邮件授权码等）不受影响"
