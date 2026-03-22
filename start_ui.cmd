@echo off
setlocal

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "BACKEND_TITLE=Football Tracking API"
set "FRONTEND_TITLE=Football Tracking Frontend"

if /i "%~1"=="--check" goto check

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing virtual environment Python: %PYTHON_EXE%
  echo Create the environment first, then run this script again.
  exit /b 1
)

if not exist "%ROOT_DIR%frontend\package.json" (
  echo [ERROR] Missing frontend\package.json
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found in PATH.
  echo Install Node.js and ensure npm is available, then run this script again.
  exit /b 1
)

if not exist "%ROOT_DIR%frontend\node_modules" (
  echo [INFO] frontend\node_modules not found. Installing frontend dependencies...
  pushd "%ROOT_DIR%frontend"
  call npm install
  if errorlevel 1 (
    popd
    echo [ERROR] npm install failed.
    exit /b 1
  )
  popd
)

echo [INFO] Starting backend window...
start "%BACKEND_TITLE%" /D "%ROOT_DIR%" cmd /k ""%PYTHON_EXE%" -m uvicorn football_tracking.api.app:app --host 127.0.0.1 --port 8000 --reload"

echo [INFO] Waiting for backend health endpoint...
powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(30); $ok=$false; while((Get-Date) -lt $deadline -and -not $ok){ try { $resp=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/v1/health -TimeoutSec 2; if($resp.StatusCode -eq 200){ $ok=$true } } catch { Start-Sleep -Seconds 1 } }; if(-not $ok){ exit 1 }"
if errorlevel 1 (
  echo [ERROR] Backend failed to start correctly. Check the backend window for details.
  exit /b 1
)

echo [INFO] Starting frontend window...
start "%FRONTEND_TITLE%" /D "%ROOT_DIR%frontend" cmd /k "npm run dev -- --host 127.0.0.1"

echo [INFO] Open http://127.0.0.1:5173 after Vite is ready.
exit /b 0

:check
echo ROOT_DIR=%ROOT_DIR%
echo PYTHON_EXE=%PYTHON_EXE%
if exist "%PYTHON_EXE%" (
  echo PYTHON_OK=1
) else (
  echo PYTHON_OK=0
)
if exist "%ROOT_DIR%frontend\package.json" (
  echo FRONTEND_OK=1
) else (
  echo FRONTEND_OK=0
)
where npm >nul 2>nul
if errorlevel 1 (
  echo NPM_OK=0
) else (
  echo NPM_OK=1
)
exit /b 0
