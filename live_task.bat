@echo off
rem Used by Windows Task Scheduler at 08:40 on weekdays: starts the live service in the background (no window).
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
if defined PYW goto runw
start "TWFlow live" /min %PY% live.py --no-browser
goto :eof
:runw
start "" %PYW% live.py --no-browser
goto :eof
:nopy
echo %date% %time% live_task: no working Python 3.7+ found >> logs\task_console.log
