@echo off
rem Used by Windows Task Scheduler at 08:40 on weekdays: opens a console window that runs the live service.
rem Keep that window open; close it (or run stop_live.bat) to stop the service.
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
start "TWFlow live - close this window to stop" %PY% live.py --no-browser
goto :eof
:nopy
echo %date% %time% live_task: no working Python 3.7+ found >> logs\task_console.log
