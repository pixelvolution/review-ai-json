@echo off
rem One-time setup: makes .venv and installs solpaper's packages. Safe to run again.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv ...
  py -3 -m venv .venv || python -m venv .venv || goto :nopython
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r solpaper\requirements.txt || goto :fail
echo.
echo Setup done. Double-click "Start solpaper.bat" (or run build_exe.bat once to make solpaper.exe).
pause
exit /b 0
:nopython
echo Python 3.11+ was not found. Install it from python.org (tick "Add python.exe to PATH").
pause
exit /b 1
:fail
echo Package install failed - see the messages above.
pause
exit /b 1
