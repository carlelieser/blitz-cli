@echo off
title Blitz CLI Installer
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed or not on PATH.
    echo Install it from https://python.org, then run this script again.
    pause
    exit /b 1
)

where node >nul 2>&1
if %errorlevel% neq 0 (
    echo Node.js is not installed or not on PATH.
    echo Install it from https://nodejs.org, then run this script again.
    pause
    exit /b 1
)

python blitz.py %*
if %errorlevel% neq 0 (
    echo.
    echo Something went wrong. See the error above.
    pause
    exit /b 1
)
pause
