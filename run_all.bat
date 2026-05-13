@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run install.bat first.
  pause
  exit /b 1
)
start "Newsroom Dashboard" "%~dp0run_server.bat"
start "Newsroom Auto Crawler" "%~dp0run_crawler.bat"
echo Dashboard and auto crawler started.
echo Open http://127.0.0.1:8000
pause

