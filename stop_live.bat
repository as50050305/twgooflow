@echo off
rem Stops the running live service (wherever it runs: background, schedule window, or live.bat window).
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
%PY% live.py --stop
timeout /t 5 >nul 2>nul
goto :eof
:nopy
echo Cannot find a working Python 3.7 or newer.
pause
