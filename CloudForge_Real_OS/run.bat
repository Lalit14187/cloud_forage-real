@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install from https://www.python.org/downloads/ and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Creating local Python environment...
  %PY% -m venv .venv
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
echo.
echo CloudForge is starting at http://127.0.0.1:5000
echo Keep this window open. Press Ctrl+C to stop.
.venv\Scripts\python.exe app.py
goto end
:failed
echo Setup failed. Check Python and internet access for the first package install.
pause
:end
endlocal
