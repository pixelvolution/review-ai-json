@echo off
rem Builds solpaper.exe (a small launcher) into this folder. Run once; re-run only if
rem launcher.py changes. A git pull updates solpaper itself without rebuilding.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run "Setup solpaper.bat" first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller || goto :fail
".venv\Scripts\python.exe" -m PyInstaller --onefile --noconsole --name solpaper ^
  --distpath . --workpath build\pyinstaller --specpath build\pyinstaller launcher.py || goto :fail
echo.
echo Built solpaper.exe in %CD%. Double-click it to open the portal.
echo (You can right-click it and choose "Send to - Desktop (create shortcut)".)
pause
exit /b 0
:fail
echo Build failed - see the messages above.
pause
exit /b 1
