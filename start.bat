@echo off
setlocal
title MC Stack
cd /d "%~dp0"
echo [1/2] starting MC-WS.exe ...
start "MC Bridge" "MC-WS.exe"
timeout /t 3 /nobreak >nul
echo [2/2] starting Discord bot (auto restart) ...
call run_bot.bat
