@echo off
setlocal
cd /d "%~dp0"
rem Explicit dry-run launcher. It never enables Demo writes or reads Production credentials.
"%~dp0.venv\Scripts\python.exe" -m live15_quant.demo_first_fill --launch-source CODEX_DRY_RUN --launcher-name Start_Demo_First_Fill_Dry_Run.cmd
pause
