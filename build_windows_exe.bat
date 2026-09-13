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

echo Generating application icon from assets\logo.png...
".venv\Scripts\python.exe" tools\make_icon.py
if errorlevel 1 goto :fail

echo Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist P4Disk4P.spec del /q P4Disk4P.spec
if exist P4Disk4P-GUI.spec del /q P4Disk4P-GUI.spec

echo Building command-line executable...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --onefile --name P4Disk4P --icon assets\app.ico main.py
if errorlevel 1 goto :fail

echo Building graphical executable...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --onefile --windowed --name P4Disk4P-GUI --icon assets\app.ico gui.py
if errorlevel 1 goto :fail

echo.
echo Build completed:
echo   dist\P4Disk4P.exe
echo   dist\P4Disk4P-GUI.exe
echo.
echo Do not commit dist to the source repository. Upload the EXE files to GitHub Releases.
pause
exit /b 0

:fail
echo Build failed. Check the messages above.
pause
exit /b 1
