# 慧博研报归档（2026-09-26 更新：下载目录 = 归档目录 = PaperA-Stock）
# 慧博客户端直接下载到 PaperA-Stock，本脚本只做规范化重命名：给无日期前缀的 PDF 加 "yyyyMMdd_" 前缀
# 用法：pwsh -File scripts\hibor_watch.ps1 （或注册计划任务定时跑）
param(
    [string]$Dir = "E:\Dsh_WorkSapce\Dify_Agents\PaperA-Stock",
    [string]$StateFile = "$PSScriptRoot\..\data\hibor_watch_state.json"
)
if (-not (Test-Path $Dir)) { Write-Host "目录不存在: $Dir"; exit 0 }
$seen = @{}
if (Test-Path $StateFile) { $seen = Get-Content $StateFile -Raw | ConvertFrom-Json -AsHashtable }
$n = 0
Get-ChildItem $Dir -Filter *.pdf -File | ForEach-Object {
    $key = $_.Name + "_" + $_.Length
    if ($_.Name -notmatch '^\d{8}_' -and -not $seen.ContainsKey($key)) {
        $new = Join-Path $Dir ("{0}_{1}" -f $_.LastWriteTime.ToString("yyyyMMdd"), $_.Name)
        Rename-Item $_.FullName $new -Force
        $seen[$key] = $new
        $n++
        Write-Host "重命名: $($_.Name) -> $(Split-Path $new -Leaf)"
    }
}
$seen | ConvertTo-Json -Depth 3 | Set-Content $StateFile -Encoding UTF8
Write-Host "完成：规范化 $n 个"
