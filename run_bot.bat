@echo off
chcp 65001 >nul
setlocal
title MC Bot
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (set "PY=py -3") else (set "PY=python")
:loop
if exist "bot_stop.flag" (
  del "bot_stop.flag" >nul 2>nul
  echo [%date% %time%] 收到停止标记，不再重启。
  goto end
)
echo [%date% %time%] starting bot.py ...
%PY% bot.py
if exist "bot_stop.flag" (
  del "bot_stop.flag" >nul 2>nul
  echo [%date% %time%] bot 已停止（斜线指令已从 Discord 移除），不自动重启。
  goto end
)
echo [%date% %time%] bot exited, restarting in 5s ...
timeout /t 5 /nobreak >nul
goto loop
:end
echo.
echo [OK] 已停止。要重新上线请再跑 start.bat
