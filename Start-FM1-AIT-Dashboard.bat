@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Starting FM1 AIT local YouTrack proxy on port 8767...

rem Use runtime errorlevel checks. Do not use %%errorlevel%% inside a parenthesized block:
rem cmd.exe expands that variable before the block executes.
set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
  where python >nul 2>&1
  if not errorlevel 1 (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)

if not defined PYTHON_CMD (
  echo Python was not found through the Windows PATH or Python Launcher.
  echo.
  echo Detected PATH: %PATH%
  echo.
  echo If Python is installed but not on PATH, add its install folder to PATH
  echo or run the proxy manually with your Python executable:
  echo   python fm1-ait-proxy.py
  pause
  exit /b 1
)

if not exist "%~dp0fm1-ait-dashboard.html" (
  echo fm1-ait-dashboard.html must be in the same folder as this file.
  pause
  exit /b 1
)

echo Using: %PYTHON_CMD%
start "FM1 AIT Proxy" /min cmd /k %PYTHON_CMD% "%~dp0fm1-ait-proxy.py"

timeout /t 2 /nobreak >nul
echo Opening FM1 AIT dashboard...
start "" "http://127.0.0.1:8767/"
echo.
echo The dashboard is open at http://127.0.0.1:8767/ - keep the proxy window running while you work.
endlocal
