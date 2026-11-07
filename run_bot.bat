@echo off
chcp 65001 >nul
setlocal
title MC Bot
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (set "PY=py -3") else (set "PY=python")

rem 第一次執行：自動裝 Python 依賴（不用手動 pip install）
%PY% -c "import discord, aiohttp, websockets, dotenv" >nul 2>nul
if errorlevel 1 (
  echo [i] 偵測到缺少 Python 依賴，自動安裝中...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [X] 依賴安裝失敗，請手動執行: %PY% -m pip install -r requirements.txt
    pause
    goto end
  )
)

rem 第一次執行：幫忙把範本複製成 .env（token 還是要自己填）
if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [!] 已建立 .env（從 .env.example 複製）。
    echo     用記事本打開 .env，填入 DISCORD_TOKEN 後存檔，再跑一次 start.bat
    pause
    goto end
  ) else (
    echo [X] 找不到 .env 也找不到 .env.example，無法啟動
    pause
    goto end
  )
)

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
  echo [%date% %time%] bot 已停止（斜线指令保留在 Discord），不自动重启。
  goto end
)
echo [%date% %time%] bot exited, restarting in 5s ...
timeout /t 5 /nobreak >nul
goto loop
:end
echo.
echo [OK] 已停止。要重新上线请再跑 start.bat
