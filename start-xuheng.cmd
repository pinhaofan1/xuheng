@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-xuheng.ps1" -OpenBrowser
if errorlevel 1 pause
