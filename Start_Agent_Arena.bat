@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0Start_Agent_Arena.ps1"
pause
