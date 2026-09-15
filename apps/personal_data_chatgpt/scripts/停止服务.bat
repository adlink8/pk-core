@echo off
setlocal
pwsh -NoProfile -File "%~dp0start-services.ps1" -Mode Stop
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
