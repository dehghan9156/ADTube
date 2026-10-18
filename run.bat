@echo off
title YouTube Downloader
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Please install it from https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH".
  pause
  exit /b 1
)

echo Installing requirements (first time only, needs internet)...
%PY% -m pip install -q -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
  echo Install failed. Check your internet connection and try again.
  pause
  exit /b 1
)

echo.
echo Starting... open this address in your browser:  http://127.0.0.1:5000
echo (Close this window to stop the program.)
start "" http://127.0.0.1:5000
%PY% main.py
pause
