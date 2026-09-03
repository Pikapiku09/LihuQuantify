@echo off
chcp 65001 >nul
rem ============================================================
rem LihuQuantify 每日巡检（Windows 计划任务入口，16:35 触发）
rem 跑 scripts/backfill_scans.py：补齐所有缺失交易日（幂等/自愈/
rem 跑完即退出），日志落 data/logs/scheduler_YYYYMMDD.log
rem ============================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" exit /b 1
".venv\Scripts\python.exe" "scripts\backfill_scans.py" --n 50 1>>data\logs\daily_task.log 2>&1
exit /b %errorlevel%
