@echo off
setlocal
set "PROJ_ROOT=%~dp0"
cd /d "%PROJ_ROOT%"

echo ===================================================
echo   App Launcher (Windows / Python-only Environment)
echo ===================================================
echo.

:: 0. Force uv to use local system Python (disable downloading Python binaries)
set "UV_PYTHON_PREFERENCE=only-system"

:: 1. Auto-detect existing uv in standard locations
where uv >nul 2>&1
if %errorlevel% neq 0 (
    if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    if exist "%LOCALAPPDATA%\bin\uv.exe" set "PATH=%LOCALAPPDATA%\bin;%PATH%"
    if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    if exist "%APPDATA%\Python\Scripts\uv.exe" set "PATH=%APPDATA%\Python\Scripts;%PATH%"
)

:: 2. Bootstrap 'uv' via Python pip if missing
where uv >nul 2>&1
if %errorlevel% neq 0 (
    where python >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] Python is not installed or not in PATH.
        echo Please install Python and ensure it is added to your environment variables.
        echo.
        pause
        exit /b 1
    )
    echo [INFO] 'uv' package manager not found. Bootstrapping via pip...
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install uv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install 'uv'.
        echo.
        pause
        exit /b 1
    )
    echo [INFO] 'uv' installed successfully.
)

:: 3. Auto-detect Python entry point with multi-tier path resolution
set "ENTRY_POINT="

:: Check 1: In batch directory (%PROJ_ROOT%)
if exist "%PROJ_ROOT%app.py" set "ENTRY_POINT=app.py"
if not defined ENTRY_POINT if exist "%PROJ_ROOT%main.py" set "ENTRY_POINT=main.py"
if not defined ENTRY_POINT if exist "%PROJ_ROOT%src\app.py" set "ENTRY_POINT=src\app.py"

:: Check 2: In current working directory (%CD%)
if not defined ENTRY_POINT if exist "%CD%\app.py" (
    set "PROJ_ROOT=%CD%\"
    set "ENTRY_POINT=app.py"
)
if not defined ENTRY_POINT if exist "%CD%\main.py" (
    set "PROJ_ROOT=%CD%\"
    set "ENTRY_POINT=main.py"
)
if not defined ENTRY_POINT if exist "%CD%\src\app.py" (
    set "PROJ_ROOT=%CD%\"
    set "ENTRY_POINT=src\app.py"
)

:: Check 3: In parent directory (if run.bat was moved into a subfolder)
if not defined ENTRY_POINT if exist "%PROJ_ROOT%..\app.py" (
    cd /d "%PROJ_ROOT%.."
    set "PROJ_ROOT=%CD%\"
    set "ENTRY_POINT=app.py"
)

if not defined ENTRY_POINT (
    echo [ERROR] Python entry point [app.py / main.py / src\app.py] not found.
    echo.
    echo -------------------------------------------------------------
    echo Diagnostic Information:
    echo   Script Directory : %~dp0
    echo   Current Directory: %CD%
    echo -------------------------------------------------------------
    echo.
    echo Possible causes and solutions:
    echo 1. You may have copied or moved 'run.bat' out of the project folder.
    echo    - Do NOT copy 'run.bat' directly to your Desktop.
    echo    - Instead, right-click 'run.bat' in the project folder and choose:
    echo      'Show more options' -^> 'Send to' -^> 'Desktop ^(create shortcut^)'.
    echo 2. The project directory was moved or renamed.
    echo.
    pause
    exit /b 1
)

cd /d "%PROJ_ROOT%"

echo [INFO] Entry point found: %ENTRY_POINT%
echo [INFO] Working directory: %PROJ_ROOT%

:: 4. Auto-create .venv and sync package dependencies
if not exist ".venv" (
    echo [INFO] Creating virtual environment...
    uv venv --python python
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment .venv.
        echo.
        pause
        exit /b %errorlevel%
    )
    echo [INFO] Virtual environment created successfully.
)

if exist "pyproject.toml" (
    echo [INFO] Syncing dependencies...
    uv sync
    if %errorlevel% neq 0 (
        echo [ERROR] Dependency sync [uv sync] failed.
        echo Please check your pyproject.toml configuration.
        echo.
        pause
        exit /b %errorlevel%
    )
)

:: 5. Launch Application (adaptive for Streamlit / Python GUI)
echo.
echo [INFO] Launching %ENTRY_POINT% ...
echo.

findstr /i "streamlit" pyproject.toml >nul 2>&1
if %errorlevel% equ 0 (
    uv run streamlit run "%ENTRY_POINT%" --server.headless false
) else (
    uv run python "%ENTRY_POINT%"
)

if %errorlevel% neq 0 (
    echo.
    echo [WARNING] Application stopped or encountered an error.
)

echo.
pause
