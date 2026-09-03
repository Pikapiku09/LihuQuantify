@echo off
chcp 65001 >nul
rem ============================================================
rem LihuQuantify 一键启动（第九轮问题4）：NAS 容器启动 → 健康检查 → 打开看板
rem 前置：Windows ssh 免密登录 NAS（ssh-keygen + authorized_keys，见部署文档）
rem ============================================================
set NAS_HOST=root@NANDH
set NAS_DIR=/volume2/Lihu_Quantify
set WEB_URL=http://192.168.123.203:8000

echo [1/3] 启动 NAS 容器（scheduler + web）...
ssh %NAS_HOST% "cd %NAS_DIR% && docker-compose up -d"
if errorlevel 1 goto :fail

echo [2/3] 健康检查（最多等待约 60 秒）...
set /a tries=0
:wait
curl -s -o nul %WEB_URL%/api/health
if not errorlevel 1 goto :ok
set /a tries+=1
if %tries% geq 12 goto :fail
timeout /t 5 /nobreak >nul
goto :wait

:ok
echo [3/3] 服务就绪，打开看板...
start %WEB_URL%
exit /b 0

:fail
echo.
echo [错误] NAS 或容器未就绪：请检查 NAS 开机、SSH 免密配置、docker-compose 路径。
pause
exit /b 1
