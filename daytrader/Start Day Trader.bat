@echo off
REM Windows launcher: double-click this file
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python is not installed. Get it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then double-click this again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up, about a minute...
  %PY% -m venv .venv || (echo Could not create the environment. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (echo Install failed, check your internet. & pause & exit /b 1)
echo Starting Day Trader... your browser will open. Keep this window open while trading; close it to quit.
".venv\Scripts\python.exe" app.py --open
pause
