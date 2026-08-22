@echo off
REM ============================================================
REM  搜狗微信采集 · 常驻循环启动器（Windows 采集 VM 用）
REM  作用：校验外部注入的端点/凭据 + 循环拉起 sogou_loop，进程退出后自动重启。
REM  用法：把仓库同步到 VM，进入仓库根目录双击本脚本（或计划任务开机自启）。
REM  前置：VM 已装 python + playwright，且执行过  playwright install chromium
REM ============================================================

REM —— 端点和凭据必须由计划任务/系统环境注入，绝不写进仓库 ——
if not defined REDIS_URL goto missing_config
if not defined API_BASE goto missing_config
if not defined SOGOU_API_TOKEN goto missing_config

REM —— 运行参数（一天档 / 每轮5词 / 间隔60秒）——
set SOGOU_DEVICE=sogou-vm-01
set SOGOU_TIME=一天
set SOGOU_BATCH=5
set SOGOU_INTERVAL=60
set SOGOU_MAX_ITEMS=30

set PYTHONPATH=%CD%
chcp 65001 >NUL

:loop
echo [%date% %time%] 启动 sogou_loop ...
python -m wxsearch.sogou_loop
echo [%date% %time%] sogou_loop 退出，10 秒后自动重启 ...
timeout /t 10 /nobreak >NUL
goto loop

:missing_config
echo 缺少 REDIS_URL / API_BASE / SOGOU_API_TOKEN 环境变量，拒绝启动。
exit /b 2
