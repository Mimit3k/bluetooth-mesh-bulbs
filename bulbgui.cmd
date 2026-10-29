@echo off
rem Windows launcher: run the desktop app with the project's virtualenv.
rem Keep this file next to bulbgui.py; no PATH changes are needed.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo The virtual environment is missing.
    echo Run setup-windows.ps1 first, from this folder:
    echo     powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
    pause
    exit /b 1
)

rem pythonw keeps the console window closed. To see startup errors, run
rem     .venv\Scripts\python.exe bulbgui.py
rem from PowerShell instead.
start "Mesh Bulbs" ".venv\Scripts\pythonw.exe" "bulbgui.py" %*
exit /b 0
