@echo off
rem Tapo Camera Viewer installer for Windows. Double-click it, or run from a terminal.
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

echo Creating virtualenv...
if not exist venv\Scripts\python.exe py -3 -m venv venv || goto :fail
echo Installing dependencies...
venv\Scripts\python -m pip install -q --upgrade pip
venv\Scripts\python -m pip install -q -r requirements.txt || goto :fail

echo.
echo Installed. Double-click run.bat to start.
echo.
echo Before first use, in the Tapo app:
echo   1. Camera ^> Settings ^> Advanced Settings ^> Camera Account  - set a username/password
echo   2. Me ^> Tapo Lab ^> Third-Party Compatibility                - turn ON
pause
exit /b 0

:fail
echo Install failed.
pause
exit /b 1
