@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3 was not found. Install Python 3.10 or newer and enable Add Python to PATH.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=python"
)

echo [2/4] Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo Failed to create .venv.
        pause
        exit /b 1
    )
) else (
    echo .venv already exists; keeping it.
)

echo [3/4] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [4/4] Preparing configuration...
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Created .env from .env.example.
    echo Open .env and fill in your API settings before enabling AI.
) else (
    echo .env already exists; it was not overwritten.
)

echo.
echo Setup completed.
echo Run launch_gui.bat to start the application.
pause
exit /b 0

:fail
echo Installation failed. Check the messages above.
pause
exit /b 1
