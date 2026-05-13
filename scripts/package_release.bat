@echo off
cd /d "%~dp0\.."
if not exist dist mkdir dist
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package_release.ps1
pause

