@echo off
setlocal
title MC Bot
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (set "PY=py -3") else (set "PY=python")
:loop
echo [%date% %time%] starting bot.py ...
%PY% bot.py
echo [%date% %time%] bot exited, restarting in 5s ...
timeout /t 5 /nobreak >nul
goto loop
