@echo off
rem Opens the solpaper portal in your browser and starts the scanner and study.
rem Keep this window open; closing it (or the portal's Quit button) stops everything.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run "Setup solpaper.bat" first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m solpaper.portal --autostart
if errorlevel 1 pause
