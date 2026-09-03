@echo off
chcp 65001 >nul
rem ============================================================
rem LihuQuantify 一键停止（第九轮问题4）：停止 NAS 上的两个容器
rem ============================================================
set NAS_HOST=root@NANDH
set NAS_DIR=/volume2/Lihu_Quantify

echo 停止 NAS 容器（scheduler + web）...
ssh %NAS_HOST% "cd %NAS_DIR% && docker-compose stop"
if errorlevel 1 goto :fail
echo 已停止。
pause
exit /b 0

:fail
echo.
echo [错误] 停止失败：请检查 NAS 开机、SSH 免密配置、docker-compose 路径。
pause
exit /b 1
