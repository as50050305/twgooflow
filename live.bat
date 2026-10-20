@echo off
rem Intraday (live) mode: runs the live service IN THIS WINDOW and opens http://localhost:8765.
rem Keep this window open during trading hours. Close it (or press Ctrl+C) to stop the service.
rem If a service is already running (e.g. started by the 08:40 schedule), it is asked to stop and this window takes over.
rem To stop the service from anywhere: double-click stop_live.bat
title TWFlow live - close this window to stop
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
echo Using Python:
%PY% --version
%PY% live.py --foreground %*
echo.
echo Live service stopped. You can close this window.
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
