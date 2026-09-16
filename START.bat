@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto no_python
".venv\Scripts\python.exe" -c "import typing; from typing_extensions import NotRequired; typing.NotRequired=NotRequired; import frida" >nul 2>&1
if errorlevel 1 goto no_frida
if /i "%~1"=="--check" goto ready

echo Starting WeChat Shop Scraper...
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto app_failed
exit /b 0

:no_python
echo [ERROR] Python environment is missing.
echo Run INSTALL.bat first.
goto failed

:no_frida
echo [ERROR] Python package "frida" is not installed.
echo Run INSTALL.bat first.
goto failed

:app_failed
echo [ERROR] The application exited with an error.
goto failed

:ready
echo [OK] Launcher prerequisites are ready.
exit /b 0

:failed
if /i "%~1"=="--check" exit /b 1
pause
exit /b 1
