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

".venv\Scripts\python.exe" start_platform.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo The assistant did not start. Review the error above.
pause
exit /b 1
