@echo off
chcp 65001 >nul
rem ============================================================
rem LihuQuantify 本机模式一键启动（NAS 不可用时快速方案）
rem  - 看板: python -m web.server  (127.0.0.1:8000)
rem  - 调度: python run_scheduler.py (模拟盘，可选手动巡检)
rem ============================================================
cd /d E:\LihuQuantify

set PY=E:\LihuQuantify\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [1/3] 启动看板服务 (127.0.0.1:8000)...
start "LihuQuantify Web" cmd /k "%PY% -m web.server"

echo [2/3] 健康检查（最多等 30 秒）...
set /a tries=0
:wait
curl -s -o nul http://127.0.0.1:8000/api/health
if not errorlevel 1 goto :ok
set /a tries+=1
if %tries% geq 6 goto :fail
timeout /t 5 /nobreak >nul
goto :wait

:ok
echo [3/3] 服务就绪，打开看板...
start http://127.0.0.1:8000/
echo.
echo 如需启动日常巡检调度（模拟盘），请另开窗口运行：
echo   E:\LihuQuantify\start_scheduler_local.bat
echo 或立即手动巡检一次：
echo   %PY% run_scheduler.py --run-now
echo.
pause
exit /b 0

:fail
echo.
echo [错误] 看板未就绪：请检查 .venv 是否完整、8000 端口是否被占用。
pause
exit /b 1
