@echo off
cd /d "%~dp0.."
set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Python 3.14 runtime was not found: %PYTHON_EXE%
  echo Install the project dependencies with that Python runtime, then run this file again.
  pause
  exit /b 1
)
if not exist .env (
  echo Missing .env. Copy .env.example to .env and add your mock credentials first.
  pause
  exit /b 1
)
set "DASHBOARD_URL=http://127.0.0.1:8765"
if not "%~1"=="" set "DASHBOARD_URL=%DASHBOARD_URL%/dashboard/index.html?account=%~1"
start "Kiwoom Dashboard" cmd /k ""%PYTHON_EXE%" dashboard\dashboard_server.py"
timeout /t 2 /nobreak >nul
start "" "%DASHBOARD_URL%"
