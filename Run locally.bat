@echo off
chcp 65001 >nul
title Shutdown_Checker (local)
cd /d "%~dp0"
echo Runs the bot on this PC (polling mode) with settings from .env.
echo Do not run it with the same TELEGRAM_TOKEN as the Render service: the two would fight over updates.
echo.
py main.py
echo.
pause
