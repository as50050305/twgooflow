@echo off
rem Manual run: fetch data, rebuild dashboard data. Double-click to run.
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
echo Using Python:
%PY% --version
%PY% fetch.py %*
echo.
echo Done. Open dashboard.html to view. Logs are in the logs folder.
pause
goto :eof

:nopy
echo.
echo Cannot find a working Python 3.7 or newer.
echo Fix: install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo      then run this file again. Diagnostic info:
where py 2>&1
where python 2>&1
py -0p 2>&1
pause
