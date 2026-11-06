@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The app is not set up yet. Double-click setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m qb2anki ui
echo.
echo The app has stopped. Double-click start.bat to open it again.
pause
