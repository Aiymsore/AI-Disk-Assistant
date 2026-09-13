@echo off
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" demo\create_demo_files.py
    ".venv\Scripts\python.exe" gui.py
) else (
    python demo\create_demo_files.py
    python gui.py
)
if errorlevel 1 pause
