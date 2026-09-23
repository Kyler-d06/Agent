@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -c "import flask, waitress, bs4, requests, jsonschema, mcp, frontmatter, psutil, yaml" >nul 2>&1
if errorlevel 1 (
  echo Installing required packages...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)

set "PATH_SETUP="
if /I "%~1"=="configure" set "PATH_SETUP=--configure-paths"
set "ALPACA_SETUP="
if /I "%~1"=="trading" set "ALPACA_SETUP=--setup-alpaca"
set "TELEGRAM_SETUP="
if /I "%~1"=="telegram" set "TELEGRAM_SETUP=--setup-telegram"
".venv\Scripts\python.exe" start_platform.py --pilot %PATH_SETUP% %ALPACA_SETUP% %TELEGRAM_SETUP%
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo The pilot did not start. Review the error above.
pause
exit /b 1
