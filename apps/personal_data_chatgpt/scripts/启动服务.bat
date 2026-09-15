@echo off
chcp 65001 >nul
title Personal Data Services (REST + MCP + Tunnel)
echo Starting Personal Data Services ...
echo.
REM Launch the watchdog in a new window that stays open so you can see status.
start "Personal Data Services" pwsh -NoExit -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-services.ps1" -HealthIntervalSeconds 15
echo Watchdog launched in a new window.
echo Close that window (or press Ctrl-C in it) to stop all services.
timeout /t 3 >nul
exit
