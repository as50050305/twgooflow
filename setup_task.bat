@echo off
rem Create scheduled tasks:
rem   TWFlow       every weekday 18:45 and 21:45 (after-market data; 21:45 catches late margin data)
rem   TWFlow_Live  every weekday 08:40 (intraday live service, runs in background until about 14:10)
rem If the PC was off at that time, it runs as soon as possible after start-up.
cd /d "%~dp0"
if not exist logs mkdir logs
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%~dp0'; $days='Monday','Tuesday','Wednesday','Thursday','Friday'; $a=New-ScheduledTaskAction -Execute ($d+'run_task.bat') -WorkingDirectory $d; $t1=New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At '18:45'; $t2=New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At '21:45'; $s=New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; Register-ScheduledTask -TaskName 'TWFlow' -Action $a -Trigger $t1,$t2 -Settings $s -Force | Out-Null; $a2=New-ScheduledTaskAction -Execute ($d+'live_task.bat') -WorkingDirectory $d; $t3=New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At '08:40'; $s2=New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; Register-ScheduledTask -TaskName 'TWFlow_Live' -Action $a2 -Trigger $t3 -Settings $s2 -Force | Out-Null; Get-ScheduledTask -TaskName 'TWFlow','TWFlow_Live' | Select-Object TaskName,State"
if errorlevel 1 (
  echo PowerShell method failed, trying schtasks...
  schtasks /Create /F /TN "TWFlow_1845" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:45 /TR "%~dp0run_task.bat"
  schtasks /Create /F /TN "TWFlow_2145" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 21:45 /TR "%~dp0run_task.bat"
  schtasks /Create /F /TN "TWFlow_Live" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:40 /TR "%~dp0live_task.bat"
)
echo.
echo If you see TWFlow and TWFlow_Live above (State Ready), the schedule is set.
echo During trading hours open http://localhost:8765 (or double-click live.bat).
echo To remove them later: open Task Scheduler and delete tasks TWFlow and TWFlow_Live.
pause
