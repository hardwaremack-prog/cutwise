@echo off
setlocal
cd /d "%~dp0"
title Cutwise - easy AI video editor
echo.
echo   ===  Cutwise  ===
echo.

rem ---- FFmpeg (does the actual video work) ----
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links;%~dp0ffmpeg"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo   Installing FFmpeg ^(one time only^)...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)

rem ---- Python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo   Installing Python ^(one time only^)...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo   Python is installed. Please close this window and double-click "Start Cutwise" again.
  pause
  exit /b
)

rem ---- App setup (first run takes a minute) ----
if not exist ".venv\Scripts\python.exe" (
  echo   Setting up Cutwise ^(first run only, about a minute^)...
  %PY% -m venv .venv
)
if not exist ".venv\.ready" (
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 (
    echo   Something went wrong installing. Check your internet and try again.
    pause
    exit /b
  )
  echo ok> ".venv\.ready"
)

".venv\Scripts\python.exe" server.py
pause
