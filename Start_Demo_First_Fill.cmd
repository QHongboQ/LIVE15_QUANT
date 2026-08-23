@echo off
setlocal
cd /d "%~dp0"
rem Explicit operator-launched Demo-only write opt-in.  Never add other venue flags here.
rem The worker enforces one 1-contract IOC POST, existing Price/EV and hard-risk gates,
rem no retry/cancel, and automatic stop after the single attempt.
"%~dp0.venv\Scripts\python.exe" -m live15_quant.demo_first_fill --execute-approved
pause
