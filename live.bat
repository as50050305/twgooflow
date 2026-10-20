@echo off
rem Intraday (live) mode: makes sure the live service runs in the background, then opens http://localhost:8765.
rem If it is already running (e.g. started by the 08:40 schedule), it just opens the browser.
rem The service does not depend on this window: this window closes by itself after 5 seconds.
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
call "%~dp0findpy.bat"
if not defined PY goto nopy
echo Using Python:
%PY% --version
%PY% live.py --ensure %*
rem exit code 3 = the live service is already running in the background (not an error): close by itself
if errorlevel 3 if not errorlevel 4 (
  timeout /t 5 >nul 2>nul
  goto :eof
)
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
