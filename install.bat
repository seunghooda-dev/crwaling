@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 (
  python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m app.cli init-db
echo Install complete.
pause

