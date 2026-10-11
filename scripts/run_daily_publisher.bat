@echo off
setlocal enabledelayedexpansion

REM ==============================================================================
REM AmazonAffiliateBot - Daily Publishing Pipeline Launcher for Windows Task Scheduler
REM ==============================================================================

REM 1. Set working directory to project root (parent of scripts/)
cd /d "%~dp0\.."

REM 2. Ensure log directory exists
if not exist "data\logs" mkdir "data\logs"

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
            echo [%date% %time%] [ERROR] Python interpreter not found in PATH or virtualenv. >> data\logs\daily_publisher.log
            exit /b 1
        )
    )
)

REM 4. Execute daily queue publisher
echo [%date% %time%] [INFO] Launching daily queue publisher...
!PYTHON_EXE! amazon_affiliate.py --publish-queue %*
set EXIT_CODE=!errorlevel!

REM 5. Exit with process exit code
exit /b !EXIT_CODE!
