@echo off
chcp 65001 >nul
rem ============================================================
rem LihuQuantify 本机一键启动看板（Windows）
rem 逻辑：已在运行 → 直接开浏览器；未运行 → 启动 .venv web 服务
rem       → 健康检查（最多约 30 秒）→ 通过后打开看板
rem ============================================================
cd /d "%~dp0"

rem ---- 服务已在运行？----
curl -s -o nul http://127.0.0.1:8000/api/health
if not errorlevel 1 goto :open

rem ---- 启动 web 服务（新窗口最小化，关窗口即停服务）----
if not exist ".venv\Scripts\python.exe" goto :fail
start "LihuQuantify Web" /min ".venv\Scripts\python.exe" -m web.server

rem ---- 等待就绪 ----
set /a tries=0
:wait
curl -s -o nul http://127.0.0.1:8000/api/health
if not errorlevel 1 goto :open
set /a tries+=1
if %tries% geq 10 goto :fail
timeout /t 3 /nobreak >nul
goto :wait

:open
start http://127.0.0.1:8000/
exit /b 0

:fail
echo.
echo [错误] 看板服务未能启动：请检查 .venv 是否完整。
echo 手动验证：.venv\Scripts\python.exe -m web.server
pause
exit /b 1
