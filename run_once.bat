@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Called by Windows Task Scheduler every 3 minutes; pythonw keeps it windowless.
REM Logs: logs\run_once.log (written by the script) + logs\task.log (exit codes).
REM Interpreter: %LOCALAPPDATA% Python 3.14 first, else pythonw from PATH.
REM Keep this file pure ASCII - cmd.exe garbles non-ASCII in .bat without a BOM.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PYW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
"%PYW%" "%~dp0lib\run_once.py" >> "%~dp0logs\task.log" 2>&1
exit /b %errorlevel%
