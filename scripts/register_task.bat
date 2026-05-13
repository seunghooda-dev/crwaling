@echo off
cd /d "%~dp0\.."
set TASK_NAME=NewsroomCrawlerAutoCollect
set CRAWLER=%CD%\run_crawler.bat
schtasks /Create /TN "%TASK_NAME%" /TR "\"%CRAWLER%\"" /SC MINUTE /MO 5 /F
echo Registered Windows scheduled task: %TASK_NAME%
pause

