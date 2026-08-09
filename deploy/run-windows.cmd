@echo off
setlocal EnableExtensions
REM Double-click / cmd.exe launcher for deploy\run-windows.sh (Git Bash)
cd /d "%~dp0\.."

where bash >nul 2>&1
if errorlevel 1 (
  echo ERROR: bash not found. Install Git for Windows, then re-run this file.
  echo Or open Git Bash and run:  bash deploy/run-windows.sh
  exit /b 1
)

bash "%~dp0run-windows.sh" %*
exit /b %ERRORLEVEL%
