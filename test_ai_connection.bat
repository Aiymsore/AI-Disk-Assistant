@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" tools\test_ai_connection.py
) else (
    python tools\test_ai_connection.py
)
pause
