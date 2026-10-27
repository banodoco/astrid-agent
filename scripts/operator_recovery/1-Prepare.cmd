@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Invoke-AstridRecovery.ps1" -Action prepare
set "recovery_exit=%ERRORLEVEL%"
echo.
pause
exit /b %recovery_exit%
