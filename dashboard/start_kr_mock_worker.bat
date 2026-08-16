@echo off
cd /d "%~dp0.."
set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Python 3.14 runtime was not found: %PYTHON_EXE%
  pause
  exit /b 1
)
set "GRID_BUY_REENTRY_DELAY_SEC=5"
set "AUTO_TRADING_ENABLED=false"
"%PYTHON_EXE%" -m src.worker_supervisor start --account kr_mock --market KR
