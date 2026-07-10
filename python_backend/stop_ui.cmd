@echo off
setlocal

for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
set "PYTHON_EXE=%ROOT_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing virtual environment Python: %PYTHON_EXE%
  echo Create the environment first, then run this script again.
  exit /b 1
)

"%PYTHON_EXE%" "%ROOT_DIR%\python_backend\scripts\project.py" stop
exit /b %errorlevel%
