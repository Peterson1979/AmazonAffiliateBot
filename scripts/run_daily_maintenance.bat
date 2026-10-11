@echo off
setlocal enabledelayedexpansion

REM ==============================================================================
REM AmazonAffiliateBot - Daily Queue Maintenance Launcher for Windows Task Scheduler
REM ==============================================================================

REM 1. Set working directory to project root (parent of scripts/)
cd /d "%~dp0\.."

REM 2. Ensure log and discovery directories exist
if not exist "data\logs" mkdir "data\logs"
if not exist "data\discovery" mkdir "data\discovery"

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
            echo [%date% %time%] [ERROR] Python interpreter not found in PATH or virtualenv. >> data\logs\queue_maintenance.log
            exit /b 1
        )
    )
)

REM 3.5 Ensure Ollama background server is running if installed
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if !errorlevel! NEQ 0 (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        echo [%date% %time%] [INFO] Ollama server offline. Launching background server...
        start /b "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve >nul 2>&1
        timeout /t 3 /nobreak >nul 2>&1
    )
)

REM 4. Execute daily queue maintainer
echo [%date% %time%] [INFO] Launching daily queue maintenance...
!PYTHON_EXE! amazon_affiliate.py --maintain-queue %*
set EXIT_CODE=!errorlevel!

REM 5. Exit with process exit code
exit /b !EXIT_CODE!
