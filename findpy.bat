@echo off
rem Finds a Python 3.7+ that really runs. Sets PY (console) and PYW (no window, may be empty).
rem Each candidate is test-run, so a broken install or a dead "py" launcher entry is skipped.
set PY=
set PYW=
py -3 -c "import sys;sys.exit(sys.version_info<(3,7))" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  where pyw >nul 2>nul && set "PYW=pyw -3"
  goto :eof
)
for %%D in ("%LOCALAPPDATA%\Programs\Python\Python314" "%LOCALAPPDATA%\Programs\Python\Python313" "%LOCALAPPDATA%\Programs\Python\Python312" "%LOCALAPPDATA%\Programs\Python\Python311" "%LOCALAPPDATA%\Programs\Python\Python310" "%ProgramFiles%\Python314" "%ProgramFiles%\Python313" "%ProgramFiles%\Python312" "%ProgramFiles%\Python311" "C:\Python314" "C:\Python313" "C:\Python312" "%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3") do if not defined PY call :trydir %%D
if defined PY goto :eof
python -c "import sys;sys.exit(sys.version_info<(3,7))" >nul 2>nul
if not errorlevel 1 (
  set PY=python
  where pythonw >nul 2>nul && set PYW=pythonw
)
goto :eof

:trydir
if not exist "%~1\python.exe" goto :eof
"%~1\python.exe" -c "import sys;sys.exit(sys.version_info<(3,7))" >nul 2>nul
if errorlevel 1 goto :eof
set PY="%~1\python.exe"
if exist "%~1\pythonw.exe" set PYW="%~1\pythonw.exe"
goto :eof
