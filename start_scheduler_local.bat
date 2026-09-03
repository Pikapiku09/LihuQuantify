@echo off
chcp 65001 >nul
rem LihuQuantify 本机调度器（模拟盘，常驻巡检）
cd /d E:\LihuQuantify
set PY=E:\LihuQuantify\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
echo 启动调度器（模拟盘）... Ctrl+C 停止
"%PY%" run_scheduler.py
pause
