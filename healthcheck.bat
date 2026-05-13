@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo FAIL: .venv not found.
  exit /b 1
)
".venv\Scripts\python.exe" -m app.cli validate-config
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -c "from app.database import connect; conn=connect(); print('DB OK', conn.execute('select count(1) from articles').fetchone()[0])"
echo Healthcheck complete.

