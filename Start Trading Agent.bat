@echo off
REM Double-click this file to start the dashboard. First run installs everything (a few minutes).
cd /d "%~dp0"
title NSE Intraday Agent
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  echo Install it from https://www.python.org/downloads/ and TICK "Add Python to PATH" in the installer, then run this again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time...
  %PY% -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Installation failed. Check your internet connection and try again.
  pause
  exit /b 1
)
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
  >"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
  >>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)
echo Opening the dashboard in your browser. Keep this window open while you use it.
".venv\Scripts\python.exe" -m streamlit run app.py
pause
