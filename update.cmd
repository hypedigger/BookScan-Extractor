@echo off
setlocal
rem Update the application: latest code, then dependencies.
cd /d "%~dp0"
echo Fetching the latest version...
git pull --ff-only
if errorlevel 1 goto failed
echo Synchronising the dependencies...
uv sync
if errorlevel 1 goto failed
echo.
echo Up to date. Start the application from the Start Menu.
pause
exit /b 0
:failed
echo.
echo The update failed. The previous version is still installed.
pause
exit /b 1
