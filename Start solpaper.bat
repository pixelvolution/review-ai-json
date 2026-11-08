@echo off
rem Starts solpaper in the background and opens the portal in your browser.
rem Same as double-clicking solpaper.exe. Stop it with the Quit button in the portal.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run "Setup solpaper.bat" first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" launcher.py
if errorlevel 1 pause
