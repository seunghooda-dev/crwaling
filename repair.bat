@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv missing, running install.
  call install.bat
)
".venv\Scripts\python.exe" -m app.cli init-db
".venv\Scripts\python.exe" -m app.cli validate-config
".venv\Scripts\python.exe" -m app.cli rescore
".venv\Scripts\python.exe" -m app.cli rebuild-clusters --limit 1000
echo Repair complete.
pause

