@echo off
chcp 65001 >nul
cd /d "%~dp0"
py app.py
if errorlevel 1 (
  echo.
  echo App closed with an error. Check installation and Python.
  pause
)
