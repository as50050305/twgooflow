@echo off
rem Used by Windows Task Scheduler (no pause).
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
%PY% fetch.py >> logs\task_console.log 2>&1
goto :eof
:nopy
echo %date% %time% run_task: no working Python 3.7+ found >> logs\task_console.log
