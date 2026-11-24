@echo off
rem OpsPilot console - one-click launcher (Windows)
rem ASCII only on purpose: batch files with non-ASCII text can be mangled by codepage issues.
rem Usage:
rem   START-WINDOWS.bat          -> demo mode (read-only preview, publish disabled)
rem   START-WINDOWS.bat full     -> normal mode (approval + publish enabled)

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.11 or later first.
  pause
  exit /b 1
)

if not exist "data\console\users.json" (
  echo [1/3] First run: creating console data and demo accounts...
  call python run.py console init --admin-password "Demo12345" --approver-password "Approve12345" --operator-password "Operate12345"
  if errorlevel 1 (
    echo [ERROR] Init failed. See messages above.
    pause
    exit /b 1
  )
) else (
  echo [1/3] Console data found, skipping init.
)

echo [2/3] Syncing site snapshot from :3000 ...
if "%SITE_AGENT_TOKEN%"=="" (
  echo [WARN] SITE_AGENT_TOKEN not set - keeping existing snapshot.
) else (
  call python run.py site sync --out data\tenants\globaltrade\site_snapshot.json
  if errorlevel 1 echo [WARN] Sync failed - keeping existing snapshot.
)

set MODE=demo
set RUN_ARGS=--demo
if /i "%~1"=="full" (
  set MODE=full
  set RUN_ARGS=
)

echo [3/3] Starting OpsPilot console (%MODE% mode) on http://127.0.0.1:8788 ...
echo.
echo ============================================================
echo   Console    : http://127.0.0.1:8788
echo   Accounts   : admin / approver / operator
echo   First run  : admin / Demo12345
echo                approver / Approve12345
echo                operator / Operate12345
echo   Mode       : %MODE%
if /i "%~1"=="full" (
  echo   ^(approval and publish are ENABLED^)
) else (
  echo   Demo mode  : read-only preview, publish disabled
  echo   Full mode  : run "START-WINDOWS.bat full"
)
echo   Stop       : press Ctrl+C in this window
echo ============================================================
echo.

start "" "http://127.0.0.1:8788"
call python run.py console serve --host 127.0.0.1 --port 8788 %RUN_ARGS%
pause
