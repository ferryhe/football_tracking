@echo off
setlocal

set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing virtual environment Python: %PYTHON_EXE%
  echo Create the environment first, then run this script again.
  exit /b 1
)

"%PYTHON_EXE%" "%ROOT_DIR%scripts\start_ui.py" %*
exit /b %errorlevel%
