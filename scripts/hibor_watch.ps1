# 慧博下载目录监控：扫描新 PDF 归档到 PaperA-Stock（按 日期_原文件名 命名）
# 用法（手动）：pwsh -File scripts\hibor_watch.ps1
# 建议注册为每 10 分钟计划任务（见 docs\运维手册.md）
param(
    [string]$WatchDir = "F:\hibor_down",
    [string]$ArchiveDir = "E:\Dsh_WorkSapce\Dify_Agents\PaperA-Stock",
    [string]$StateFile = "$PSScriptRoot\..\data\hibor_watch_state.json"
)
if (-not (Test-Path $WatchDir)) { Write-Host "监控目录 $WatchDir 不存在——请先在慧博客户端设置默认下载目录为该路径"; exit 0 }
New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null
$seen = @{}
if (Test-Path $StateFile) { $seen = Get-Content $StateFile -Raw | ConvertFrom-Json -AsHashtable }
$n = 0
Get-ChildItem $WatchDir -Filter *.pdf -File | ForEach-Object {
    $key = $_.Name + "_" + $_.Length
    if (-not $seen.ContainsKey($key)) {
        $dest = Join-Path $ArchiveDir ("{0}_{1}" -f $_.LastWriteTime.ToString("yyyyMMdd"), $_.Name)
        Copy-Item $_.FullName $dest -Force
        $seen[$key] = $dest
        $n++
        Write-Host "归档: $dest"
    }
}
$seen | ConvertTo-Json -Depth 3 | Set-Content $StateFile -Encoding UTF8
Write-Host "完成：新归档 $n 个"
