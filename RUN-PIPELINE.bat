@echo off
rem OpsPilot pipeline - one-click refresh (ASCII only on purpose)
rem Sync site snapshot -> director scan (rule fallback when no LLM key) ->
rem workshop proposals -> review, writing everything into the tenant dir the
rem console actually reads.
rem Prereq: trade site running on :3000, SITE_AGENT_TOKEN env var set.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  pause
  exit /b 1
)
if "%SITE_AGENT_TOKEN%"=="" (
  echo [ERROR] SITE_AGENT_TOKEN is not set in this window.
  echo         Close this window and open a NEW one, then retry.
  pause
  exit /b 1
)

set T=data\tenants\globaltrade

echo [1/4] Syncing site snapshot from http://localhost:3000/api/agent ...
python run.py site sync --out %T%\site_snapshot.json
if errorlevel 1 goto fail

rem --planner auto: with an LLM key this is the director (store health score +
rem task orders); without one it falls back to the rule engine and says so.
rem Do NOT hardcode "director run" here: a missing key would abort the
rem whole pipeline and leave the console with yesterday's data.
echo [2/4] Director: store health + task orders (rule fallback if no key) ...
python run.py supervisor run --planner auto --out %T%\task_orders.json
if errorlevel 1 goto fail

echo [3/4] Workshop: generating proposals ...
python run.py workshop run --tasks %T%\task_orders.json --snapshot %T%\site_snapshot.json --out %T%\workshop_proposals.json
if errorlevel 1 goto fail

echo [4/4] Review: compliance check ...
python run.py review run --proposals %T%\workshop_proposals.json --snapshot %T%\site_snapshot.json --out %T%\review_results.json
if errorlevel 1 goto fail

echo.
echo ============================================================
echo   Pipeline done. Refresh the OpsPilot console (F5) to see
echo   the new tasks and proposals.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Pipeline failed. See messages above.
pause
exit /b 1
