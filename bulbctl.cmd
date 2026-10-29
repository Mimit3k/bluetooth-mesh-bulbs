@echo off
rem Windows launcher for the CLI, using the project's virtualenv.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment is missing.
    echo Run setup-windows.ps1 first, from this folder:
    echo     powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
    exit /b 1
)

".venv\Scripts\python.exe" "bulbctl.py" %*
exit /b %errorlevel%
