@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" exit /b 2
if exist ".secrets\pyth-api-key.txt" set "LIVE15_PYTH_API_KEY_PATH=%CD%\.secrets\pyth-api-key.txt"
".venv\Scripts\python.exe" -m live15_quant.runtime_supervisor
exit /b %errorlevel%
