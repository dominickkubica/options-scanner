@echo off
rem Double-click to install. Runs install.ps1 from this folder, for this user only.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
