@echo off
setlocal enabledelayedexpansion

REM ==============================================================================
REM AmazonAffiliateBot - Local Management Dashboard Launcher
REM Runs strictly on localhost (127.0.0.1)
REM ==============================================================================

REM 1. Set working directory to project root
cd /d "%~dp0\.."

REM 2. Ensure data directories exist
if not exist "data\logs" mkdir "data\logs"
if not exist "data\queue" mkdir "data\queue"
if not exist "data\products" mkdir "data\products"
if not exist "data\generated" mkdir "data\generated"

REM 3. Resolve Python interpreter
set "PYTHON_EXE="

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
    where py >nul 2>&1
    if !errorlevel! EQU 0 (
        set "PYTHON_EXE=py -3"
    ) else (
        where python >nul 2>&1
        if !errorlevel! EQU 0 (
            set "PYTHON_EXE=python"
        ) else (
            echo [ERROR] Python interpreter not found in PATH or virtualenv.
            pause
            exit /b 1
        )
    )
)

REM 3.5 Ensure Ollama background server is running if installed
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if !errorlevel! NEQ 0 (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        echo [INFO] Ollama server offline. Launching background server...
        start /b "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve >nul 2>&1
        timeout /t 3 /nobreak >nul 2>&1
    )
)

REM 4. Start/reuse the dashboard, verify HTTP readiness, then open browser.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_dashboard.ps1" -OpenBrowser %*
exit /b !errorlevel!
