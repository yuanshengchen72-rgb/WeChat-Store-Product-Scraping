@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if defined PYTHON_EXE goto create_venv
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PYTHON_EXE goto create_venv
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if defined PYTHON_EXE goto create_venv
if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python310\python.exe"
if defined PYTHON_EXE goto create_venv
goto no_python

:create_venv
echo Creating Python environment...
"%PYTHON_EXE%" -m venv .venv
if errorlevel 1 goto no_python

:install
echo Installing dependencies. Keep this window open...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed

echo.
echo [OK] Installation completed.
echo You can now run START.bat or the Chinese-named launcher.
pause
exit /b 0

:no_python
echo.
echo [ERROR] Python 3.10 or newer was not found.
echo Install Python, then run this file again.
goto failed

:install_failed
echo.
echo [ERROR] Dependency installation failed.
echo Check the network and run this file again.

:failed
pause
exit /b 1
