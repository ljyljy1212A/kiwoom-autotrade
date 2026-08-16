@echo off
cd /d "%~dp0.."
set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
set "AUTO_TRADING_ENABLED=false"
if not exist "%PYTHON_EXE%" (
  echo Python 3.14 runtime was not found: %PYTHON_EXE%
  pause
  exit /b 1
)
"%PYTHON_EXE%" -m src.worker_supervisor start --account us_mock --market US
