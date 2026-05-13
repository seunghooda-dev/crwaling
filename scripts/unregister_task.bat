@echo off
set TASK_NAME=NewsroomCrawlerAutoCollect
schtasks /Delete /TN "%TASK_NAME%" /F
echo Removed Windows scheduled task: %TASK_NAME%
pause

