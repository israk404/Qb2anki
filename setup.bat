@echo off
cd /d "%~dp0"
echo Creating the app's private Python environment (Python 3.12, 3.13 or 3.11)...
set PYOK=
py -3.12 -m venv .venv >nul 2>&1 && set PYOK=1
if not defined PYOK py -3.13 -m venv .venv >nul 2>&1 && set PYOK=1
if not defined PYOK py -3.11 -m venv .venv >nul 2>&1 && set PYOK=1
if not defined PYOK (
  rem no "py" launcher: try "python" if it is a suitable version
  python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1 && python -m venv .venv && set PYOK=1
)
if not defined PYOK (
  echo.
  echo Python 3.12 was not found. This app needs Python 3.11, 3.12 or 3.13. Newer versions such as 3.14 or 3.15 do NOT work yet.
  echo Install Python 3.12 from https://www.python.org/downloads/ ^(64-bit^), then run setup.bat again.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installing the packages failed. Read the messages above, then see "If something goes wrong" in README.md.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -c "from PIL import Image; import google.genai, pydantic; print('Packages import OK')"
if errorlevel 1 (
  echo.
  echo A package did not install correctly. See "If something goes wrong" in README.md.
  pause
  exit /b 1
)
echo.
echo All set. Double-click start.bat to open the app. In the app, open Settings and paste your Google AI key.
pause
