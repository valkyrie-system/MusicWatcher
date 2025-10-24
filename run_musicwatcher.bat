@echo off
setlocal enabledelayedexpansion

echo --- MusicWatcher Setup (Windows) ---

REM --- 1. Check for Python ---
echo Checking for Python...
where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo ERROR: Python could not be found in your PATH. Please install Python 3.
        pause
        exit /b 1
    ) else (
        set PYTHON_CMD=py
    )
) else (
    set PYTHON_CMD=python
)
echo Found Python: %PYTHON_CMD%

REM --- 2. Check for venv module ---
echo Checking for venv module...
%PYTHON_CMD% -m venv --help >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Python venv module not found or Python installation is incomplete.
    echo Please ensure Python 3 is installed correctly and includes the standard library.
    pause
    exit /b 1
)

REM --- 3. Set up Python virtual environment ---
set VENV_DIR=venv
if not exist "%VENV_DIR%\\Scripts\\activate.bat" (
    echo Creating Python virtual environment in '%VENV_DIR%'...
    %PYTHON_CMD% -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment '%VENV_DIR%' already exists.
)

REM --- 4. Activate virtual environment ---
echo Activating virtual environment...
call "%VENV_DIR%\\Scripts\\activate.bat"
if !errorlevel! neq 0 (
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)

REM --- 5. Install dependencies ---
echo Installing dependencies from requirements.txt...
pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo ERROR: Failed to install dependencies.
    call "%VENV_DIR%\\Scripts\\deactivate.bat"
    pause
    exit /b 1
)

REM --- 6. Launch MusicWatcher ---
echo Launching MusicWatcher...
%PYTHON_CMD% musicwatcher.py

REM --- 7. Deactivate on exit (optional, happens automatically when script closes) ---
REM call "%VENV_DIR%\\Scripts\\deactivate.bat"

echo.
echo --- MusicWatcher Exited ---
pause
endlocal
