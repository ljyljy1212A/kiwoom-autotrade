@echo off
cd /d "%~dp0.."
set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
"%PYTHON_EXE%" -m src.worker_supervisor stop --account kr_mock --market KR
