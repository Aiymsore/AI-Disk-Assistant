@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Running setup_windows.bat first...
    call setup_windows.bat
    if errorlevel 1 exit /b 1
)

echo Installing build dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto :fail

echo Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist AI-Disk-Assistant.spec del /q AI-Disk-Assistant.spec
if exist AI-Disk-Assistant-GUI.spec del /q AI-Disk-Assistant-GUI.spec

echo Building command-line executable...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --onefile --name AI-Disk-Assistant main.py
if errorlevel 1 goto :fail

echo Building graphical executable...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --onefile --windowed --name AI-Disk-Assistant-GUI gui.py
if errorlevel 1 goto :fail

echo.
echo Build completed:
echo   dist\AI-Disk-Assistant.exe
echo   dist\AI-Disk-Assistant-GUI.exe
echo.
echo Do not commit dist to the source repository. Upload the EXE files to GitHub Releases.
pause
exit /b 0

:fail
echo Build failed. Check the messages above.
pause
exit /b 1
