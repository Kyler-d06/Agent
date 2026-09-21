@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$all=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'start_platform.py' }); $ids=@($all.ProcessId); $roots=@($all | Where-Object { $_.ParentProcessId -notin $ids }); if(-not $roots){ Write-Host 'Universal Assistant is not running.'; exit 0 }; foreach($process in $roots){ Write-Host ('Stopping supervisor PID ' + $process.ProcessId); taskkill.exe /PID $process.ProcessId /T /F }"
exit /b %errorlevel%
