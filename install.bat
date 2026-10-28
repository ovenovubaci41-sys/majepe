@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing required Python packages...
py -m pip install --upgrade pip
if errorlevel 1 goto error
py -m pip install -r requirements.txt
if errorlevel 1 goto error
echo.
echo Installation complete. Start run.bat
pause
exit /b 0
:error
echo.
echo Installation failed. Check that Python 3.11+ is installed and added to PATH.
pause
exit /b 1
